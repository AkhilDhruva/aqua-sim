#!/usr/bin/env python3
"""Derive 3D building massing for an area from the USGS NYC LiDAR survey.

    python3 -I examples/build_lidar_buildings.py \
        --laz-dir  <dir from fetch_nyc_lidar.py> \
        --dem USGS_one_meter_x58y451_NY_CMPG_2013.tif --dem USGS_one_meter_x58y452_NY_CMPG_2013.tif \
        --bbox -74.022 40.698 -73.968 40.742 --out lm_lidar_buildings.geojson.gz

Writes the building parts (nested height-band polygons, EPSG:26918 meters,
heights in meters above the 1 m DTM) as gzip GeoJSON, the 1 m roof-height
raster beside it (``*_ndsm.tif``), and a ``*_summary.json`` with the counts,
parameters and input digests. Feed the GeoJSON to
``examples/nyc_street_run.py --lidar-buildings``.
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from aqua_sim.ingestion.lidar import NYC_LIDAR_DATASET, buildings_from_lidar, write_geojson  # noqa: E402


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--laz-dir", required=True)
    ap.add_argument("--dem", action="append", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"), required=True)
    ap.add_argument("--crs", default="EPSG:26918")
    ap.add_argument("--band-m", type=float, default=3.0)
    ap.add_argument("--elevated-lines", default=None,
                    help="WGS84 GeoJSON of bridges/viaducts from fetch_overture_bridges.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from pyproj import Transformer
    t0 = time.time()
    tr = Transformer.from_crs("EPSG:4326", a.crs, always_xy=True)
    xs, ys = zip(*(tr.transform(lon, lat) for lon in (a.bbox[0], a.bbox[2]) for lat in (a.bbox[1], a.bbox[3])))
    bounds = (min(xs), min(ys), max(xs), max(ys))
    laz = sorted(glob.glob(os.path.join(a.laz_dir, "*.laz")))
    fetch_manifest = os.path.join(a.laz_dir, "fetch_manifest.json")
    print(f"[lidar] {len(laz)} LAZ files, bounds {[round(v) for v in bounds]} {a.crs}", flush=True)

    stem = a.out[:-len(".geojson.gz")] if a.out.endswith(".geojson.gz") else os.path.splitext(a.out)[0]
    prov = dict(NYC_LIDAR_DATASET)
    prov = dict(prov)
    prov["dem_sha256"] = {os.path.basename(p): sha256(p) for p in a.dem}
    if os.path.exists(fetch_manifest):
        prov["fetch_manifest_sha256"] = sha256(fetch_manifest)
    coll = buildings_from_lidar(laz, a.dem, bounds, crs=a.crs, provenance=prov,
                                raster_out=stem + "_ndsm.tif",
                                parts_kwargs={"band_m": a.band_m},
                                elevated_lines=a.elevated_lines)
    if a.elevated_lines:
        src = json.load(open(a.elevated_lines)).get("source", {})
        coll.provenance["elevated_lines"] = {"file": os.path.basename(a.elevated_lines),
                                             "sha256": sha256(a.elevated_lines), **src}
    write_geojson(coll, a.out)
    heights = sorted(b.height_m for b in coll.buildings)
    summary = {
        "out": os.path.basename(a.out), "sha256": sha256(a.out),
        "ndsm_tif": os.path.basename(stem + "_ndsm.tif"), "ndsm_sha256": sha256(stem + "_ndsm.tif"),
        "bbox_wgs84": a.bbox, "bounds": bounds, "crs": a.crs,
        "parts": len(coll), "regions": coll.provenance["detection"]["regions"],
        "tallest_part_m": heights[-1] if heights else None,
        "provenance": coll.provenance, "wall_seconds": round(time.time() - t0, 1),
    }
    json.dump(summary, open(stem + "_summary.json", "w"), indent=2)
    print(f"[lidar] {summary['regions']} building regions -> {summary['parts']} level parts, "
          f"tallest {summary['tallest_part_m']} m, {summary['wall_seconds']} s -> {a.out}", flush=True)
    print(json.dumps(coll.provenance["detection"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

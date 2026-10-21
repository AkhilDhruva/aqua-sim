#!/usr/bin/env python3
"""Download the USGS-hosted NYC LiDAR point cloud for a bounding box.

Source: USGS 3DEP Entwine Point Tiles, project ``NY_NewYorkCity`` in the
public ``usgs-lidar-public`` S3 bucket (4.76 billion points, the 2013-2014
New York City survey, the same acquisition as the USGS 1 m DEM project
``NY_CMPG_2013``). Points are stored in EPSG:3857 with NAVD88 heights.

    python3 -I examples/fetch_nyc_lidar.py --bbox -74.022 40.698 -73.968 40.742 \
        --out /path/to/empty/dir

Walks the EPT octree hierarchy, keeps every node whose cube intersects the
box (EPT stores each point exactly once, spread across all depths, so every
depth is needed for full density), downloads the LAZ files with retries, and
writes ``fetch_manifest.json`` with the URLs, byte sizes, SHA-256 digests and
UTC timestamps. Downloads are untrusted data: keep --out an empty directory
and read the files only with ``python3 -I``.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/NY_NewYorkCity"


def get(url, attempts=5):
    delay = 2
    for k in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as exc:  # network errors: back off 2/4/8/16 s
            if k == attempts - 1:
                raise RuntimeError(f"{url}: {exc}") from exc
            time.sleep(delay)
            delay *= 2


def lonlat_to_3857(lon, lat):
    import math
    r = 6378137.0
    return r * math.radians(lon), r * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def node_bounds(root, key):
    d, x, y, z = key
    size = [(root[i + 3] - root[i]) / (2 ** d) for i in range(3)]
    return (root[0] + x * size[0], root[1] + y * size[1], root[0] + (x + 1) * size[0], root[1] + (y + 1) * size[1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"), required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="list nodes and point counts only")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    start = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    ept = json.loads(get(f"{BASE}/ept.json"))
    root = ept["bounds"]
    w, s = lonlat_to_3857(a.bbox[0], a.bbox[1])
    e, n = lonlat_to_3857(a.bbox[2], a.bbox[3])

    def hits(key):
        bx0, by0, bx1, by1 = node_bounds(root, key)
        return bx0 < e and bx1 > w and by0 < n and by1 > s

    selected = {}
    pending = ["0-0-0-0"]
    hierarchy_files = 0
    while pending:
        hk = pending.pop()
        h = json.loads(get(f"{BASE}/ept-hierarchy/{hk}.json"))
        hierarchy_files += 1
        for k, count in h.items():
            key = tuple(int(v) for v in k.split("-"))
            if not hits(key):
                continue
            if count == -1:
                if k != hk:
                    pending.append(k)
            elif count > 0:
                selected[k] = count
    total = sum(selected.values())
    print(f"[ept] {len(selected)} nodes, {total:,} points in box (hierarchy files read: {hierarchy_files})", flush=True)
    if a.dry_run:
        return 0

    os.makedirs(a.out, exist_ok=True)

    def fetch(k):
        path = os.path.join(a.out, f"{k}.laz")
        if not os.path.exists(path):
            data = get(f"{BASE}/ept-data/{k}.laz")
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return k, os.path.getsize(path), digest

    done = []
    with ThreadPoolExecutor(a.workers) as pool:
        for i, res in enumerate(pool.map(fetch, sorted(selected)), 1):
            done.append(res)
            if i % 200 == 0 or i == len(selected):
                print(f"[ept] {i}/{len(selected)} files, {sum(r[1] for r in done) / 1e6:.0f} MB", flush=True)

    manifest = {
        "source": "USGS 3DEP Entwine Point Tiles, project NY_NewYorkCity (usgs-lidar-public)",
        "ept_url": f"{BASE}/ept.json",
        "data_url_template": f"{BASE}/ept-data/{{D-X-Y-Z}}.laz",
        "srs": "EPSG:3857 horizontal (reprojected by USGS from EPSG:26918), NAVD88 heights (m)",
        "acquisition": "2013-08 to 2014-04 (from point GPS times; matches USGS 1 m DEM NY_CMPG_2013)",
        "bbox_wgs84": a.bbox, "bbox_3857": [w, s, e, n],
        "fetch_start_utc": start, "fetch_end_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "node_count": len(done), "point_count": total,
        "bytes": sum(r[1] for r in done),
        "nodes": [{"key": k, "points": selected[k], "bytes": b, "sha256": h} for k, b, h in done],
    }
    with open(os.path.join(a.out, "fetch_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"[ept] done: {manifest['bytes'] / 1e6:.0f} MB -> {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

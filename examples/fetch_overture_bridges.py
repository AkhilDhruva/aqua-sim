#!/usr/bin/env python3
"""Extract elevated road and rail segments (bridges, viaducts) for a bbox from
Overture Maps' transportation theme, as GeoJSON lines for
``examples/build_lidar_buildings.py --elevated-lines``.

    python3 -I examples/fetch_overture_bridges.py --release 2026-10-21.0 \
        --bbox -74.022 40.698 -73.968 40.742 --out elevated_lm.geojson

Why: LiDAR cannot reliably tell a low elevated deck (FDR Drive viaduct, the
bridge approaches) from a low roof, and as solver walls such decks would dam
the streets water actually flows beneath. Overture's segments carry the
OpenStreetMap bridge flag and level. Kept: segments flagged ``is_bridge`` or
with a level >= 1 for their whole length, and not ``is_tunnel``/``is_indoor``.
Each line gets a deck width from its class (Overture width_rules when present).
Unnamed elevated footways/service roads (rooftop parking ramps, podium
terraces, in-building skyways) and closed footway loops are dropped.

Data: Overture Maps Foundation, transportation theme (OpenStreetMap
contributors, ODbL). Anonymous read from the public S3 bucket.
Needs pyarrow and shapely.
"""
import argparse
import datetime as dt
import json
import sys

# Deck width (m) by Overture road/rail class, used when width_rules is empty.
WIDTH_BY_CLASS = {
    "motorway": 22.0, "trunk": 20.0, "primary": 16.0, "secondary": 14.0, "tertiary": 12.0,
    "residential": 10.0, "unclassified": 10.0, "service": 8.0, "living_street": 8.0,
    "subway": 10.0, "standard_gauge": 10.0, "light_rail": 8.0,
    "footway": 4.0, "cycleway": 4.0, "path": 4.0, "steps": 4.0, "pedestrian": 6.0, "bridleway": 4.0,
}
DEFAULT_WIDTH = 8.0
FOOT_CLASSES = {"footway", "cycleway", "path", "steps", "pedestrian", "bridleway"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", required=True)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"), required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import pyarrow.fs as pafs
    from shapely import wkb

    W, S, E, N = a.bbox
    start = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path = f"overturemaps-us-west-2/release/{a.release}/theme=transportation/type=segment/"
    fs = pafs.S3FileSystem(anonymous=True, region="us-west-2")
    d = ds.dataset(path, filesystem=fs, format="parquet")
    flt = ((pc.field("bbox", "xmin") < E) & (pc.field("bbox", "xmax") > W)
           & (pc.field("bbox", "ymin") < N) & (pc.field("bbox", "ymax") > S))
    rows = d.to_table(filter=flt, columns=["id", "names", "subtype", "class", "road_flags",
                                           "level_rules", "width_rules", "geometry"]).to_pylist()
    feats = []
    skipped_loops = 0
    skipped_unnamed = 0
    for r in rows:
        whole = [rf for rf in (r.get("road_flags") or []) if not rf.get("between")]
        part = [rf for rf in (r.get("road_flags") or []) if rf.get("between")]
        flags_whole = {v for rf in whole for v in (rf.get("values") or [])}
        if flags_whole & {"is_tunnel", "is_indoor"}:
            continue
        levels_whole = [lr.get("value") or 0 for lr in (r.get("level_rules") or []) if not lr.get("between")]
        geom = wkb.loads(r["geometry"])
        pieces = []
        if "is_bridge" in flags_whole or any(v >= 1 for v in levels_whole):
            pieces.append(geom)
        else:
            # Linear-referenced flags/levels: keep just the elevated stretch.
            from shapely.ops import substring
            for rule in part + [lr for lr in (r.get("level_rules") or []) if lr.get("between")]:
                elevated = ("is_bridge" in (rule.get("values") or [])) or ((rule.get("value") or 0) >= 1)
                if elevated and not ({"is_tunnel", "is_indoor"} & set(rule.get("values") or [])):
                    b0, b1 = rule["between"]
                    pieces.append(substring(geom, b0, b1, normalized=True))
        if not pieces:
            continue
        width = None
        for wr in (r.get("width_rules") or []):
            if wr.get("value"):
                width = float(wr["value"])
        cls = r.get("class") or ""
        width = width or WIDTH_BY_CLASS.get(cls, DEFAULT_WIDTH)
        name = (r.get("names") or {}).get("primary")
        if cls in FOOT_CLASSES | {"service"} and not name:
            # Unnamed elevated walkways and service roads are overwhelmingly
            # rooftop parking ramps, podium terraces and in-building skyways —
            # carving them out would cut real buildings. Named ones (the
            # pedestrian bridges over West St, bridge bike paths) are kept.
            skipped_unnamed += 1
            continue
        for g in pieces:
            if g.is_empty or g.geom_type != "LineString":
                continue
            if cls in FOOT_CLASSES and g.is_closed:
                # A walkway that loops back on itself (rooftop tracks, terrace
                # rings mapped as "bridge") is not a deck over a street.
                skipped_loops += 1
                continue
            feats.append({"type": "Feature",
                          "properties": {"id": r["id"], "name": name, "subtype": r["subtype"], "class": cls,
                                         "width_m": width},
                          "geometry": {"type": "LineString", "coordinates": [list(c[:2]) for c in g.coords]}})
    doc = {"type": "FeatureCollection",
           "source": {"dataset": "Overture Maps — transportation/segment", "release": a.release,
                      "url": f"s3://{path}", "license": "ODbL (OpenStreetMap contributors) via Overture Maps Foundation",
                      "bbox_wgs84": a.bbox, "segments_scanned": len(rows), "fetched_utc": start,
                      "skipped_closed_footway_loops": skipped_loops,
                      "skipped_unnamed_footway_or_service": skipped_unnamed,
                      "rule": "is_bridge or level>=1 (whole or linear-referenced), excluding tunnels/indoor "
                              "and unnamed footway/service segments"},
           "features": feats}
    with open(a.out, "w") as f:
        json.dump(doc, f)
    print(f"[overture] {len(rows)} segments scanned, {len(feats)} elevated lines -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Street-scale flood run over Manhattan on real USGS terrain, with official
building footprints as no-flow walls, driven by the documented Hurricane Ida
(2021) hyetograph. Writes a run folder the dashboard and the holo table read.

    python3 examples/nyc_street_run.py --dem USGS_13_n41w074.tif \
        --buildings nyc_building_footprints.gpkg --out viz/nyc_street_run

Without --buildings the run still executes on the real terrain, but it is
labelled TERRAIN ONLY in the manifest's area name and in run_notes.json, so a
rehearsal can never be mistaken for the building-aware result.

Design, all drawn from the engine's own validated pieces:
  * terrain    DEMSource over the 1/3-arc-second tile, clipped to --bbox,
               resampled to --dx (default 10 m, the engine's threshold for
               turning footprints into closed cells; see
               ingestion.buildings.CLOSED_CELL_MAX_DX_M)
  * buildings  BuildingsSource -> apply_buildings: cells with footprint
               coverage >= 0.5 become walls of the tallest building's height;
               export_buildings_json writes the viewer's extruded-prism layer
  * storm      validation.ida2021.IDA_HYETOGRAPH (20 -> 80 -> 40 -> 20 mm/h,
               the 80 is the Central Park record hour) with NYC DEP's 44 mm/h
               drainage design capacity, surcharged to half (blockage 0.5, the
               middle case of the validation matrix)
  * sensors    the documented Ida-flooded stations that fall inside --bbox,
               each probed at the lowest cell within the coordinate uncertainty
               radius exactly as the validation does, plus auto-placed low points
               restricted to dry land (z >= --min-node-z), because the lowest
               cells of a coastal grid are the rivers themselves

Coverage matters: the solver treats nodata cells as walls, so an AOI that runs
past the edge of the supplied tile(s) grows a spurious dam along the seam (the
Hudson strip west of -74.0006 is outside USGS_13_n41w074; pass n41w075 too).
The script prints the tile bounds and the masked fraction and records both.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from aqua_sim.config import SimConfig, SolverConfig, StormConfig            # noqa: E402
from aqua_sim.ingestion.dem import DEMSource                                # noqa: E402
from aqua_sim.risk.sink_nodes import SinkNode                               # noqa: E402
from aqua_sim.scenario import Scenario, run_scenario                        # noqa: E402
from aqua_sim.validation.ida2021 import (IDA_DURATION_HOURS, IDA_FLOODED_STATIONS,  # noqa: E402
                                         IDA_HYETOGRAPH, NYC_DRAINAGE_CAPACITY_MM_HR,
                                         VENT_LIP_M, _local_low, _lonlat_to_index)

DEFAULT_BBOX = (-74.025, 40.695, -73.955, 40.775)   # Lower Manhattan to Midtown


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tile_coverage(paths):
    """WGS84 bounds of each DEM tile, so the notes show what the AOI did and did not cover."""
    import rasterio
    from rasterio.warp import transform_bounds
    out = []
    for p in paths:
        with rasterio.open(p) as src:
            b = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        out.append({"path": p, "sha256": sha256(p), "wgs84_bounds": [round(v, 6) for v in b]})
    return out


def land_low_points(grid, z, mask, count, min_z):
    """Lowest cells at or above ``min_z``, spaced like scenario._auto_sink_nodes.

    The engine's own helper picks the lowest in-AOI cells, which in a coastal
    metro are the rivers. These are illustrative low points, not real assets."""
    ys, xs = np.nonzero(mask & (z >= min_z))
    order = np.argsort(z[ys, xs], kind="stable")
    spacing = max(grid.nx, grid.ny) // 6
    nodes, used = [], []
    for k in order:
        x, y = int(xs[k]), int(ys[k])
        if all(abs(x - ux) + abs(y - uy) > spacing for ux, uy in used):
            used.append((x, y))
            nodes.append(SinkNode(f"Low Point {len(nodes) + 1}", x=x, y=y,
                                  threshold_elevation=float(z[y, x]) + 0.2,
                                  opening_area_m2=4.0, capacity_m3=3000.0))
        if len(nodes) >= count:
            break
    return nodes


def land_peak_depth(run_dir, manifest, z, mask, min_z):
    """Max depth over the run restricted to land cells (z >= min_z), with its cell.

    The manifest's peak_depth_m is over every active cell, which on a coastal
    grid is dominated by rain pooling on the river surface or in DEM pits."""
    best = None
    land = mask & (z >= min_z)
    for f in manifest["frames"]:
        depth = np.asarray(json.load(open(os.path.join(run_dir, f["file"])))["depth"], dtype=float)
        best = depth if best is None else np.maximum(best, depth)
    d = np.where(land, best, 0.0)
    iy, ix = np.unravel_index(int(np.argmax(d)), d.shape)
    return {"min_z_m": min_z, "max_depth_m": round(float(d[iy, ix]), 4), "cell": [int(ix), int(iy)],
            "z_m": round(float(z[iy, ix]), 2),
            "cells_over_0p5_m": int((d > 0.5).sum()), "cells_over_0p1_m": int((d > 0.1).sum()),
            "all_cells_max_depth_m": manifest["peak_depth_m"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dem", required=True, action="append", help="USGS 3DEP GeoTIFF tile(s); repeat to mosaic")
    ap.add_argument("--buildings", default=None, help="official footprint file (GPKG/SHP/GeoJSON); omit for terrain-only")
    ap.add_argument("--buildings-url", default=None, help="where the footprint file was actually fetched from (recorded as provenance)")
    ap.add_argument("--lidar-buildings", default=None,
                    help="LiDAR building massing from examples/build_lidar_buildings.py (GeoJSON[.gz]); "
                         "alternative to --buildings")
    ap.add_argument("--target-crs", default=None, help="grid CRS (default: UTM zone of the AOI); "
                    "EPSG:26918 keeps USGS 1 m tiles in their native CRS")
    ap.add_argument("--resampling", default="bilinear", help="DEM resampling; 'average' when coarsening 1 m terrain")
    ap.add_argument("--max-cells", type=int, default=4_000_000)
    ap.add_argument("--area-name", default="Manhattan, Lower to Midtown")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"), default=DEFAULT_BBOX)
    ap.add_argument("--dx", type=float, default=10.0)
    ap.add_argument("--hours", type=float, default=6.0, help="simulated hours (rain lasts 4 h; the rest is drain-down)")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--blockage", type=float, default=0.5, help="drainage blockage 0..1 (validation matrix: 0, 0.5, 1)")
    ap.add_argument("--auto-nodes", type=int, default=4)
    ap.add_argument("--min-node-z", type=float, default=1.0, help="auto low points only on cells at or above this elevation (m)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    t0 = time.time()
    bbox = tuple(a.bbox)
    tiles = tile_coverage(a.dem)
    for t in tiles:
        print(f"[nyc] tile {os.path.basename(t['path'])} covers lon {t['wgs84_bounds'][0]:.4f}..{t['wgs84_bounds'][2]:.4f} "
              f"lat {t['wgs84_bounds'][1]:.4f}..{t['wgs84_bounds'][3]:.4f}", flush=True)
    if a.buildings and a.lidar_buildings:
        ap.error("pass --buildings or --lidar-buildings, not both")
    grid = DEMSource(a.dem, target_dx_m=a.dx, aoi_bounds=bbox, max_cells=a.max_cells,
                     target_crs=a.target_crs, resampling=a.resampling).load()
    mask = np.asarray(grid.mask, dtype=bool)
    z = np.asarray(grid.z, dtype=float)
    covered = float(mask.mean())
    print(f"[nyc] grid {grid.nx}x{grid.ny} = {grid.nx * grid.ny / 1e6:.2f}M cells @ {grid.dx:g} m [{grid.crs}] "
          f"covered {covered * 100:.1f}%  z {z[mask].min():.1f}..{z[mask].max():.1f} m  "
          f"below-sea-level cells {int((mask & (z < 0)).sum())}  ingest {time.time() - t0:.1f}s", flush=True)
    if covered < 0.99:
        print(f"[nyc] WARNING: {100 - covered * 100:.1f}% of the AOI has no elevation data and will act as a wall; "
              f"supply the neighbouring tile(s)", flush=True)

    buildings_summary = None
    collection = None
    if a.buildings:
        from aqua_sim.ingestion.buildings import BuildingsSource, apply_buildings
        tb = time.time()
        collection = BuildingsSource(a.buildings, transport_url=a.buildings_url).load_for_grid(grid, aoi_bounds=bbox)
    elif a.lidar_buildings:
        from aqua_sim.ingestion.buildings import apply_buildings
        from aqua_sim.ingestion.lidar import read_geojson
        tb = time.time()
        collection = read_geojson(a.lidar_buildings, grid)
    if collection is not None:
        buildings_summary = apply_buildings(grid, collection)
        print(f"[nyc] buildings: {buildings_summary['buildings']} footprints, mode {buildings_summary['mode']}, "
              f"{buildings_summary['closed_cells']} closed cells, mean coverage {buildings_summary['mean_coverage']:.3f} "
              f"({time.time() - tb:.1f}s)", flush=True)
        if buildings_summary["closed_cells"] == 0 and grid.dx <= 10.0:
            print("[nyc] WARNING: no closed cells — footprints did not intersect the grid or were empty", flush=True)

    # Sensors: documented Ida stations inside the box, probed like the validation does.
    radius_cells = max(1, int(round(100.0 / grid.dx)))
    nodes = []
    for name, lat, lon in IDA_FLOODED_STATIONS:
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        x0, y0 = _lonlat_to_index(grid, lon, lat)
        x, y = _local_low(grid, x0, y0, radius_cells)
        nodes.append(SinkNode(name=name, x=x, y=y, threshold_elevation=grid.z[y][x] + VENT_LIP_M,
                              opening_area_m2=3.0, capacity_m3=2000.0))
    for n in land_low_points(grid, z, mask, count=a.auto_nodes, min_z=a.min_node_z):
        if all(abs(n.x - m.x) + abs(n.y - m.y) > 3 for m in nodes):
            nodes.append(n)
    for n in nodes:
        print(f"[nyc] sensor {n.name!r} at cell ({n.x},{n.y}) z={grid.z[n.y][n.x]:.2f} m", flush=True)

    storm = StormConfig(rainfall_mm_per_hr=80.0, duration_hours=IDA_DURATION_HOURS,
                        drainage_capacity_mm_per_hr=NYC_DRAINAGE_CAPACITY_MM_HR,
                        drainage_blockage=a.blockage, hyetograph=IDA_HYETOGRAPH)
    total_s = a.hours * 3600.0
    scale = "street" if grid.dx <= 10.0 else "screening"
    if buildings_summary and buildings_summary["closed_cells"]:
        bld_label = ("USGS LiDAR building massing as no-flow walls" if a.lidar_buildings
                     else "official NYC footprints as no-flow walls")
    else:
        bld_label = "TERRAIN ONLY — no buildings"
    config = SimConfig(storm=storm,
                       solver=SolverConfig(cfl=0.7, total_time_s=total_s, output_interval_s=total_s / a.frames),
                       aoi_name=f"{a.area_name} — {scale} resolution {grid.dx:g} m — Ida 2021 hyetograph — {bld_label}")
    sc = Scenario(grid=grid, config=config, nodes=nodes)

    print(f"[nyc] solving {a.hours:g} h, {a.frames} frames, {len(nodes)} sensors …", flush=True)
    ts = time.time()
    manifest = run_scenario(sc, a.out)
    solve_s = time.time() - ts

    land_peak = land_peak_depth(a.out, manifest, z, mask, min_z=a.min_node_z)

    if collection is not None:
        from aqua_sim.ingestion.buildings import export_buildings_json, make_presets
        presets = make_presets(grid, [("Times Square", -73.9870, 40.7553, 900.0),
                                      ("Lower Manhattan", -74.0110, 40.7075, 1200.0),
                                      ("Wall St canyon", -74.0090, 40.7060, 450.0),
                                      ("World Trade Center", -74.0130, 40.7115, 600.0),
                                      ("The Battery", -74.0165, 40.7033, 700.0)])
        export_buildings_json(collection, grid, a.out, presets=presets)

    notes = {
        "purpose": "street-scale Manhattan run on real USGS 3DEP terrain with the Ida 2021 hyetograph",
        "buildings": buildings_summary or {"mode": "NONE — TERRAIN ONLY", "note": "no footprint file was supplied; this run is a pipeline rehearsal, not the building-aware result"},
        "buildings_file": a.buildings or a.lidar_buildings,
        "buildings_sha256": sha256(a.buildings or a.lidar_buildings) if (a.buildings or a.lidar_buildings) else None,
        "buildings_kind": "lidar_massing" if a.lidar_buildings else ("footprints" if a.buildings else None),
        "dem_resampling": a.resampling,
        "buildings_transport_url": a.buildings_url,
        "dem": [{"path": p, "sha256": sha256(p)} for p in a.dem],
        "dem_tiles": tiles,
        "coverage": {"fraction_with_elevation": round(covered, 4),
                     "note": "cells without elevation data are masked and act as walls in the solver",
                     "below_sea_level_cells": int((mask & (z < 0)).sum()),
                     "land_cells_z_ge_min_node_z": int((mask & (z >= a.min_node_z)).sum())},
        "bbox_wgs84": list(bbox), "dx_m": grid.dx, "grid": [grid.nx, grid.ny], "crs": grid.crs,
        "transform": list(grid.transform) if grid.transform else None,
        "storm": {"hyetograph_hr_mmph": IDA_HYETOGRAPH, "duration_h": IDA_DURATION_HOURS,
                  "drainage_capacity_mm_hr": NYC_DRAINAGE_CAPACITY_MM_HR, "blockage": a.blockage},
        "sensors": [{"name": n.name, "x": n.x, "y": n.y} for n in nodes],
        "sim_hours": a.hours, "frames": a.frames,
        "run_id": manifest["run_id"], "peak_depth_m": manifest["peak_depth_m"],
        "peak_depth_land": land_peak,
        "wall_seconds_solve": round(solve_s, 1), "wall_seconds_total": round(time.time() - t0, 1),
    }
    json.dump(notes, open(os.path.join(a.out, "run_notes.json"), "w"), indent=2)
    print(f"[nyc] DONE run_id {manifest['run_id']}  peak {manifest['peak_depth_m']} m (all cells)  "
          f"land peak {land_peak['max_depth_m']} m at z={land_peak['z_m']} m  frames {manifest['frame_count']}  "
          f"solve {solve_s / 60:.1f} min -> {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

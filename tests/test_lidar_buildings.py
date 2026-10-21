"""LiDAR building massing: detection, nested levels, physics hand-off."""

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("rasterio")

from aqua_sim.grid import Grid  # noqa: E402
from aqua_sim.ingestion.buildings import apply_buildings  # noqa: E402
from aqua_sim.ingestion.lidar import (ReturnRasters, building_parts, detect_buildings,  # noqa: E402
                                      grid_for_bounds, read_geojson, write_geojson)
from aqua_sim.ingestion.buildings import BuildingCollection  # noqa: E402

LEFT, TOP = 500000.0, 4500000.0
TF = (1.0, 0.0, LEFT, 0.0, -1.0, TOP)


def scene():
    """80x80 m at 1 m: a 24x24 m podium at 30 m with an 8x8 m tower at 60 m,
    a leafy 10x10 m tree at 12 m, a 2x4 m truck at 3 m and a 1 m wide crane
    boom at 50 m. Ground is flat at 5 m NAVD88."""
    n = 80
    dtm = np.full((n, n), 5.0, dtype=np.float32)
    z = np.full((n, n), np.nan, dtype=np.float32)
    count = np.zeros((n, n), dtype=np.int32)
    multi = np.zeros((n, n), dtype=np.int32)

    def put(r0, r1, c0, c1, h, ratio):
        z[r0:r1, c0:c1] = 5.0 + h
        count[r0:r1, c0:c1] = 10
        multi[r0:r1, c0:c1] = int(round(10 * ratio))

    put(8, 32, 8, 32, 30.0, 0.0)        # podium (aligned to 4 m cells)
    put(16, 24, 16, 24, 60.0, 0.0)      # tower on top of it
    put(50, 60, 50, 60, 12.0, 0.6)      # tree canopy
    put(65, 67, 10, 14, 3.0, 0.0)       # parked truck (8 m^2)
    put(45, 46, 5, 45, 50.0, 0.0)       # crane boom, 1 cell wide
    rr = ReturnRasters(zmax=z, count=count, multi=multi, transform=TF, crs="EPSG:26918")
    return rr, dtm


def test_detection_keeps_building_rejects_tree_truck_crane():
    rr, dtm = scene()
    br = detect_buildings(rr, dtm)
    assert br.stats["regions"] == 1
    assert br.mask[8:32, 8:32].all()             # whole podium
    assert not br.mask[50:60, 50:60].any()       # canopy (multi-return)
    assert not br.mask[65:67, 10:14].any()       # truck (below min area)
    assert not br.mask[45, 36:45].any()          # crane boom (opened away)
    assert br.ndsm[22, 22] == pytest.approx(60.0)
    assert br.ndsm[10, 10] == pytest.approx(30.0)
    assert br.ndsm[8, 8] == pytest.approx(30.0)          # podium corner keeps its height
    assert br.ndsm[16, 16] == pytest.approx(60.0)        # tower corner keeps its height


def test_nested_levels_reproduce_setback():
    rr, dtm = scene()
    br = detect_buildings(rr, dtm)
    parts = building_parts(br, TF, band_m=3.0)
    assert len(parts) == 2
    low, high = sorted(parts, key=lambda b: b.height_m)

    def area(b):
        tot = 0.0
        for poly in b.polygons:
            for k, ring in enumerate(poly):
                s = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))
                tot += abs(s) / 2 * (1 if k == 0 else -1)
        return tot

    # Level 0 is the full podium footprint (the tower sits inside it, no hole).
    assert area(low) == pytest.approx(24 * 24)
    assert low.height_m == pytest.approx(30.0)
    assert area(high) == pytest.approx(8 * 8)
    assert high.height_m == pytest.approx(60.0)
    # Polygons are in grid CRS meters at the right place.
    xs = [x for poly in high.polygons for x, _ in poly[0]]
    assert min(xs) == pytest.approx(LEFT + 16) and max(xs) == pytest.approx(LEFT + 24)


def test_parts_become_walls_with_tower_height(tmp_path):
    rr, dtm = scene()
    parts = building_parts(detect_buildings(rr, dtm), TF)
    coll = BuildingCollection(buildings=parts, crs="EPSG:26918", provenance={"name": "synthetic"})
    path = str(tmp_path / "b.geojson.gz")
    write_geojson(coll, path)

    grid = Grid.empty(20, 20, 4.0)
    grid.crs = "EPSG:26918"
    grid.transform = (4.0, 0.0, LEFT, 0.0, -4.0, TOP)
    back = read_geojson(path, grid)
    assert len(back) == 2
    summary = apply_buildings(grid, back)
    obs = np.asarray(grid.obstacle)
    assert summary["closed_cells"] == 36                 # 24 m podium / 4 m cells = 6x6
    assert obs[5, 5] == pytest.approx(60.0)              # tower cells carry the tower height
    assert obs[3, 3] == pytest.approx(30.0)              # podium cells the podium height
    assert obs[15, 15] == 0.0


def test_grid_snaps_to_dem_origin():
    tf, shape = grid_for_bounds((100.4, 200.2, 110.6, 210.9), 1.0, snap_origin=(0.25, 0.75))
    assert tf[2] == pytest.approx(100.25) and tf[5] == pytest.approx(210.75 + 1.0)
    assert shape == (12, 11)    # rows 199.75..211.75, cols 100.25..111.25


def test_rasterize_returns_bins_max_and_pulse_counts(tmp_path):
    laspy = pytest.importorskip("laspy")
    from aqua_sim.ingestion.lidar import rasterize_returns

    hdr = laspy.LasHeader(point_format=3, version="1.2")
    hdr.scales = [0.01, 0.01, 0.01]
    hdr.offsets = [LEFT, TOP - 10, 0]
    las = laspy.LasData(hdr)
    las.x = np.array([LEFT + 0.5, LEFT + 0.6, LEFT + 1.5, LEFT + 1.5])
    las.y = np.array([TOP - 0.5, TOP - 0.4, TOP - 0.5, TOP - 0.5])
    las.z = np.array([10.0, 12.0, 7.0, 3.0])
    las.classification = np.array([1, 1, 1, 2], dtype=np.uint8)   # last is ground: ignored
    las.return_number = np.array([1, 1, 1, 2], dtype=np.uint8)
    las.number_of_returns = np.array([1, 1, 2, 2], dtype=np.uint8)
    p = str(tmp_path / "t.laz")
    las.write(p)
    rr = rasterize_returns([p], TF, (2, 2), src_crs="EPSG:26918", dst_crs="EPSG:26918")
    assert rr.zmax[0, 0] == pytest.approx(12.0)
    assert rr.zmax[0, 1] == pytest.approx(7.0)
    assert np.isnan(rr.zmax[1, 1])
    assert rr.count[0, 0] == 2 and rr.multi[0, 0] == 0
    assert rr.count[0, 1] == 1 and rr.multi[0, 1] == 1


def test_bridge_deck_and_superstructure_are_not_walls():
    """A classified deck with an unclassified truss above it, crossing the
    scene and touching the podium: no part of it may become a wall."""
    rr, dtm = scene()
    bridge = np.zeros_like(rr.count)
    bridge[70:76, :] = 5                       # 6 m wide deck across the scene
    rr.zmax[70:76, :] = 5.0 + 20.0             # truss returns above it (class 1)
    rr.count[70:76, :] = 10
    rr.bridge = bridge
    br = detect_buildings(rr, dtm)
    assert not br.mask[67:79, :].any()
    assert br.mask[8:32, 8:32].all()           # the real building is untouched
    assert br.stats["bridge_cells"] == 6 * 80

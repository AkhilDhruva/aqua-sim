"""BuildingsSource physics-integration tests: CRS, feet->m, rasterization,
clipping, provenance, and the flow-routing guarantee at fine resolution."""

import json

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
fiona = pytest.importorskip("fiona")

from rasterio.transform import from_bounds

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.grid import Grid
from aqua_sim.ingestion.buildings import (BuildingsSource, apply_buildings,
                                          export_buildings_json,
                                          rasterize_coverage)
from aqua_sim.ingestion.dem import DEMSource
from aqua_sim.physics import BoundaryType
from aqua_sim.physics.swe_numpy import NumpyShallowWaterSolver

BBOX = (-74.02, 40.70, -73.99, 40.72)  # small lower-Manhattan-ish box


@pytest.fixture
def flat_grid(tmp_path):
    """A flat 8 m grid over BBOX via DEMSource (georeferenced, EPSG:32618)."""
    w = h = 80
    z = np.full((h, w), 5.0, np.float32)
    dem = str(tmp_path / "d.tif")
    with rasterio.open(dem, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_bounds(*BBOX, w, h), nodata=-9999.0) as dst:
        dst.write(z, 1)
    return DEMSource(dem, target_dx_m=8.0, max_cells=400_000).load()


def _write_official_style_gpkg(path, grid, rect_m, heightroof_ft=100.0,
                               groundelev_ft=16.0):
    """A GPKG in EPSG:2263 (US survey ft) mimicking the official NYC schema.

    ``rect_m`` is (x0, y0, x1, y1) in the GRID's CRS meters; we reproject it
    into 2263 so the test exercises the real ft-CRS -> m-CRS path.
    """
    from rasterio.warp import transform as warp_transform

    x0, y0, x1, y1 = rect_m
    xs, ys = warp_transform(grid.crs, "EPSG:2263", [x0, x1], [y0, y1])
    schema = {"geometry": "Polygon",
              "properties": {"BIN": "str", "DOITT_ID": "str",
                             "HEIGHTROOF": "float", "GROUNDELEV": "float",
                             "CNSTRCT_YR": "int"}}
    with fiona.open(path, "w", driver="GPKG", crs="EPSG:2263",
                    schema=schema, layer="buildings") as dst:
        dst.write({
            "geometry": {"type": "Polygon", "coordinates": [[
                (xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]),
                (xs[0], ys[1]), (xs[0], ys[0])]]},
            "properties": {"BIN": "1000001", "DOITT_ID": "42",
                           "HEIGHTROOF": heightroof_ft,
                           "GROUNDELEV": groundelev_ft, "CNSTRCT_YR": 1931},
        })
    return path


def _rect_in_grid(grid, i0, j0, i1, j1):
    """Cell-index rect -> grid-CRS meter rect."""
    a, _, left, _, e, top = grid.transform
    return (left + i0 * a, top + j1 * e, left + i1 * a, top + j0 * e)


def test_feet_to_meters_and_schema(tmp_path, flat_grid):
    rect = _rect_in_grid(flat_grid, 10, 10, 20, 20)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b.gpkg"), flat_grid, rect,
                                      heightroof_ft=100.0, groundelev_ft=16.0)
    coll = BuildingsSource(gpkg).load_for_grid(flat_grid)
    assert len(coll) == 1
    b = coll.buildings[0]
    assert b.height_m == pytest.approx(100.0 * 0.3048)      # 30.48 m
    assert b.ground_m == pytest.approx(16.0 * 0.3048)
    assert b.bin == "1000001" and b.year == 1931
    # Geometry landed in the grid CRS (meters): ring coords within grid extent.
    a, _, left, _, e, top = flat_grid.transform
    xs = [p[0] for p in b.polygons[0][0]]
    assert left <= min(xs) and max(xs) <= left + flat_grid.nx * a


def test_coverage_fraction_and_closed_cells(tmp_path, flat_grid):
    # A rectangle exactly covering cells 10..19 x 10..19 (fully covered cells).
    rect = _rect_in_grid(flat_grid, 10, 10, 20, 20)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b.gpkg"), flat_grid, rect)
    coll = BuildingsSource(gpkg).load_for_grid(flat_grid)
    cov = rasterize_coverage(coll, flat_grid)
    assert cov[15][15] > 0.9          # interior cell ~fully covered
    assert cov[5][5] == 0.0           # far cell untouched
    summary = apply_buildings(flat_grid, coll)
    assert summary["mode"] == "closed_cells"
    assert summary["closed_cells"] > 0
    assert flat_grid.obstacle[15][15] == pytest.approx(30.48)
    assert flat_grid.obstacle[5][5] == 0.0
    assert flat_grid.coverage is not None


def test_coarse_grid_stays_presentation_only(tmp_path):
    # Same building, 30 m grid: coverage exported, NO obstacle cells.
    w = h = 40
    z = np.full((h, w), 5.0, np.float32)
    dem_path_dir = tmp_path
    dem = str(dem_path_dir / "d30.tif")
    with rasterio.open(dem, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_bounds(*BBOX, w, h), nodata=-9999.0) as dst:
        dst.write(z, 1)
    grid = DEMSource(dem, target_dx_m=30.0).load()
    rect = _rect_in_grid(grid, 5, 5, 8, 8)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b30.gpkg"), grid, rect)
    coll = BuildingsSource(gpkg).load_for_grid(grid)
    summary = apply_buildings(grid, coll)
    assert "coverage_only" in summary["mode"]
    assert summary["closed_cells"] == 0
    assert max(v for row in grid.obstacle for v in row) == 0.0
    assert max(v for row in grid.coverage for v in row) > 0.5


def test_aoi_clip_excludes_outside_buildings(tmp_path, flat_grid):
    inside = _rect_in_grid(flat_grid, 10, 10, 14, 14)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b.gpkg"), flat_grid, inside)
    # Clip to a *far corner* AOI that excludes the building.
    from rasterio.warp import transform_bounds
    a, _, left, _, e, top = flat_grid.transform
    corner = transform_bounds(flat_grid.crs, "EPSG:4326",
                              left + 60 * a, top + 79 * e, left + 79 * a, top + 60 * e)
    coll = BuildingsSource(gpkg).load_for_grid(flat_grid, aoi_bounds=corner)
    assert len(coll) == 0
    assert coll.provenance["features_in_aoi"] == 0
    assert coll.provenance["features_in_source"] == 1


def test_provenance_fields(tmp_path, flat_grid):
    rect = _rect_in_grid(flat_grid, 10, 10, 20, 20)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b.gpkg"), flat_grid, rect)
    coll = BuildingsSource(gpkg, transport_url="https://example.org/mirror.gpkg"
                           ).load_for_grid(flat_grid)
    p = coll.provenance
    assert p["dataset_id"] == "nqwf-w8eh"
    assert p["official_url"].startswith("https://data.cityofnewyork.us/")
    assert p["transport_url"] == "https://example.org/mirror.gpkg"
    assert len(p["source_sha256"]) == 64
    assert p["grid_crs"] == flat_grid.crs
    assert "license" in p and "vertical_datum" in p
    apply_buildings(flat_grid, coll)
    assert flat_grid.meta["buildings"]["dataset_id"] == "nqwf-w8eh"


def test_dam_break_is_blocked_and_routed_by_footprint(tmp_path, flat_grid):
    """The physics guarantee: at fine dx a footprint blocks flow through
    itself and water routes around it — verified with an actual dam break."""
    grid = flat_grid
    # A wall-like building spanning most of the domain width, with gaps at the
    # edges: rows 30..40, columns 10..69 of an 80-wide grid.
    rect = _rect_in_grid(grid, 10, 30, 70, 40)
    gpkg = _write_official_style_gpkg(str(tmp_path / "wall.gpkg"), grid, rect,
                                      heightroof_ft=200.0)
    coll = BuildingsSource(gpkg).load_for_grid(grid)
    apply_buildings(grid, coll)
    assert grid.obstacle[35][40] > 0.0          # building interior is walled

    cfg = SimConfig(storm=StormConfig(rainfall_mm_per_hr=0.0,
                                      drainage_capacity_mm_per_hr=0.0),
                    solver=SolverConfig(total_time_s=900.0, output_interval_s=300.0))
    solver = NumpyShallowWaterSolver(grid, cfg, boundary=BoundaryType.CLOSED)
    init = [[0.0] * grid.nx for _ in range(grid.ny)]
    for j in range(5, 25):
        for i in range(20, 60):
            init[j][i] = 1.0                    # reservoir north of the building
    states = list(solver.run(initial_depth=init))
    last = np.asarray(states[-1].depth)

    assert float(last[35, 40]) == 0.0           # never water inside the footprint
    south = float(last[45:, :].sum())
    assert south > 0.0                          # water reached the far side...
    edge_flow = float(last[30:41, :10].sum() + last[30:41, 70:].sum())
    assert edge_flow > 0.0                      # ...by routing around the gaps
    v0, v1 = states[0].total_volume_m3, states[-1].total_volume_m3
    assert abs(v1 - v0) / v0 < 1e-9             # and mass stayed conserved


def test_multipolygon_parts_stay_separate(tmp_path, flat_grid):
    # A MultiPolygon with two disjoint parts must NOT fold part 2's outer ring
    # into part 1 as a hole — both parts should be solid coverage.
    r1 = _rect_in_grid(flat_grid, 10, 10, 14, 14)
    r2 = _rect_in_grid(flat_grid, 30, 30, 34, 34)
    from rasterio.warp import transform as warp_transform

    def ring(rect):
        x0, y0, x1, y1 = rect
        xs, ys = warp_transform(flat_grid.crs, "EPSG:2263", [x0, x1], [y0, y1])
        return [(xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1]), (xs[0], ys[0])]

    schema = {"geometry": "MultiPolygon",
              "properties": {"HEIGHTROOF": "float", "DOITT_ID": "str"}}
    path = str(tmp_path / "mp.gpkg")
    with fiona.open(path, "w", driver="GPKG", crs="EPSG:2263", schema=schema,
                    layer="buildings") as dst:
        dst.write({"geometry": {"type": "MultiPolygon",
                                "coordinates": [[ring(r1)], [ring(r2)]]},
                   "properties": {"HEIGHTROOF": 100.0, "DOITT_ID": "7"}})
    coll = BuildingsSource(path).load_for_grid(flat_grid)
    assert len(coll) == 1
    assert len(coll.buildings[0].polygons) == 2      # two parts preserved
    cov = rasterize_coverage(coll, flat_grid)
    assert cov[12][12] > 0.9 and cov[32][32] > 0.9   # BOTH parts covered
    assert cov[22][22] == 0.0                        # gap between them is open


def test_buildings_json_export(tmp_path, flat_grid):
    rect = _rect_in_grid(flat_grid, 10, 10, 20, 20)
    gpkg = _write_official_style_gpkg(str(tmp_path / "b.gpkg"), flat_grid, rect)
    coll = BuildingsSource(gpkg).load_for_grid(flat_grid)
    doc = export_buildings_json(coll, flat_grid, str(tmp_path / "run"), tile_m=200.0)
    assert doc["building_count"] == 1 and doc["tile_count"] == 1
    b = doc["tiles"][0]["buildings"][0]
    assert b["h"] == pytest.approx(30.48, abs=0.01)
    assert b["base"] == pytest.approx(5.0, abs=0.5)     # DEM base, not GROUNDELEV
    i0, j0, i1, j1 = b["cells"]
    assert 0 <= i0 <= i1 < flat_grid.nx and 0 <= j0 <= j1 < flat_grid.ny
    # Scene-local coords: within [0, W]x[0, H]
    outer = b["polys"][0][0]
    xs = [p[0] for p in outer]; ys = [p[1] for p in outer]
    assert 0 <= min(xs) and max(xs) <= flat_grid.nx * flat_grid.dx
    assert 0 <= min(ys) and max(ys) <= flat_grid.ny * flat_grid.dx
    saved = json.load(open(tmp_path / "run" / "buildings.json"))
    assert saved["provenance"]["dataset_id"] == "nqwf-w8eh"
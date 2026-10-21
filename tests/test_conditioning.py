"""Conditioning-engine tests: surface classes, barrier crests, inlets, culverts,
CRS/feet normalization, and provenance — exercised with synthetic layers in the
official NYC schema (EPSG:2263, feet) since the real hosts are unreachable."""

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
fiona = pytest.importorskip("fiona")

from rasterio.transform import from_bounds
from rasterio.warp import transform as warp_transform

from aqua_sim.ingestion.conditioning import (FeatureLayer, add_culverts,
                                             add_drainage_inlets, burn_barriers,
                                             burn_surface_classes, NYC_SOURCES,
                                             FT_TO_M)
from aqua_sim.ingestion.dem import DEMSource

BBOX = (-74.01, 40.71, -73.99, 40.725)


@pytest.fixture
def grid(tmp_path):
    w = h = 60
    z = np.full((h, w), 3.0, np.float32)
    dem = str(tmp_path / "d.tif")
    with rasterio.open(dem, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_bounds(*BBOX, w, h), nodata=-9999.0) as dst:
        dst.write(z, 1)
    return DEMSource(dem, target_dx_m=8.0, max_cells=400_000).load()


def _rect_2263(grid, i0, j0, i1, j1):
    a, _, left, _, e, top = grid.transform
    xm = [left + i0 * a, left + i1 * a]; ym = [top + j1 * e, top + j0 * e]
    xs, ys = warp_transform(grid.crs, "EPSG:2263", xm, ym)
    return [(xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1]), (xs[0], ys[0])]


def _poly_gpkg(path, grid, rect_cells, props=None, height_key=None):
    schema = {"geometry": "Polygon",
              "properties": {"CLASS": "str", **({height_key: "float"} if height_key else {})}}
    with fiona.open(path, "w", driver="GPKG", crs="EPSG:2263", schema=schema,
                    layer="l") as dst:
        dst.write({"geometry": {"type": "Polygon",
                                "coordinates": [_rect_2263(grid, *rect_cells)]},
                   "properties": props or {"CLASS": "x"}})
    return path


def _line_gpkg(path, grid, cell_pts, height_key=None, height_val=None):
    a, _, left, _, e, top = grid.transform
    xm = [left + (i + 0.5) * a for i, j in cell_pts]
    ym = [top + (j + 0.5) * e for i, j in cell_pts]
    xs, ys = warp_transform(grid.crs, "EPSG:2263", xm, ym)
    schema = {"geometry": "LineString",
              "properties": {height_key: "float"} if height_key else {}}
    with fiona.open(path, "w", driver="GPKG", crs="EPSG:2263", schema=schema,
                    layer="l") as dst:
        dst.write({"geometry": {"type": "LineString",
                                "coordinates": list(zip(xs, ys))},
                   "properties": {height_key: height_val} if height_key else {}})
    return path


def test_surface_classes_set_manning_and_infiltration(tmp_path, grid):
    road = _poly_gpkg(str(tmp_path / "road.gpkg"), grid, (10, 28, 50, 32))
    grass = _poly_gpkg(str(tmp_path / "grass.gpkg"), grid, (0, 0, 60, 60))
    rep = burn_surface_classes(grid, {
        "grass": FeatureLayer(grass, "landcover"),
        "road": FeatureLayer(road, "roadbed"),
    })
    # Road strip: low Manning + zero infiltration; grass elsewhere: higher.
    assert grid.manning[30][30] == pytest.approx(0.013)
    assert grid.infiltration_rate[30][30] == 0.0
    assert grid.manning[5][5] == pytest.approx(0.035)         # grass
    assert grid.infiltration_rate[5][5] > 0.0
    assert grid.road_coverage[30][30] == 1.0
    assert rep.provenance["road"]["official_url"] == NYC_SOURCES["roadbed"]["url"]
    assert rep.provenance["grass"]["official_url"] == NYC_SOURCES["landcover"]["url"]


def test_retaining_wall_becomes_face_crest_in_meters(tmp_path, grid):
    # A vertical wall line along column 30, height 10 ft (=3.048 m).
    wall = _line_gpkg(str(tmp_path / "wall.gpkg"), grid,
                      [(30, 5), (30, 55)], height_key="HEIGHT", height_val=10.0)
    rep = burn_barriers(grid, [FeatureLayer(wall, "retaining_wall",
                                            height_attr="HEIGHT")])
    crest = np.asarray(grid.crest_x)
    bed = 3.0
    # Some vertical face near column 30 got a crest ≈ bed + 10 ft.
    got = crest[crest > -1e30]
    assert got.size > 0
    assert float(got.max()) == pytest.approx(bed + 10.0 * FT_TO_M, abs=0.3)
    assert rep.stats["barrier_cells"] > 0


def test_curb_default_height(tmp_path, grid):
    curb = _line_gpkg(str(tmp_path / "curb.gpkg"), grid, [(10, 30), (50, 30)])
    burn_barriers(grid, [FeatureLayer(curb, "median")], default_height_m=0.15)
    crest = np.asarray(grid.crest_y)
    got = crest[crest > -1e30]
    assert got.size > 0
    assert float(got.max()) == pytest.approx(3.0 + 0.15, abs=0.01)


def test_drainage_inlets_to_sink(tmp_path, grid):
    schema = {"geometry": "Point", "properties": {}}
    path = str(tmp_path / "inlets.gpkg")
    a, _, left, _, e, top = grid.transform
    pts = [(left + (20 + 0.5) * a, top + (20 + 0.5) * e),
           (left + (40 + 0.5) * a, top + (40 + 0.5) * e)]
    xs, ys = warp_transform(grid.crs, "EPSG:2263", [p[0] for p in pts], [p[1] for p in pts])
    with fiona.open(path, "w", driver="GPKG", crs="EPSG:2263", schema=schema, layer="l") as dst:
        for x, y in zip(xs, ys):
            dst.write({"geometry": {"type": "Point", "coordinates": (x, y)}, "properties": {}})
    rep = add_drainage_inlets(grid, FeatureLayer(path, "drainage_inlet"),
                              per_inlet_capacity_mm_hr=50.0)
    assert rep.stats["inlets"] == 2
    assert grid.drainage[20][20] > 0.0 and grid.drainage[0][0] == 0.0


def test_culverts_registered_from_lonlat(grid):
    rep = add_culverts(grid, [(-74.005, 40.715, -74.004, 40.716, 2.5)])
    assert rep.stats["culverts"] == 1
    assert len(grid.connections) == 1
    x1, y1, x2, y2, cd = grid.connections[0]
    assert cd == 2.5 and 0 <= x1 < grid.nx and 0 <= y2 < grid.ny


def test_provenance_has_official_urls(tmp_path, grid):
    road = _poly_gpkg(str(tmp_path / "road.gpkg"), grid, (10, 28, 50, 32))
    layer = FeatureLayer(road, "roadbed", transport_url="https://example.org/road.gpkg")
    p = layer.provenance()
    assert p["official_url"] == NYC_SOURCES["roadbed"]["url"]
    assert p["transport_url"] == "https://example.org/road.gpkg"
    assert len(p["source_sha256"]) == 64


def test_conditioned_grid_runs_and_routes(tmp_path, grid):
    # End-to-end on the real DEMSource grid dims: a full-height wall down the
    # mid-column must hold water released on the left, and the conditioned grid
    # must run on both solver backends identically.
    from aqua_sim.config import SimConfig, SolverConfig, StormConfig
    from aqua_sim.physics import BoundaryType
    from aqua_sim.physics.swe import ShallowWaterSolver
    from aqua_sim.physics.swe_numpy import NumpyShallowWaterSolver
    nx, ny = grid.nx, grid.ny
    mid = nx // 2
    for j in range(ny):
        for i in range(nx):
            grid.z[j][i] = 1.0
    # Wall spanning the FULL grid height at the mid column (top→bottom rows).
    wall = _line_gpkg(str(tmp_path / "w.gpkg"), grid,
                      [(mid, 0), (mid, ny - 1)], height_key="H", height_val=20.0)
    burn_barriers(grid, [FeatureLayer(wall, "retaining_wall", height_attr="H")])
    cfg = SimConfig(storm=StormConfig(rainfall_mm_per_hr=0, drainage_capacity_mm_per_hr=0),
                    solver=SolverConfig(total_time_s=600, output_interval_s=300))
    init = [[0.0] * nx for _ in range(ny)]
    for j in range(ny):
        for i in range(0, mid):
            init[j][i] = 1.0
    st_fast = list(NumpyShallowWaterSolver(grid, cfg, boundary=BoundaryType.CLOSED)
                   .run(initial_depth=[r[:] for r in init]))
    right = sum(st_fast[-1].depth[j][i] for j in range(ny) for i in range(mid + 1, nx))
    assert right == 0.0    # full-height wall holds the water on the left
    # Both backends agree on the conditioned grid.
    st_ref = list(ShallowWaterSolver(grid, cfg, boundary=BoundaryType.CLOSED)
                  .run(initial_depth=[r[:] for r in init]))
    assert abs(st_ref[-1].total_volume_m3 - st_fast[-1].total_volume_m3) < 1e-6

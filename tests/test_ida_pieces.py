"""Tests for the Ida-validation building blocks: hyetograph + building burn-in."""

import pytest

from aqua_sim.config import StormConfig


def test_hyetograph_steps_and_total():
    storm = StormConfig(duration_hours=4.0,
                        hyetograph=((0.0, 20.0), (1.0, 80.0), (2.0, 40.0), (3.0, 20.0)))
    mmhr = 1000.0 * 3600.0  # m/s -> mm/hr
    assert storm.rainfall_at(0.5 * 3600) * mmhr == pytest.approx(20.0)
    assert storm.rainfall_at(1.5 * 3600) * mmhr == pytest.approx(80.0)  # record hour
    assert storm.rainfall_at(2.5 * 3600) * mmhr == pytest.approx(40.0)
    assert storm.rainfall_at(5.0 * 3600) == 0.0                          # storm over
    assert storm.total_rainfall_mm() == pytest.approx(160.0)


def test_constant_storm_unchanged():
    storm = StormConfig(rainfall_mm_per_hr=50.0, duration_hours=2.0)
    assert storm.rainfall_at(3600.0) == pytest.approx(storm.rainfall_m_per_s())
    assert storm.rainfall_at(3 * 3600.0) == 0.0
    assert storm.total_rainfall_mm() == pytest.approx(100.0)


def test_burn_buildings_marks_obstacles(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_bounds

    from aqua_sim.ingestion.buildings import burn_buildings
    from aqua_sim.ingestion.dem import DEMSource

    # A small real-CRS grid via DEMSource.
    bbox = (-74.02, 40.70, -73.98, 40.74)
    w = h = 40
    z = np.full((h, w), 5.0, np.float32)
    dem = str(tmp_path / "d.tif")
    with rasterio.open(dem, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_bounds(*bbox, w, h), nodata=-9999.0) as dst:
        dst.write(z, 1)
    grid = DEMSource(dem, target_dx_m=60.0).load()

    # One square footprint in the middle of the bbox, with a height attribute.
    import json
    lon0, lat0 = -74.001, 40.719
    poly = {"type": "Polygon", "coordinates": [[
        [lon0, lat0], [lon0 + 0.002, lat0], [lon0 + 0.002, lat0 + 0.002],
        [lon0, lat0 + 0.002], [lon0, lat0]]]}
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": poly, "properties": {"heightroof": 25.0}}]}
    path = str(tmp_path / "bld.geojson")
    json.dump(gj, open(path, "w"))

    burned = burn_buildings(grid, path)
    assert burned > 0
    assert max(v for row in grid.obstacle for v in row) == pytest.approx(25.0)
    assert grid.meta["buildings_cells"] == burned


def test_burn_buildings_requires_georeferenced_grid():
    from aqua_sim.grid import Grid
    from aqua_sim.ingestion.buildings import burn_buildings
    with pytest.raises(ValueError, match="transform"):
        burn_buildings(Grid.empty(4, 4, 10.0), "nonexistent.geojson")
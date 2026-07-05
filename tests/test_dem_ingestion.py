"""DEM (GeoTIFF) ingestion tests.

Skipped when the optional ``geo`` extra (rasterio/numpy/pyproj) is not installed,
so the core test suite stays dependency-free. When present, a real GeoTIFF fixture
exercises the full reproject -> resample -> clip -> Grid pipeline.
"""

import pytest

pytest.importorskip("rasterio")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402

from aqua_sim.ingestion.dem import DEMSource, utm_epsg  # noqa: E402


# A Manhattan-ish WGS84 bbox for the fixture.
BBOX = (-74.02, 40.70, -73.93, 40.78)


@pytest.fixture
def dem_tif(tmp_path):
    """Write a real WGS84 GeoTIFF with a known elevation ramp (2..42 m)."""
    w = h = 120
    z = np.zeros((h, w), np.float32)
    for r in range(h):
        for c in range(w):
            z[r, c] = 2.0 + 30.0 * (c / (w - 1)) + 10.0 * (1 - r / (h - 1))
    path = str(tmp_path / "dem.tif")
    transform = from_bounds(*BBOX, w, h)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326", transform=transform,
                       nodata=-9999.0) as dst:
        dst.write(z, 1)
    return path


def test_utm_epsg_for_manhattan():
    assert utm_epsg(-73.97, 40.74) == 32618  # UTM 18N


def test_dem_reprojects_to_metric_utm(dem_tif):
    grid = DEMSource(dem_tif, target_dx_m=30.0).load()
    assert grid.crs == "EPSG:32618"          # metric UTM, not geographic
    assert grid.dx == 30.0
    assert grid.nx > 0 and grid.ny > 0
    zmin, zmax = grid.elevation_range()
    assert 1.0 < zmin < 5.0 and 38.0 < zmax < 45.0   # ramp preserved
    assert grid.meta["source"] == "DEMSource"
    assert grid.transform is not None


def test_aoi_clip_narrows_grid(dem_tif):
    full = DEMSource(dem_tif, target_dx_m=30.0).load()
    clipped = DEMSource(dem_tif, target_dx_m=30.0,
                        aoi_bounds=(-73.975, 40.70, -73.93, 40.78)).load()
    assert clipped.nx < full.nx  # eastern-half AOI has fewer columns


def test_resolution_controls_cell_count(dem_tif):
    coarse = DEMSource(dem_tif, target_dx_m=60.0).load()
    fine = DEMSource(dem_tif, target_dx_m=30.0).load()
    assert fine.nx > coarse.nx and fine.ny > coarse.ny


def test_max_cells_guard(dem_tif):
    with pytest.raises(ValueError, match="max_cells"):
        DEMSource(dem_tif, target_dx_m=1.0, max_cells=1000).load()

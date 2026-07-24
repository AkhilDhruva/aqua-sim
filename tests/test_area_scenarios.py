"""Named-area scenario builder + CLI: the reproducible entry point for every
scale we run (Manhattan / five-borough metro / complete New York area)."""

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")

from rasterio.transform import from_bounds

from aqua_sim.main import main
from aqua_sim.scenario import AREAS, build_area_scenario


@pytest.fixture
def dem_tiles(tmp_path):
    """Two adjacent 1-degree-ish tiles covering the NY-area longitudes."""
    paths = []
    for k, (lon0, lon1) in enumerate([(-74.3, -73.6), (-73.6, -71.9)]):
        w = h = 60
        z = np.full((h, w), 5.0 + k, np.float32)
        p = str(tmp_path / f"t{k}.tif")
        with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1,
                           dtype="float32", crs="EPSG:4326",
                           transform=from_bounds(lon0, 40.4, lon1, 41.3, w, h),
                           nodata=-9999.0) as dst:
            dst.write(z, 1)
        paths.append(p)
    return paths


def test_area_registry_is_ordered_by_scale():
    # Each area's default cell size must respect the walls-at-<=10 m policy:
    # screening areas are coarser, the fine area is finest.
    assert AREAS["ny"]["dx"] > AREAS["metro"]["dx"] > AREAS["manhattan"]["dx"]
    # NY area must span east past the metro box (it includes Long Island).
    assert AREAS["ny"]["bbox"][2] > AREAS["metro"]["bbox"][2]
    # ...and start west of it (Staten Island).
    assert AREAS["ny"]["bbox"][0] <= AREAS["metro"]["bbox"][0]


def test_build_area_scenario_mosaics_and_labels(dem_tiles):
    sc = build_area_scenario("ny", dem_tiles, target_dx_m=2000.0,
                             max_cells=500_000, node_count=2)
    assert sc.grid.crs.startswith("EPSG:326")          # metric UTM
    assert sc.grid.nx > 0 and sc.grid.ny > 0
    # Screening resolution must be stated in the AOI name (never mistaken for
    # a street-scale run).
    assert "screening resolution 2000 m" in sc.config.aoi_name
    assert "Staten Island to Long Island" in sc.config.aoi_name
    assert len(sc.nodes) == 2
    # The mosaic spans both tiles: elevations from tile 0 (5) and tile 1 (6).
    zs = {round(v) for row in sc.grid.z for v in row}
    assert 5 in zs and 6 in zs


def test_street_scale_label_when_fine(dem_tiles):
    # The full Manhattan box at 8 m is ~3.9M cells — real street-scale size.
    sc = build_area_scenario("manhattan", dem_tiles, target_dx_m=8.0,
                             max_cells=4_500_000, node_count=1)
    assert "street resolution 8 m" in sc.config.aoi_name
    assert sc.grid.nx * sc.grid.ny > 1_000_000


def test_unknown_area_rejected(dem_tiles):
    with pytest.raises(ValueError, match="unknown area"):
        build_area_scenario("brooklyn_only", dem_tiles)


def test_cli_area_run_end_to_end(tmp_path, dem_tiles):
    import json
    out = str(tmp_path / "run")
    rc = main(["run", out, "--area", "ny", "--dx", "3000",
               "--dem", dem_tiles[0], "--dem", dem_tiles[1]])
    assert rc == 0
    man = json.load(open(out + "/manifest.json"))
    assert "Staten Island to Long Island" in man["aoi"]
    assert man["frame_count"] > 0
    assert len(man["provenance"]["terrain_meta"]["source_path"]) == 2   # mosaic


def test_cli_area_without_dem_errors(tmp_path):
    assert main(["run", str(tmp_path / "x"), "--area", "ny"]) == 2

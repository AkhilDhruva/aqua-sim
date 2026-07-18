from aqua_sim.geofence import apply_bbox_mask
from aqua_sim.grid import Grid
from aqua_sim.ingestion import SyntheticTerrain


def test_synthetic_terrain_shape_and_provenance():
    grid = SyntheticTerrain(nx=32, ny=16, dx=2.0).load()
    assert grid.nx == 32 and grid.ny == 16
    assert len(grid.z) == 16 and len(grid.z[0]) == 32
    assert grid.meta["source"] == "SyntheticTerrain"


def test_synthetic_valley_is_lowest_at_center():
    grid = SyntheticTerrain(nx=32, ny=33, dx=5.0, slope=0.0, valley_depth=3.0).load()
    # With no slope, the central row should sit below an edge row.
    center = grid.z[16][10]
    edge = grid.z[0][10]
    assert center < edge


def test_empty_grid_defaults():
    grid = Grid.empty(4, 4, 1.0, default_manning=0.02)
    assert grid.elevation_range() == (0.0, 0.0)
    assert grid.cell_area_m2() == 1.0
    assert all(all(row) for row in grid.mask)


def test_bbox_mask_selects_only_the_box():
    grid = Grid.empty(4, 4, 1.0)
    apply_bbox_mask(grid, 1, 1, 2, 2)
    assert grid.mask[1][1] is True
    assert grid.mask[0][0] is False
    inside = sum(1 for row in grid.mask for c in row if c)
    assert inside == 4

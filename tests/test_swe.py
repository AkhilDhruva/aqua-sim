"""Physics validation for the local-inertial shallow-water solver.

These are the properties that make the engine trustworthy: mass conservation,
well-balancedness (lake-at-rest), non-negativity under a violent dam-break, and
correct source-term accounting.
"""

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.grid import Grid
from aqua_sim.physics import BoundaryType, ShallowWaterSolver


def _closed_cfg(total=600.0, out=300.0, rain=0.0, drain=0.0):
    return SimConfig(
        storm=StormConfig(rainfall_mm_per_hr=rain, duration_hours=10.0,
                          drainage_capacity_mm_per_hr=drain),
        solver=SolverConfig(cfl=0.7, total_time_s=total, output_interval_s=out),
    )


def test_mass_is_conserved_under_violent_dam_break():
    grid = Grid.empty(20, 20, 5.0, default_manning=0.03)
    solver = ShallowWaterSolver(grid, _closed_cfg(1200, 600), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 20 for _ in range(20)]
    for y in range(8, 12):
        for x in range(8, 12):
            init[y][x] = 5.0  # a 5 m column collapsing onto dry bed
    states = list(solver.run(initial_depth=init))
    v0, v1 = states[0].total_volume_m3, states[-1].total_volume_m3
    assert abs(v1 - v0) / v0 < 1e-9  # exact to floating point


def test_depths_never_go_negative():
    grid = Grid.empty(16, 16, 5.0, default_manning=0.02)
    solver = ShallowWaterSolver(grid, _closed_cfg(900, 450), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 16 for _ in range(16)]
    init[8][8] = 4.0
    for state in solver.run(initial_depth=init):
        assert min(v for row in state.depth for v in row) >= 0.0


def test_lake_at_rest_stays_still():
    grid = Grid.empty(20, 20, 5.0)
    for y in range(20):
        for x in range(20):
            grid.z[y][x] = 0.1 * x  # sloped bed
    level = 3.0
    init = [[max(level - grid.z[y][x], 0.0) for x in range(20)] for y in range(20)]
    solver = ShallowWaterSolver(grid, _closed_cfg(600, 300), boundary=BoundaryType.CLOSED)
    states = list(solver.run(initial_depth=init))
    assert max(s.max_speed for s in states) < 1e-9  # well-balanced: no spurious flow


def test_rainfall_adds_expected_volume():
    grid = Grid.empty(10, 10, 5.0)
    cfg = _closed_cfg(total=3600.0, out=1800.0, rain=100.0)  # 100 mm/hr for 1 hr
    solver = ShallowWaterSolver(grid, cfg, boundary=BoundaryType.CLOSED)
    states = list(solver.run())
    # 0.1 m over 10x10 cells of 25 m^2 = 250 m^3.
    assert abs(states[-1].total_volume_m3 - 250.0) < 1e-6


def test_obstacle_wall_blocks_flow():
    # A full column of building/obstacle cells splits the domain; water released on
    # the left must not cross to the right (DTM flow surface, DSM buildings = walls).
    grid = Grid.empty(21, 8, 5.0, default_manning=0.02)
    wall_x = 10
    for y in range(8):
        grid.obstacle[y][wall_x] = 30.0  # a 30 m building — impassable
    solver = ShallowWaterSolver(grid, _closed_cfg(600, 300), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 21 for _ in range(8)]
    for y in range(8):
        for x in range(0, wall_x):
            init[y][x] = 1.0  # water only on the left of the wall
    states = list(solver.run(initial_depth=init))
    right_water = sum(states[-1].depth[y][x]
                      for y in range(8) for x in range(wall_x + 1, 21))
    assert right_water == 0.0  # nothing leaked past the wall


def test_open_boundary_drains_water():
    grid = Grid.empty(16, 16, 5.0)
    for y in range(16):
        for x in range(16):
            grid.z[y][x] = 0.2 * x  # tilt so water runs toward x=0 edge
    solver = ShallowWaterSolver(grid, _closed_cfg(1200, 600), boundary=BoundaryType.OPEN)
    init = [[1.0] * 16 for _ in range(16)]
    states = list(solver.run(initial_depth=init))
    assert states[-1].total_volume_m3 < states[0].total_volume_m3

"""Equivalence: the NumPy backend must reproduce the reference solver.

Both solvers are driven through identical scenarios — dam-break over obstacles,
rain on real-ish terrain with open boundaries, hyetograph forcing — and must
agree cell-by-cell to floating-point tolerance at every output frame. This is
what licenses using the fast backend for science: it IS the reference, faster.
"""

import pytest

np = pytest.importorskip("numpy")

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.grid import Grid
from aqua_sim.physics import BoundaryType, ShallowWaterSolver
from aqua_sim.physics.swe_numpy import NumpyShallowWaterSolver, make_solver


def _compare(grid, cfg, boundary, init=None, tol=1e-9):
    ref = ShallowWaterSolver(grid, cfg, boundary=boundary)
    fast = NumpyShallowWaterSolver(grid, cfg, boundary=boundary)
    ref_states = list(ref.run(initial_depth=init))
    fast_states = list(fast.run(initial_depth=[row[:] for row in init] if init else None))
    assert len(ref_states) == len(fast_states)
    for a, b in zip(ref_states, fast_states):
        assert a.time_s == pytest.approx(b.time_s, abs=1e-9)
        da, db = np.asarray(a.depth), np.asarray(b.depth)
        assert float(np.max(np.abs(da - db))) < tol, \
            f"depth mismatch at t={a.time_s}: {float(np.max(np.abs(da - db)))}"
        assert a.total_volume_m3 == pytest.approx(b.total_volume_m3, rel=1e-9, abs=1e-6)
        sa, sb = np.asarray(a.speed), np.asarray(b.speed)
        assert float(np.max(np.abs(sa - sb))) < 1e-6
        # The scalar peaks drive hazard/alert reporting — they must match too.
        assert a.max_depth == pytest.approx(b.max_depth, rel=1e-9, abs=1e-12)
        assert a.max_speed == pytest.approx(b.max_speed, rel=1e-6, abs=1e-9)
    return ref_states, fast_states


def _cfg(total=900.0, out=300.0, rain=0.0, drain=0.0, hyet=None, dur=10.0):
    return SimConfig(
        storm=StormConfig(rainfall_mm_per_hr=rain, duration_hours=dur,
                          drainage_capacity_mm_per_hr=drain, hyetograph=hyet),
        solver=SolverConfig(cfl=0.7, total_time_s=total, output_interval_s=out),
    )


def test_equivalence_dam_break_closed():
    grid = Grid.empty(24, 20, 5.0, default_manning=0.03)
    init = [[0.0] * 24 for _ in range(20)]
    for y in range(8, 12):
        for x in range(4, 8):
            init[y][x] = 3.0
    _compare(grid, _cfg(), BoundaryType.CLOSED, init)


def test_equivalence_obstacles_and_open_boundary():
    grid = Grid.empty(20, 16, 5.0, default_manning=0.02)
    for y in range(16):
        for x in range(20):
            grid.z[y][x] = 0.05 * x  # tilt toward x=0 edge
    for y in range(5, 11):
        grid.obstacle[y][9] = 20.0   # building wall with a gap? no — solid slab
    grid.mask[0][19] = False          # one nodata void
    init = [[0.4] * 20 for _ in range(16)]
    for y in range(5, 11):
        init[y][9] = 0.0
    _compare(grid, _cfg(total=600.0, out=200.0), BoundaryType.OPEN, init)


def test_equivalence_rain_and_drainage():
    grid = Grid.empty(15, 15, 10.0, default_manning=0.025)
    for y in range(15):
        for x in range(15):
            grid.z[y][x] = 0.02 * ((x - 7) ** 2 + (y - 7) ** 2) ** 0.5  # shallow bowl
    _compare(grid, _cfg(total=1800.0, out=600.0, rain=80.0, drain=15.0),
             BoundaryType.CLOSED)


def test_equivalence_hyetograph():
    grid = Grid.empty(12, 12, 10.0)
    hyet = ((0.0, 20.0), (0.25, 80.0), (0.5, 40.0))
    _compare(grid, _cfg(total=2700.0, out=900.0, hyet=hyet, dur=0.75),
             BoundaryType.CLOSED)


def test_equivalence_single_row_and_column():
    # Degenerate 1xN / Nx1 channels exercise the empty interior-face slices and
    # the both-edges-share-one-cell paths of the vectorized backend.
    row = Grid.empty(12, 1, 5.0, default_manning=0.03)
    init_row = [[0.0] * 12]
    init_row[0][2] = 1.5
    _compare(row, _cfg(total=300.0, out=100.0), BoundaryType.OPEN, init_row)
    _compare(Grid.empty(12, 1, 5.0), _cfg(total=300.0, out=100.0, rain=60.0),
             BoundaryType.CLOSED)

    col = Grid.empty(1, 12, 5.0, default_manning=0.03)
    init_col = [[0.0] for _ in range(12)]
    init_col[3][0] = 1.5
    _compare(col, _cfg(total=300.0, out=100.0), BoundaryType.OPEN, init_col)

    one = Grid.empty(1, 1, 5.0)
    _compare(one, _cfg(total=120.0, out=60.0, rain=100.0), BoundaryType.CLOSED)


def test_make_solver_backends():
    grid = Grid.empty(4, 4, 5.0)
    cfg = _cfg()
    assert isinstance(make_solver(grid, cfg, backend="numpy"), NumpyShallowWaterSolver)
    assert isinstance(make_solver(grid, cfg, backend="reference"), ShallowWaterSolver)
    assert isinstance(make_solver(grid, cfg, backend="auto"), NumpyShallowWaterSolver)
    with pytest.raises(ValueError):
        make_solver(grid, cfg, backend="cuda")
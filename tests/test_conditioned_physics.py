"""Phase 6A conditioned-surface physics: subgrid crests, per-cell infiltration
& drainage, and bridge/culvert connections — the validation proofs, plus a
dual-backend equivalence check that the conditioned path also stays in lockstep.
"""

import math

import pytest

from aqua_sim.config import GRAVITY, SimConfig, SolverConfig, StormConfig
from aqua_sim.grid import Grid, zeros
from aqua_sim.physics import BoundaryType, ShallowWaterSolver
from aqua_sim.physics.swe import NO_CREST

np = pytest.importorskip("numpy")
from aqua_sim.physics.swe_numpy import NumpyShallowWaterSolver  # noqa: E402


def _cfg(total=600.0, out=200.0, rain=0.0, drain=0.0):
    return SimConfig(storm=StormConfig(rainfall_mm_per_hr=rain, duration_hours=10.0,
                                       drainage_capacity_mm_per_hr=drain),
                     solver=SolverConfig(cfl=0.7, total_time_s=total, output_interval_s=out))


def _flat(nx, ny, dx=5.0, z=1.0, manning=0.03):
    g = Grid.empty(nx, ny, dx, default_manning=manning)
    for y in range(ny):
        for x in range(nx):
            g.z[y][x] = z
    return g


# --- Proof 1: a retaining wall / curb crest blocks & routes flow ------------

def test_crest_blocks_flow_until_overtopped():
    # Flat bed at z=1; a crest wall of elevation 3 m spans the middle column
    # face, with NO gap. Water 1.5 m on the left cannot reach the right.
    g = _flat(20, 6)
    g.crest_x = zeros(6, 21)
    for row in g.crest_x:
        for i in range(21):
            row[i] = NO_CREST
    for y in range(6):
        g.crest_x[y][10] = 3.0            # crest at the face left-of-column-10
    solver = ShallowWaterSolver(g, _cfg(), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 20 for _ in range(6)]
    for y in range(6):
        for x in range(0, 10):
            init[y][x] = 1.5              # surface 2.5 m < crest 3 m
    states = list(solver.run(initial_depth=init))
    right = sum(states[-1].depth[y][x] for y in range(6) for x in range(10, 20))
    assert right == 0.0                   # crest holds; nothing crosses
    v0, v1 = states[0].total_volume_m3, states[-1].total_volume_m3
    assert abs(v1 - v0) / v0 < 1e-9       # closed + crest => mass conserved


def test_crest_is_overtopped_when_surface_exceeds_it():
    # Same wall but water deep enough (surface 3.5 m > crest 3 m) tops it.
    g = _flat(20, 6)
    g.crest_x = [[NO_CREST] * 21 for _ in range(6)]
    for y in range(6):
        g.crest_x[y][10] = 3.0
    solver = ShallowWaterSolver(g, _cfg(total=1200, out=600), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 20 for _ in range(6)]
    for y in range(6):
        for x in range(0, 10):
            init[y][x] = 2.6             # surface 3.6 m > crest
    states = list(solver.run(initial_depth=init))
    right = sum(states[-1].depth[y][x] for y in range(6) for x in range(10, 20))
    assert right > 0.0                   # overtopping spills to the right


# --- Proof 2: roads (low roughness) channel flow ----------------------------

def test_road_channels_flow_faster_than_rough_ground():
    # A tilted plane; a smooth "road" strip (n=0.013) vs rough grass (n=0.06).
    # Measured before the front reaches the edge, the road conveys it farther.
    def run(road):
        g = Grid.empty(60, 9, 5.0, default_manning=0.06)
        for y in range(9):
            for x in range(60):
                g.z[y][x] = 0.04 * (59 - x)      # gentle slope toward +x
        if road:
            for y in range(3, 6):
                for x in range(60):
                    g.manning[y][x] = 0.013
        solver = ShallowWaterSolver(g, _cfg(total=60, out=60), boundary=BoundaryType.OPEN)
        init = [[0.0] * 60 for _ in range(9)]
        for y in range(9):
            for x in range(0, 4):
                init[y][x] = 1.0
        st = list(solver.run(initial_depth=init))[-1]
        row = st.depth[4]                       # centre row (smooth when road=True)
        return max((x for x in range(60) if row[x] > 0.02), default=0)

    front_road = run(road=True)
    front_grass = run(road=False)
    assert front_road > front_grass and front_road < 59   # farther, not saturated


# --- Proof 3: a bridge/culvert connection prevents a false dam --------------

def test_culvert_prevents_false_dam():
    # An embankment (crest wall) splits a channel; without a culvert the
    # upstream side dams, with one the downstream side fills.
    def run(with_culvert):
        g = _flat(20, 3, dx=5.0, z=0.0)
        g.crest_x = [[NO_CREST] * 21 for _ in range(3)]
        for y in range(3):
            g.crest_x[y][10] = 5.0            # tall embankment, never overtopped
        if with_culvert:
            # a culvert linking cell (9,1) upstream to (10,1) downstream
            g.connections = [(9, 1, 10, 1, 2.0)]
        solver = ShallowWaterSolver(g, _cfg(total=1200, out=600), boundary=BoundaryType.CLOSED)
        init = [[0.0] * 20 for _ in range(3)]
        for y in range(3):
            for x in range(0, 10):
                init[y][x] = 1.0
        st = list(solver.run(initial_depth=init))[-1]
        downstream = sum(st.depth[y][x] for y in range(3) for x in range(10, 20))
        return downstream

    assert run(with_culvert=False) == 0.0     # embankment dams completely
    assert run(with_culvert=True) > 0.0       # culvert passes flow through it


def test_connection_conserves_mass():
    g = _flat(12, 3, z=0.0)
    g.connections = [(2, 1, 9, 1, 1.5)]
    solver = ShallowWaterSolver(g, _cfg(), boundary=BoundaryType.CLOSED)
    init = [[0.0] * 12 for _ in range(3)]
    init[1][2] = 2.0
    states = list(solver.run(initial_depth=init))
    v0, v1 = states[0].total_volume_m3, states[-1].total_volume_m3
    assert abs(v1 - v0) / v0 < 1e-9


# --- Proof 4: per-cell infiltration & drainage mass balance -----------------

def test_infiltration_removes_water_up_to_capacity():
    # Rain onto a flat closed basin; infiltration capacity caps total loss.
    g = _flat(10, 10, z=0.0)
    rate = 20.0 / 1000.0 / 3600.0        # 20 mm/hr in m/s
    cap = 0.01                           # 10 mm total capacity
    g.infiltration_rate = [[rate] * 10 for _ in range(10)]
    g.infiltration_capacity = [[cap] * 10 for _ in range(10)]
    cfg = _cfg(total=3600.0, out=1800.0, rain=50.0)   # 50 mm/hr for 1 hr = 50 mm
    solver = ShallowWaterSolver(g, cfg, boundary=BoundaryType.CLOSED)
    states = list(solver.run())
    area = g.cell_area_m2() * 100
    # 50 mm rained, ≤10 mm infiltrated → ≥40 mm (0.04 m) remains as depth.
    final = states[-1].total_volume_m3
    assert final == pytest.approx((0.050 - 0.010) * area, rel=0.02)
    # cumulative infiltration never exceeds capacity anywhere.
    assert max(v for row in solver._cum_infil for v in row) <= cap + 1e-12


def test_drainage_inlet_removes_standing_water():
    g = _flat(6, 6, z=0.0)
    g.drainage = [[10.0 / 1000.0 / 3600.0] * 6 for _ in range(6)]  # 10 mm/hr sink
    solver = ShallowWaterSolver(g, _cfg(total=3600, out=1800), boundary=BoundaryType.CLOSED)
    init = [[0.05] * 6 for _ in range(6)]   # 50 mm standing
    states = list(solver.run(initial_depth=init))
    assert states[-1].total_volume_m3 < states[0].total_volume_m3   # drained down


# --- Proof 5: conditioned dual-backend equivalence --------------------------

def test_conditioned_backends_agree():
    # Crests + infiltration + drainage + a connection, both solvers, lockstep.
    def build():
        g = Grid.empty(16, 10, 5.0, default_manning=0.03)
        for y in range(10):
            for x in range(16):
                g.z[y][x] = 0.03 * x
        g.crest_x = [[NO_CREST] * 17 for _ in range(10)]
        for y in range(10):
            g.crest_x[y][8] = 1.2
        g.infiltration_rate = [[15.0 / 1000 / 3600] * 16 for _ in range(10)]
        g.infiltration_capacity = [[0.02] * 16 for _ in range(10)]
        g.drainage = [[5.0 / 1000 / 3600] * 16 for _ in range(10)]
        g.connections = [(7, 5, 8, 5, 1.0)]
        return g

    cfg = _cfg(total=1200.0, out=400.0, rain=90.0)
    ref = ShallowWaterSolver(build(), cfg, boundary=BoundaryType.OPEN)
    fast = NumpyShallowWaterSolver(build(), cfg, boundary=BoundaryType.OPEN)
    init = [[0.3] * 16 for _ in range(10)]
    rs = list(ref.run(initial_depth=[r[:] for r in init]))
    fs = list(fast.run(initial_depth=[r[:] for r in init]))
    assert len(rs) == len(fs)
    for a, b in zip(rs, fs):
        da, db = np.asarray(a.depth), np.asarray(b.depth)
        assert float(np.max(np.abs(da - db))) < 1e-9
        assert a.total_volume_m3 == pytest.approx(b.total_volume_m3, rel=1e-9, abs=1e-6)
        assert a.max_depth == pytest.approx(b.max_depth, rel=1e-9, abs=1e-12)

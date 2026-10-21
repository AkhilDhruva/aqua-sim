"""Tests for the Phase 6A validation utilities: cross-section sampling and the
multi-resolution mass-balance / arrival-time comparison."""

import pytest

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.grid import Grid
from aqua_sim.physics import BoundaryType, ShallowWaterSolver
from aqua_sim.physics.swe import NO_CREST
from aqua_sim.scenario import Scenario
from aqua_sim.validation.hydraulic import compare_resolutions, cross_section


def test_cross_section_reports_bed_crest_depth():
    g = Grid.empty(20, 5, 5.0)
    for y in range(5):
        for x in range(20):
            g.z[y][x] = 1.0
    g.crest_x = [[NO_CREST] * 21 for _ in range(5)]
    for y in range(5):
        g.crest_x[y][10] = 3.0            # a curb at the face left of column 10
    cfg = SimConfig(storm=StormConfig(rainfall_mm_per_hr=0, drainage_capacity_mm_per_hr=0),
                    solver=SolverConfig(total_time_s=1, output_interval_s=1))
    solver = ShallowWaterSolver(g, cfg, boundary=BoundaryType.CLOSED)
    init = [[0.5] * 20 for _ in range(5)]
    state = list(solver.run(initial_depth=init))[0]
    xs = cross_section(g, state, 0, 2, 19, 2)   # horizontal line across the curb
    assert len(xs.cells) == 20
    assert xs.bed_m[5] == pytest.approx(1.0)
    assert xs.depth_m[5] == pytest.approx(0.5)
    assert xs.surface_m[5] == pytest.approx(1.5)
    # The curb crest shows up near column 10.
    assert any(c is not None and c == pytest.approx(3.0) for c in xs.crest_m)
    assert all(c is None for c in xs.crest_m[:9])   # no barrier away from the curb


def _flat_scenario_factory(nx_at_5m=24):
    """A closure producing a flat rainfall basin at a chosen cell size."""
    def make(dx):
        cells = int(round(nx_at_5m * 5.0 / dx))
        g = Grid.empty(cells, cells, dx)
        for y in range(cells):
            for x in range(cells):
                g.z[y][x] = 0.0
        cfg = SimConfig(
            storm=StormConfig(rainfall_mm_per_hr=60.0, duration_hours=1.0,
                              drainage_capacity_mm_per_hr=0.0),
            solver=SolverConfig(cfl=0.7, total_time_s=1800.0, output_interval_s=600.0),
            aoi_name=f"flat-{dx}m")
        return Scenario(grid=g, config=cfg, nodes=[], boundary=BoundaryType.CLOSED)
    return make


def test_compare_resolutions_mass_balance():
    # A closed flat basin under uniform rain: peak stored volume should equal
    # the rain volume at BOTH resolutions (mass conserved), so the diff is ~0.
    cmp = compare_resolutions(_flat_scenario_factory(), coarse_dx=10.0, fine_dx=5.0)
    assert cmp.coarse_mass_error < 1e-6      # closed basin, no losses
    assert cmp.fine_mass_error < 1e-6
    assert cmp.volume_rel_diff < 1e-6        # both hold the same rain volume
    assert cmp.notes                          # human-readable report present

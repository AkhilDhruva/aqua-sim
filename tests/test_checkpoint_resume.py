"""An interrupted run resumed from checkpoints equals an uninterrupted run."""

import os

import pytest

np = pytest.importorskip("numpy")

from aqua_sim import scenario as scenario_mod  # noqa: E402
from aqua_sim.config import SimConfig, SolverConfig, StormConfig  # noqa: E402
from aqua_sim.grid import Grid  # noqa: E402
from aqua_sim.physics import BoundaryType  # noqa: E402
from aqua_sim.risk.sink_nodes import SinkNode  # noqa: E402
from aqua_sim.scenario import Scenario, run_scenario  # noqa: E402


def make_scenario():
    """A sloped street with a wall, a low node and a time-varying storm, so
    the state carries real fluxes, a breach and infiltration capacity."""
    nx, ny = 24, 16
    g = Grid.empty(nx, ny, 5.0)
    z = np.add.outer(np.linspace(0.6, 0.0, ny), np.linspace(0.4, 0.0, nx))
    z[6:10, 12:14] += 3.0
    g.z = z.tolist()
    obst = np.zeros((ny, nx)); obst[2:5, 3:7] = 12.0
    g.obstacle = obst.tolist()
    g.infiltration_rate = np.full((ny, nx), 2e-6).tolist()
    g.infiltration_capacity = np.full((ny, nx), 0.004).tolist()
    node = SinkNode("Low", x=22, y=14, threshold_elevation=float(z[14, 22]) + 0.02,
                    opening_area_m2=2.0, capacity_m3=40.0)
    cfg = SimConfig(storm=StormConfig(rainfall_mm_per_hr=90.0, duration_hours=1.0,
                                      drainage_capacity_mm_per_hr=5.0,
                                      hyetograph=[(0.0, 60.0), (0.25, 90.0)]),
                    solver=SolverConfig(total_time_s=2400.0, output_interval_s=300.0, backend="numpy"),
                    aoi_name="checkpoint-test")
    return Scenario(grid=g, config=cfg, nodes=[node], boundary=BoundaryType.CLOSED)


def read_all(d):
    return {f: open(os.path.join(d, f), "rb").read() for f in sorted(os.listdir(d))}


def test_resume_after_interruption_is_byte_identical(tmp_path, monkeypatch):
    ref = str(tmp_path / "ref")
    run_scenario(make_scenario(), ref)

    ck = str(tmp_path / "ck")
    out = str(tmp_path / "out")
    real_write_run = scenario_mod.write_run

    def crashing_write_run(run_dir, grid, frames, *args, **kwargs):
        for i, _ in enumerate(frames, start=1):
            if i == 4:
                raise RuntimeError("container restarted")
        raise AssertionError("should have crashed")

    monkeypatch.setattr(scenario_mod, "write_run", crashing_write_run)
    with pytest.raises(RuntimeError, match="container restarted"):
        run_scenario(make_scenario(), out, checkpoint_dir=ck)
    monkeypatch.setattr(scenario_mod, "write_run", real_write_run)
    assert sorted(f for f in os.listdir(ck) if f.startswith("frame_")) == \
        ["frame_001.npz", "frame_002.npz", "frame_003.npz", "frame_004.npz"]

    manifest = run_scenario(make_scenario(), out, checkpoint_dir=ck)
    assert manifest["frame_count"] == 9
    assert read_all(out) == read_all(ref)


def test_checkpoint_folder_refuses_a_different_run(tmp_path):
    ck = str(tmp_path / "ck")
    run_scenario(make_scenario(), str(tmp_path / "a"), checkpoint_dir=ck)
    import dataclasses
    other = make_scenario()
    other.config = dataclasses.replace(
        other.config, solver=dataclasses.replace(other.config.solver, total_time_s=1200.0))
    with pytest.raises(ValueError, match="different run"):
        run_scenario(other, str(tmp_path / "b"), checkpoint_dir=ck)

"""End-to-end scenario: solver -> risk -> exported run folder."""

import json
import os

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.scenario import Scenario, build_manhattan_demo, run_scenario
from aqua_sim.grid import Grid
from aqua_sim.risk.sink_nodes import SinkNode


def test_manhattan_demo_builds_a_basin_and_nodes():
    sc = build_manhattan_demo()
    assert sc.grid.nx > 0 and sc.grid.ny > 0
    assert len(sc.nodes) >= 1
    zmin, zmax = sc.grid.elevation_range()
    assert zmax > zmin  # real relief


def test_run_scenario_writes_frames_and_manifest(tmp_path):
    # A small, fast scenario: flat grid, one basin node, short storm.
    grid = Grid.empty(12, 12, 10.0)
    node = SinkNode("Test Node", x=6, y=6, threshold_elevation=0.05,
                    opening_area_m2=2.0, capacity_m3=500.0)
    cfg = SimConfig(
        storm=StormConfig(rainfall_mm_per_hr=120.0, duration_hours=1.0,
                          drainage_capacity_mm_per_hr=0.0),
        solver=SolverConfig(total_time_s=600.0, output_interval_s=120.0),
        aoi_name="unit-test",
    )
    from aqua_sim.physics import BoundaryType
    sc = Scenario(grid=grid, config=cfg, nodes=[node], boundary=BoundaryType.CLOSED)

    run_dir = str(tmp_path / "run")
    manifest = run_scenario(sc, run_dir)

    # Manifest is well-formed and provenance is recorded.
    assert manifest["frame_count"] == len(manifest["frames"])
    assert manifest["provenance"]["solver_scheme"] == "local_inertial"
    assert manifest["provenance"]["storm"]["rainfall_mm_per_hr"] == 120.0
    assert manifest["grid"]["nx"] == 12

    # Frame files exist and match the manifest.
    assert os.path.exists(os.path.join(run_dir, "frame_001.json"))
    assert os.path.exists(os.path.join(run_dir, "manifest.json"))
    assert os.path.exists(os.path.join(run_dir, "terrain.json"))
    last = f"frame_{manifest['frame_count']:03d}.json"
    frame = json.load(open(os.path.join(run_dir, last)))
    assert len(frame["depth"]) == 12 and len(frame["depth"][0]) == 12


def test_scenario_triggers_sink_node_alert():
    sc = build_manhattan_demo()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        run_scenario(sc, d)
        alerts = json.load(open(os.path.join(d, "alerts.json")))
    assert any(a["severity"] == "CRITICAL" for a in alerts)
    # Critical is ranked ahead of warning.
    assert alerts[0]["severity"] == "CRITICAL"

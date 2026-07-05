"""aqua-sim CLI.

Two modes:
  * default    — a quick self-check of the wired building blocks.
  * ``run``    — run the Manhattan demo scenario end to end (solver -> risk ->
                 exported frame_NNN.json), the "brain" side of the architecture.

    python -m aqua_sim              # self-check
    python -m aqua_sim run OUTDIR   # full pre-computed run into OUTDIR
"""

from __future__ import annotations

import sys

from aqua_sim import __version__
from aqua_sim.config import SimConfig
from aqua_sim.ingestion import SyntheticTerrain
from aqua_sim.physics import cfl_timestep
from aqua_sim.risk import SinkNode, classify_hazard, orifice_inflow


def _self_check() -> int:
    cfg = SimConfig(aoi_name="demo-synthetic")
    print(f"aqua-sim v{__version__} — Flood Zone Risk Simulator")

    grid = SyntheticTerrain(nx=64, ny=64, dx=5.0).load()
    zmin, zmax = grid.elevation_range()
    print(
        f"  terrain: {grid.nx}x{grid.ny} @ {grid.dx} m/cell, "
        f"elevation {zmin:.1f}..{zmax:.1f} m  [{grid.crs}]"
    )
    dt = cfl_timestep(dx=grid.dx, max_depth=0.5, max_speed=1.0, cfl=cfg.solver.cfl)
    print(f"  stable timestep (CFL {cfg.solver.cfl}): dt = {dt:.2f} s")
    print(
        f"  hazard(0.2 m, 0.3 m/s) = {classify_hazard(0.2, 0.3).name}; "
        f"hazard(1.0 m, 2.0 m/s) = {classify_hazard(1.0, 2.0).name}"
    )
    node = SinkNode(name="Transit Node 4", x=32, y=32,
                    threshold_elevation=98.0, opening_area_m2=4.0)
    q = orifice_inflow(node, water_surface_elevation=98.8)
    print(f"  sink '{node.name}': inflow at 0.8 m head = {q:.2f} m^3/s")
    print("  run a full simulation with:  python -m aqua_sim run OUTDIR")
    return 0


def _run(out_dir: str) -> int:
    from aqua_sim.scenario import build_manhattan_demo, run_scenario

    print(f"aqua-sim v{__version__} — running Manhattan demo scenario...")
    sc = build_manhattan_demo()
    manifest = run_scenario(sc, out_dir)
    print(f"  grid: {sc.grid.nx}x{sc.grid.ny} @ {sc.grid.dx} m  [{sc.grid.crs}]")
    print(f"  frames: {manifest['frame_count']}  peak depth: {manifest['peak_depth_m']} m")
    print(f"  wrote run to: {out_dir}/  (manifest.json, frame_NNN.json, alerts.json)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "run":
        out = args[1] if len(args) > 1 else "output/manhattan_run"
        return _run(out)
    return _self_check()


if __name__ == "__main__":
    raise SystemExit(main())

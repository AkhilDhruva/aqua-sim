"""aqua-sim CLI.

Phase 0 entry point: exercises the dependency-free seed end to end so the module
skeleton is demonstrably wired — generate synthetic terrain, report a
CFL-limited timestep, and show the hazard + sink-node physics. The shallow-water
solver itself is Phase 2 (see docs/PLANNING.md for the roadmap).
"""

from __future__ import annotations

from aqua_sim import __version__
from aqua_sim.config import SimConfig
from aqua_sim.ingestion import SyntheticTerrain
from aqua_sim.physics import cfl_timestep
from aqua_sim.risk import SinkNode, classify_hazard, orifice_inflow


def main() -> int:
    cfg = SimConfig(aoi_name="demo-synthetic")
    print(f"aqua-sim v{__version__} — Flood Zone Risk Simulator (Phase 0)")

    grid = SyntheticTerrain(nx=64, ny=64, dx=5.0).load()
    zmin, zmax = grid.elevation_range()
    print(
        f"  terrain: {grid.nx}x{grid.ny} @ {grid.dx} m/cell, "
        f"elevation {zmin:.1f}..{zmax:.1f} m  [{grid.crs}]"
    )

    # A CFL-limited timestep for a hypothetical 0.5 m deep, 1 m/s flow.
    dt = cfl_timestep(dx=grid.dx, max_depth=0.5, max_speed=1.0, cfl=cfg.solver.cfl)
    print(f"  stable timestep (CFL {cfg.solver.cfl}): dt = {dt:.2f} s")

    # Hazard demo: shallow-slow vs deep-fast.
    print(
        f"  hazard(0.2 m, 0.3 m/s) = {classify_hazard(0.2, 0.3).name}; "
        f"hazard(1.0 m, 2.0 m/s) = {classify_hazard(1.0, 2.0).name}"
    )

    # Sink-node demo: inflow once surface water tops the threshold.
    node = SinkNode(name="Transit Node 4", x=32, y=32,
                    threshold_elevation=98.0, opening_area_m2=4.0)
    q = orifice_inflow(node, water_surface_elevation=98.8)
    print(f"  sink '{node.name}': inflow at 0.8 m head = {q:.2f} m^3/s")

    print("  solver: Phase 2 (not yet implemented) — see docs/PLANNING.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

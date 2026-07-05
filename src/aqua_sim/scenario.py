"""End-to-end scenario runner: DEM/terrain -> solver -> risk -> exported frames.

This is the "brain" side of the pre-computed architecture. A scenario:
  1. loads a terrain Grid (real DEM in Phase 1; a Manhattan-scaled synthetic
     stand-in today),
  2. runs the shallow-water solver under a storm,
  3. evaluates sink-node and hazard alerts per frame,
  4. writes a self-describing run folder (manifest + frame_NNN.json + alerts).

The Three.js viewer (Phase 4) is a thin telemetry dashboard over that folder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.export.frames import write_run
from aqua_sim.grid import Grid
from aqua_sim.physics.boundary import BoundaryType
from aqua_sim.physics.swe import ShallowWaterSolver
from aqua_sim.risk.alerts import AlertLog, Severity
from aqua_sim.risk.hazard import HazardClass, classify_hazard
from aqua_sim.risk.sink_nodes import SinkNode, orifice_inflow, time_to_fill


@dataclass
class Scenario:
    grid: Grid
    config: SimConfig
    nodes: list[SinkNode]
    boundary: BoundaryType = BoundaryType.OPEN


def build_manhattan_demo(nx: int = 48, ny: int = 72, dx: float = 60.0) -> Scenario:
    """A Manhattan-scaled synthetic AOI: an elongated island with a central ridge
    sloping to river edges near sea level, plus a low-lying flood-prone southern
    zone. A stand-in for the real USGS 3DEP 1 m DEM (see docs/DATA_SOURCING.md);
    swap in ``ingestion.DEMSource`` when the real tile is ingested.

    Coordinates are illustrative, not georeferenced. Elevation in meters above a
    local sea-level datum; river edges sit at ~0 m (open water / outflow).
    """
    grid = Grid.empty(nx, ny, dx, default_manning=0.02)  # ~urban concrete/asphalt
    cx = (nx - 1) / 2.0
    for y in range(ny):
        # A gentle ridge running the length of the island, higher toward the north.
        spine = 6.0 + 6.0 * (y / (ny - 1))
        for x in range(nx):
            # Cross-island profile: high at the central spine, ~0 at the river banks.
            across = 1.0 - (abs(x - cx) / (cx + 1e-9))  # 1 at center, 0 at edges
            elev = spine * (across ** 1.5)
            # Low-lying, flood-prone southern tip (below y ~ 12): pull it down.
            if y < 12:
                elev *= (0.25 + 0.06 * y)
            grid.z[y][x] = max(elev, 0.0)
            # Rivers on the outer two columns are open water (low roughness).
            if x <= 0 or x >= nx - 1:
                grid.manning[y][x] = 0.03

    # Carve an enclosed low-lying basin (the flood-prone zone). A rim of higher
    # ground traps runoff, so water ponds here until it overtops — exactly how
    # underpasses and basin neighborhoods flood. Bottom sits near sea level.
    bx, by, radius, bottom, rim = cx, 9.0, 8.0, 0.2, 2.6
    for y in range(ny):
        for x in range(nx):
            d = math.hypot(x - bx, y - by) / radius
            if d < 1.0:
                grid.z[y][x] = bottom + (rim - bottom) * d * d  # parabolic bowl

    grid.crs = "SYNTHETIC (Manhattan-scaled stand-in)"
    grid.meta = {
        "source": "build_manhattan_demo",
        "resolution_m": dx,
        "note": "synthetic stand-in for USGS 3DEP 1 m DEM of Manhattan; not georeferenced",
        "aoi": "Manhattan (illustrative)",
    }

    # Subterranean sink nodes (illustrative subway entrances). The first sits at
    # the basin floor, where runoff concentrates; its threshold is just above the
    # basin bottom so a design storm breaches it.
    bcx, bcy = int(cx), 9
    nodes = [
        SinkNode("Basin Transit Node", x=bcx, y=bcy,
                 threshold_elevation=grid.z[bcy][bcx] + 0.15,
                 opening_area_m2=4.0, capacity_m3=2500.0),
        SinkNode("Riverside Underpass", x=3, y=30,
                 threshold_elevation=grid.z[30][3] + 0.25,
                 opening_area_m2=3.0, capacity_m3=2000.0),
    ]

    config = SimConfig(
        storm=StormConfig(rainfall_mm_per_hr=90.0, duration_hours=2.0,
                          drainage_capacity_mm_per_hr=15.0, drainage_blockage=0.6),
        solver=SolverConfig(cfl=0.7, total_time_s=7200.0, output_interval_s=72.0),
        aoi_name="Manhattan (demo)",
    )
    return Scenario(grid=grid, config=config, nodes=nodes)


def _evaluate_alerts(scenario: Scenario, states) -> AlertLog:
    """Scan frames for hazard escalation and sink-node breaches."""
    log = AlertLog()
    grid, nodes = scenario.grid, scenario.nodes
    warned_surface = set()
    breached = set()
    node_fill = {n.name: 0.0 for n in nodes}  # accumulated inflow volume (m^3)
    prev_t = 0.0

    for state in states:
        dt = state.time_s - prev_t
        prev_t = state.time_s
        # Peak hazard across the domain.
        worst = HazardClass.NONE
        for y in range(grid.ny):
            for x in range(grid.nx):
                worst = max(worst, classify_hazard(state.depth[y][x], state.max_speed))
        for node in nodes:
            h = state.depth[node.y][node.x]
            wse = grid.z[node.y][node.x] + h
            if h > 0 and node.name not in warned_surface and wse >= node.threshold_elevation - 0.15:
                warned_surface.add(node.name)
                log.add(state.time_s, Severity.WARNING, node.name,
                        f"Surface water approaching critical depth near {node.name}.")
            q = orifice_inflow(node, wse)
            if q > 0:
                node_fill[node.name] += q * dt
                if node.name not in breached:
                    breached.add(node.name)
                    ttf = time_to_fill(node, wse)
                    eta = f" Est. full inundation in ~{ttf/60:.0f} min." if ttf else ""
                    log.add(state.time_s, Severity.CRITICAL, node.name,
                            f"Breach detected at {node.name}.{eta}")
    return log


def run_scenario(scenario: Scenario, run_dir: str) -> dict:
    """Run the solver, evaluate alerts, and write the run folder. Returns manifest."""
    solver = ShallowWaterSolver(scenario.grid, scenario.config, boundary=scenario.boundary)
    states = list(solver.run())  # ~100 frames; small enough to hold in memory
    alerts = _evaluate_alerts(scenario, states)
    return write_run(run_dir, scenario.grid, states, scenario.config,
                     alerts=alerts.to_records(), scheme=scenario.config.solver.scheme)

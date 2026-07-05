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


# Breach thresholds (see the "breach definition" note in docs/ARCHITECTURE.md §4.2).
_MIN_DEPTH = 1e-3          # m; ignore numerically thin water films
_BREACH_HEAD_EPS = 0.02   # m; head above the entrance lip that counts as a breach
_WARN_APPROACH = 0.15     # m; how close below the lip triggers an early warning


def build_scenario_from_dem(
    dem_path: str,
    aoi_bounds: tuple[float, float, float, float] | None = None,
    target_dx_m: float = 30.0,
    storm: StormConfig | None = None,
    nodes: list[SinkNode] | None = None,
    aoi_name: str = "Manhattan (USGS 3DEP)",
) -> Scenario:
    """Build a scenario from a real GeoTIFF DEM (see ingestion.DEMSource).

    Requires the ``geo`` extra. Sink nodes, if not supplied, are auto-placed at
    the lowest in-AOI cells (proxies for flood-prone low points) so the run still
    produces risk output on real terrain.
    """
    from aqua_sim.ingestion.dem import DEMSource

    grid = DEMSource(dem_path, target_dx_m=target_dx_m, aoi_bounds=aoi_bounds).load()

    if nodes is None:
        nodes = _auto_sink_nodes(grid, count=3)

    storm = storm or StormConfig(rainfall_mm_per_hr=90.0, duration_hours=2.0,
                                 drainage_capacity_mm_per_hr=15.0, drainage_blockage=0.5)
    # Scale the run so ~100 frames are produced regardless of grid size.
    config = SimConfig(
        storm=storm,
        solver=SolverConfig(cfl=0.7, total_time_s=7200.0, output_interval_s=72.0),
        aoi_name=aoi_name,
    )
    return Scenario(grid=grid, config=config, nodes=nodes)


def _auto_sink_nodes(grid: Grid, count: int = 3) -> list[SinkNode]:
    """Place sink nodes at the lowest in-AOI cells (illustrative low points)."""
    cells = [(grid.z[y][x], x, y)
             for y in range(grid.ny) for x in range(grid.nx) if grid.mask[y][x]]
    cells.sort(key=lambda c: c[0])
    nodes = []
    used: list[tuple[int, int]] = []
    for z, x, y in cells:
        if all(abs(x - ux) + abs(y - uy) > max(grid.nx, grid.ny) // 6 for ux, uy in used):
            used.append((x, y))
            nodes.append(SinkNode(f"Low Point {len(nodes)+1}", x=x, y=y,
                                  threshold_elevation=z + 0.2,
                                  opening_area_m2=4.0, capacity_m3=3000.0))
        if len(nodes) >= count:
            break
    return nodes


def _evaluate_alerts(scenario: Scenario, states):
    """Scan frames for hazard escalation and sink-node breaches.

    Returns ``(AlertLog, frame_breaches)`` where ``frame_breaches[i]`` is the list
    of active-breach objects for ``states[i]`` — the solver-side breach records
    embedded per frame, so a frame is self-contained proof of the event.
    """
    log = AlertLog()
    grid, nodes = scenario.grid, scenario.nodes
    warned_surface: set[str] = set()
    breached: set[str] = set()
    node_fill = {n.name: 0.0 for n in nodes}  # accumulated inflow volume (m^3)
    frame_breaches: list[list[dict]] = []
    prev_t = 0.0

    for state in states:
        dt = state.time_s - prev_t
        prev_t = state.time_s
        events: list[dict] = []
        for node in nodes:
            h = state.depth[node.y][node.x]
            if h <= _MIN_DEPTH:
                continue
            wse = grid.z[node.y][node.x] + h      # local water-surface elevation η
            head = wse - node.threshold_elevation  # hydraulic head over the entrance lip

            # Early warning: surface water is nearing the entrance lip.
            if (head >= -_WARN_APPROACH and node.name not in warned_surface
                    and node.name not in breached):
                warned_surface.add(node.name)
                log.add(state.time_s, Severity.WARNING, node.name,
                        f"Surface water approaching critical threshold at {node.name} "
                        f"(head {head:+.2f} m).")

            # Breach: water surface is above the entrance lip by a physical margin.
            if head > _BREACH_HEAD_EPS:
                q = orifice_inflow(node, wse)  # orifice discharge, m^3/s
                node_fill[node.name] = min(node.capacity_m3, node_fill[node.name] + q * dt)
                frac = node_fill[node.name] / node.capacity_m3 if node.capacity_m3 else 0.0
                events.append({
                    "breach_detected": True,
                    "node_id": node.name,
                    "water_surface_m": round(wse, 3),
                    "head_m": round(head, 3),
                    "inundation_rate_m3_s": round(q, 2),
                    "cumulative_volume_m3": round(node_fill[node.name], 1),
                    "fraction_full": round(frac, 3),
                })
                if node.name not in breached:
                    breached.add(node.name)
                    ttf = time_to_fill(node, wse)
                    eta = f" Est. full inundation in ~{ttf / 60:.0f} min." if ttf else ""
                    log.add(state.time_s, Severity.CRITICAL, node.name,
                            f"Breach detected at {node.name}.{eta}")
        frame_breaches.append(events)
    return log, frame_breaches


def run_scenario(scenario: Scenario, run_dir: str) -> dict:
    """Run the solver, evaluate alerts, and write the run folder. Returns manifest."""
    solver = ShallowWaterSolver(scenario.grid, scenario.config, boundary=scenario.boundary)
    states = list(solver.run())  # ~100 frames; small enough to hold in memory
    alerts, frame_breaches = _evaluate_alerts(scenario, states)
    return write_run(run_dir, scenario.grid, states, scenario.config,
                     alerts=alerts.to_records(), frame_breaches=frame_breaches,
                     scheme=scenario.config.solver.scheme)

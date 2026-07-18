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


class _AlertScanner:
    """Per-frame risk evaluation, built for streaming.

    ``scan(state)`` is called once per frame as the solver yields it; the
    scanner appends that frame's active-breach objects to ``frame_breaches``
    (which the exporter indexes as it writes) and accumulates the alert log.
    Holding only running accumulators — never the frames — keeps memory at
    O(one frame) for arbitrarily long runs.
    """

    def __init__(self, scenario: Scenario) -> None:
        self.grid = scenario.grid
        self.nodes = scenario.nodes
        # "Dry film" cutoff for risk logic, derived from the solver's own dry
        # threshold (one order of magnitude above it) so the two can never be
        # configured into contradiction.
        self.min_depth = 10.0 * scenario.config.solver.min_depth
        self.cell_area = scenario.grid.cell_area_m2()
        self.log = AlertLog()
        self.frame_breaches: list[list[dict]] = []
        self._warned: set[str] = set()
        self._breached: set[str] = set()
        self._node_fill = {n.name: 0.0 for n in scenario.nodes}
        self._prev_t = 0.0
        self._peak_hazard = HazardClass.NONE
        self._peak_hazard_time = 0.0

    def scan(self, state) -> None:
        grid = self.grid
        dt = state.time_s - self._prev_t
        self._prev_t = state.time_s
        # Track the worst depth-velocity hazard reached anywhere in the domain.
        for y in range(grid.ny):
            row_d, row_s = state.depth[y], state.speed[y]
            for x in range(grid.nx):
                hz = classify_hazard(row_d[x], row_s[x])
                if hz > self._peak_hazard:
                    self._peak_hazard, self._peak_hazard_time = hz, state.time_s

        events: list[dict] = []
        for node in self.nodes:
            h = state.depth[node.y][node.x]
            if h <= self.min_depth:
                continue
            wse = grid.z[node.y][node.x] + h      # local water-surface elevation η
            head = wse - node.threshold_elevation  # hydraulic head over the entrance lip
            breaching = head > _BREACH_HEAD_EPS

            # Early warning: water nearing the lip. Suppressed when this same
            # frame already breaches — a warning with zero lead time is noise.
            if (not breaching and head >= -_WARN_APPROACH
                    and node.name not in self._warned
                    and node.name not in self._breached):
                self._warned.add(node.name)
                self.log.add(state.time_s, Severity.WARNING, node.name,
                             f"Surface water approaching critical threshold at "
                             f"{node.name} (head {head:+.2f} m).")

            # Breach: water surface above the lip by a physical margin.
            if breaching:
                q = orifice_inflow(node, wse)  # orifice discharge, m^3/s
                if dt > 0:
                    # The node cannot ingest more water than the cell holds:
                    # cap the rate by the volume available over this interval.
                    q = min(q, h * self.cell_area / dt)
                    self._node_fill[node.name] = min(
                        node.capacity_m3, self._node_fill[node.name] + q * dt)
                fill = self._node_fill[node.name]
                frac = fill / node.capacity_m3 if node.capacity_m3 else 0.0
                events.append({
                    "breach_detected": True,
                    "node_id": node.name,
                    "water_surface_m": round(wse, 3),
                    "head_m": round(head, 3),
                    "inundation_rate_m3_s": round(q, 2),
                    "cumulative_volume_m3": round(fill, 1),
                    "fraction_full": round(frac, 3),
                })
                if node.name not in self._breached:
                    self._breached.add(node.name)
                    ttf = time_to_fill(node, wse)
                    eta = f" Est. full inundation in ~{ttf / 60:.0f} min." if ttf else ""
                    self.log.add(state.time_s, Severity.CRITICAL, node.name,
                                 f"Breach detected at {node.name}.{eta}")
        self.frame_breaches.append(events)

    def records(self) -> list[dict]:
        """Finalize: add the domain-wide hazard summary, return ranked records."""
        if self._peak_hazard >= HazardClass.SIGNIFICANT:
            self.log.add(self._peak_hazard_time, Severity.WARNING, "domain",
                         f"Peak surface-flow hazard reached {self._peak_hazard.name} "
                         f"(depth×velocity rating) at t={self._peak_hazard_time / 60:.0f} min.")
        return self.log.to_records()


def run_scenario(scenario: Scenario, run_dir: str) -> dict:
    """Run the solver, evaluate alerts, and write the run folder. Returns manifest.

    Fully streaming: each frame is risk-scanned and written as the solver yields
    it, so memory stays O(one frame) regardless of run length or grid size.
    """
    solver = ShallowWaterSolver(scenario.grid, scenario.config, boundary=scenario.boundary)
    scanner = _AlertScanner(scenario)

    def stream():
        for state in solver.run():
            scanner.scan(state)  # appends this frame's breaches before export reads them
            yield state

    nodes = [{"name": n.name, "x": n.x, "y": n.y,
              "threshold_elevation": round(n.threshold_elevation, 3)}
             for n in scenario.nodes]
    return write_run(run_dir, scenario.grid, stream(), scenario.config,
                     alerts=scanner.records,  # evaluated after the last frame
                     frame_breaches=scanner.frame_breaches, nodes=nodes)

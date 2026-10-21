"""The shallow-water solver — the scientific core (Phase 2).

Default scheme: **local-inertial** (LISFLOOD-FP / Bates, Horritt & Fewtrell 2010) —
the standard for 2D urban flood modeling. It keeps local (temporal) acceleration,
the pressure/gravity gradient, and Manning friction, while dropping convective
acceleration. It is well-balanced (preserves lake-at-rest over uneven terrain),
mass-conservative under closed boundaries, and robust at wet/dry fronts.

This is a real, dependency-free implementation (pure Python) so the whole
pipeline runs and is unit-tested at head. It is deliberately un-vectorized for
clarity; the production path swaps the inner loops for NumPy / Taichi (GPU) with
the same public interface (see docs/ARCHITECTURE.md §3).

Face flux (per unit width, m^2/s) between two cells, from Bates et al. 2010:

    q_{t+dt} = ( q_t - g * hflow * dt * dη/dx )
               / ( 1 + g * dt * n^2 * |q_t| / hflow^(7/3) )

with hflow = max(η_a, η_b) - max(z_a, z_b), η = z + h the water-surface elevation.
Depths update by mass balance from the four face fluxes plus rainfall / drainage.

Walls vs boundaries: interior obstacle cells and cells outside the geofence mask
are **always** no-flow walls, regardless of the domain boundary type — water
routes around buildings, never into them. The OPEN/CLOSED boundary setting
applies only to the outer edge of the domain.

Timestep: the semi-implicit friction term makes the scheme stable under the
celerity-based bound of Bates et al. (2010), Δt ≤ α·Δx/√(g·h_max), recomputed
every step (see physics.stability). The peak depth is tracked incrementally
during the depth update, so the bound costs nothing extra per step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from aqua_sim.config import GRAVITY, SimConfig
from aqua_sim.grid import Grid, Matrix, zeros
from aqua_sim.physics.boundary import BoundaryType
from aqua_sim.physics.stability import cfl_timestep

#: Solver schemes this module implements. SolverConfig.scheme is validated
#: against this so run provenance can never record a scheme that did not run.
SUPPORTED_SCHEMES = ("local_inertial",)

#: Sentinel "no barrier" crest — never wins a max(), so max(za, zb, NO_CREST)
#: is bit-identical to max(za, zb). Lets crests be passed unconditionally.
NO_CREST = float("-inf")


@dataclass
class FlowState:
    """The simulation state at one instant (a frame)."""

    time_s: float
    depth: Matrix                 # water depth h (m)
    speed: Matrix                 # per-cell flow speed (m/s), face-flux referenced
    max_depth: float              # peak depth in the domain (m)
    max_speed: float              # peak flow speed (m/s)
    total_volume_m3: float        # water volume in the domain (mass-balance check)


class ShallowWaterSolver:
    """Local-inertial 2D shallow-water solver over a :class:`Grid`.

    Usage:

        solver = ShallowWaterSolver(grid, config)
        for state in solver.run():
            ...  # export frame / feed the risk layer
    """

    def __init__(
        self,
        grid: Grid,
        config: SimConfig,
        boundary: BoundaryType = BoundaryType.OPEN,
    ) -> None:
        scheme = config.solver.scheme
        if scheme not in SUPPORTED_SCHEMES:
            raise NotImplementedError(
                f"solver scheme {scheme!r} is not implemented; "
                f"supported: {', '.join(SUPPORTED_SCHEMES)}"
            )
        self.grid = grid
        self.config = config
        self.boundary = boundary
        self.g = GRAVITY

        nx, ny = grid.nx, grid.ny
        self.h: Matrix = zeros(ny, nx)           # water depth
        # Face fluxes carry the inertial memory between steps.
        self.qx: Matrix = zeros(ny, nx + 1)      # x-faces: qx[y][i] left face of cell i
        self.qy: Matrix = zeros(ny + 1, nx)      # y-faces: qy[j][x] top face of cell j
        self._limit: Matrix = zeros(ny, nx)      # per-cell outflow limiter (scratch)
        # Static wall map: obstacle or outside-AOI cells are no-flow for the whole
        # run. Precomputed once — the flux loops index it directly.
        self._wall: list[list[bool]] = [
            [grid.obstacle[y][x] > 0.0 or not grid.mask[y][x] for x in range(nx)]
            for y in range(ny)
        ]
        self._max_h = 0.0        # peak depth, maintained incrementally by step()
        self._max_face_v = 0.0   # peak face velocity, tracked by _face_flux()
        self.time_s = 0.0

        # --- Phase 6A conditioned-surface inputs (all optional) --------------
        self._crest_x = grid.crest_x        # ny x (nx+1) face crests, or None
        self._crest_y = grid.crest_y        # (ny+1) x nx face crests, or None
        self._infil_rate = grid.infiltration_rate
        self._infil_cap = grid.infiltration_capacity
        self._drainage = grid.drainage
        self._connections = grid.connections or []
        # Per-cell sink path (infiltration or inlet drainage) — only taken when
        # conditioning is present, so a bare-DTM run stays bit-identical.
        self._per_cell_sink = (self._infil_rate is not None
                               or self._drainage is not None)
        self._cum_infil = (zeros(ny, nx) if self._infil_rate is not None else None)

    # -- setup ---------------------------------------------------------------

    def set_initial_depth(self, depth: Matrix) -> None:
        """Seed standing water (e.g. a dam-break block or a filled channel)."""
        self.h = [row[:] for row in depth]
        self._max_h = max((v for row in self.h for v in row), default=0.0)

    # -- source terms --------------------------------------------------------

    def _source_rate(self) -> float:
        """Net vertical rate (m/s): rainfall minus drainage.

        Rainfall follows the storm's hyetograph when one is set (historical
        events), otherwise constant intensity for the storm duration.
        """
        storm = self.config.storm
        return storm.rainfall_at(self.time_s) - storm.effective_drainage_m_per_s()

    # -- one timestep --------------------------------------------------------

    def _face_flux(self, za, ha, zb, hb, q_prev, n, dt, crest=NO_CREST) -> float:
        eta_a, eta_b = za + ha, zb + hb
        # A subgrid barrier (curb/median/retaining wall) raises the controlling
        # bed at this face: flow only once the water surface tops the crest.
        hflow = max(eta_a, eta_b) - max(za, zb, crest)
        if hflow <= self.config.solver.min_depth:
            return 0.0
        slope = (eta_b - eta_a) / self.grid.dx
        q = (q_prev - self.g * hflow * dt * slope) / (
            1.0 + self.g * dt * n * n * abs(q_prev) / hflow ** (7.0 / 3.0)
        )
        # Track the fastest face velocity (|q|/hflow — conveyance-referenced, so
        # physical at fronts) for the advective term of the CFL bound. Costs one
        # divide/compare in a computation already in hand.
        v = abs(q) / hflow
        if v > self._max_face_v:
            self._max_face_v = v
        return q

    def step(self, dt: float) -> None:
        nx, ny = self.grid.nx, self.grid.ny
        self._max_face_v = 0.0  # re-tracked by this step's flux updates
        z, man, h, wall = self.grid.z, self.grid.manning, self.h, self._wall
        open_edge = self.boundary is not BoundaryType.CLOSED

        # x-direction fluxes. Interior wall faces are always no-flow; only the
        # two domain-edge faces of each row honor the OPEN/CLOSED boundary.
        for y in range(ny):
            wy, zy, my, hy, qxy = wall[y], z[y], man[y], h[y], self.qx[y]
            cxy = self._crest_x[y] if self._crest_x is not None else None
            for i in range(nx + 1):
                left, right = i - 1, i
                a_edge, b_edge = left < 0, right >= nx
                a_blk = a_edge or wy[left]
                b_blk = b_edge or wy[right]
                if a_blk and b_blk:
                    qxy[i] = 0.0
                    continue
                if a_blk:
                    # Interior wall, or a closed domain edge: no flow. An OPEN
                    # domain edge gets a dry ghost cell at the live cell's bed.
                    if not (a_edge and open_edge):
                        qxy[i] = 0.0
                        continue
                    za, ha, n = zy[right], 0.0, my[right]
                    zb, hb = zy[right], hy[right]
                elif b_blk:
                    if not (b_edge and open_edge):
                        qxy[i] = 0.0
                        continue
                    za, ha = zy[left], hy[left]
                    zb, hb, n = zy[left], 0.0, my[left]
                else:  # interior face between two live cells
                    za, ha = zy[left], hy[left]
                    zb, hb = zy[right], hy[right]
                    n = 0.5 * (my[left] + my[right])
                crest = cxy[i] if cxy is not None else NO_CREST
                qxy[i] = self._face_flux(za, ha, zb, hb, qxy[i], n, dt, crest)

        # y-direction fluxes (same wall/boundary rules).
        for j in range(ny + 1):
            up, down = j - 1, j
            a_edge, b_edge = up < 0, down >= ny
            qyj = self.qy[j]
            cyj = self._crest_y[j] if self._crest_y is not None else None
            for x in range(nx):
                a_blk = a_edge or wall[up][x]
                b_blk = b_edge or wall[down][x]
                if a_blk and b_blk:
                    qyj[x] = 0.0
                    continue
                if a_blk:
                    if not (a_edge and open_edge):
                        qyj[x] = 0.0
                        continue
                    za, ha, n = z[down][x], 0.0, man[down][x]
                    zb, hb = z[down][x], h[down][x]
                elif b_blk:
                    if not (b_edge and open_edge):
                        qyj[x] = 0.0
                        continue
                    za, ha = z[up][x], h[up][x]
                    zb, hb, n = z[up][x], 0.0, man[up][x]
                else:
                    za, ha = z[up][x], h[up][x]
                    zb, hb = z[down][x], h[down][x]
                    n = 0.5 * (man[up][x] + man[down][x])
                crest = cyj[x] if cyj is not None else NO_CREST
                qyj[x] = self._face_flux(za, ha, zb, hb, qyj[x], n, dt, crest)

        # Flux limiter: cap each cell's total outflow at the water it holds so
        # depths never go negative. This keeps the scheme mass-conserving (each
        # face is scaled by its single donor cell) and robust at wet/dry fronts.
        dx = self.grid.dx
        for y in range(ny):
            wy, hy, ly = wall[y], h[y], self._limit[y]
            for x in range(nx):
                if wy[x]:
                    continue
                out = (max(-self.qx[y][x], 0.0) + max(self.qx[y][x + 1], 0.0)
                       + max(-self.qy[y][x], 0.0) + max(self.qy[y + 1][x], 0.0))
                if out <= 0.0:
                    ly[x] = 1.0
                    continue
                # Available depth can drain in dt: out * dt <= h * dx.
                ly[x] = min(1.0, hy[x] * dx / (out * dt))

        for y in range(ny):
            qxy, ly = self.qx[y], self._limit[y]
            for i in range(nx + 1):
                q = qxy[i]
                if q == 0.0:
                    continue
                donor = i - 1 if q > 0.0 else i
                if 0 <= donor < nx:
                    qxy[i] = q * ly[donor]
        for j in range(ny + 1):
            qyj = self.qy[j]
            for x in range(nx):
                q = qyj[x]
                if q == 0.0:
                    continue
                donor = j - 1 if q > 0.0 else j
                if 0 <= donor < ny:
                    qyj[x] = q * self._limit[donor][x]

        # Depth update by mass balance + source term (non-negative by
        # construction). Peak depth is tracked here so the CFL bound needs no
        # extra grid pass.
        inv_dx = 1.0 / dx
        max_h = 0.0
        if not self._per_cell_sink:
            # Original path (bare DTM): uniform rain minus scalar drainage.
            source = self._source_rate()
            for y in range(ny):
                wy, hy = wall[y], h[y]
                qx0, qy0, qy1 = self.qx[y], self.qy[y], self.qy[y + 1]
                for x in range(nx):
                    if wy[x]:
                        hy[x] = 0.0
                        continue
                    net = (qx0[x] - qx0[x + 1] + qy0[x] - qy1[x]) * inv_dx
                    new_h = hy[x] + (net + source) * dt
                    if new_h > 0.0:
                        hy[x] = new_h
                        if new_h > max_h:
                            max_h = new_h
                    else:
                        hy[x] = 0.0
        else:
            # Conditioned path: uniform rain in, then per-cell infiltration
            # (rate-limited, capped by remaining capacity), then per-cell inlet
            # drainage (falls back to the scalar storm drainage where absent).
            rain = self.config.storm.rainfall_at(self.time_s)
            scalar_drain = self.config.storm.effective_drainage_m_per_s()
            irate, icap, cum = self._infil_rate, self._infil_cap, self._cum_infil
            drain = self._drainage
            for y in range(ny):
                wy, hy = wall[y], h[y]
                qx0, qy0, qy1 = self.qx[y], self.qy[y], self.qy[y + 1]
                for x in range(nx):
                    if wy[x]:
                        hy[x] = 0.0
                        continue
                    net = (qx0[x] - qx0[x + 1] + qy0[x] - qy1[x]) * inv_dx
                    avail = hy[x] + (net + rain) * dt
                    if avail < 0.0:
                        avail = 0.0
                    if irate is not None:
                        cap_left = (icap[y][x] - cum[y][x]) if icap is not None else 1e18
                        if cap_left < 0.0:
                            cap_left = 0.0
                        inf = irate[y][x] * dt
                        if inf > cap_left:
                            inf = cap_left
                        if inf > avail:
                            inf = avail
                        avail -= inf
                        cum[y][x] += inf
                    d_rate = drain[y][x] if drain is not None else scalar_drain
                    drn = d_rate * dt
                    if drn > avail:
                        drn = avail
                    new_h = avail - drn
                    if new_h > 0.0:
                        hy[x] = new_h
                        if new_h > max_h:
                            max_h = new_h
                    else:
                        hy[x] = 0.0

        # Bridge/culvert conduits: move water between linked cells by head, so a
        # barrier/embankment with a culvert passes flow instead of false-damming.
        if self._connections:
            area = self.grid.cell_area_m2()
            md = self.config.solver.min_depth
            for (x1, y1, x2, y2, cd_area) in self._connections:
                eta1 = z[y1][x1] + h[y1][x1]
                eta2 = z[y2][x2] + h[y2][x2]
                dh = eta1 - eta2
                if dh > md:
                    dx1, dy1, dx2, dy2 = x1, y1, x2, y2  # donor is cell 1
                elif dh < -md:
                    dx1, dy1, dx2, dy2 = x2, y2, x1, y1  # donor is cell 2
                    dh = -dh
                else:
                    continue
                vol = cd_area * (2.0 * self.g * dh) ** 0.5 * dt   # orifice Q·dt
                avail_vol = h[dy1][dx1] * area
                if vol > avail_vol:
                    vol = avail_vol
                ddepth = vol / area
                h[dy1][dx1] -= ddepth
                h[dy2][dx2] += ddepth
                if h[dy2][dx2] > max_h:
                    max_h = h[dy2][dx2]

        self._max_h = max_h
        self.time_s += dt

    # -- driving loop --------------------------------------------------------

    def _face_speed(self, q, za, ha, zb, hb, crest=NO_CREST) -> float:
        """Flow speed carried by a face: |q| / hflow, with hflow the depth that
        actually conveys the flux (Bates et al. 2010). Referencing the face's
        own conveyance depth — not the receiving cell's — keeps reported speeds
        physical at wetting fronts, where dividing by a near-zero cell depth
        would explode. A crest raises the controlling bed (subgrid barrier)."""
        hflow = max(za + ha, zb + hb) - max(za, zb, crest)
        if hflow <= self.config.solver.min_depth:
            return 0.0
        return abs(q) / hflow

    def _snapshot(self) -> FlowState:
        nx, ny = self.grid.nx, self.grid.ny
        z, h = self.grid.z, self.h
        max_h = 0.0
        max_speed = 0.0
        total = 0.0
        area = self.grid.cell_area_m2()
        md = self.config.solver.min_depth
        speed = zeros(ny, nx)
        for y in range(ny):
            hy, zy = h[y], z[y]
            for x in range(nx):
                hv = hy[x]
                if hv > max_h:
                    max_h = hv
                total += hv * area
                if hv > md:
                    # Cell speed = fastest of its four faces, each referenced to
                    # that face's conveyance depth (crest-aware).
                    cx = self._crest_x
                    cy = self._crest_y
                    s = 0.0
                    if x > 0:
                        cr = cx[y][x] if cx is not None else NO_CREST
                        s = max(s, self._face_speed(self.qx[y][x], zy[x - 1], hy[x - 1], zy[x], hv, cr))
                    if x < nx - 1:
                        cr = cx[y][x + 1] if cx is not None else NO_CREST
                        s = max(s, self._face_speed(self.qx[y][x + 1], zy[x], hv, zy[x + 1], hy[x + 1], cr))
                    if y > 0:
                        cr = cy[y][x] if cy is not None else NO_CREST
                        s = max(s, self._face_speed(self.qy[y][x], z[y - 1][x], h[y - 1][x], zy[x], hv, cr))
                    if y < ny - 1:
                        cr = cy[y + 1][x] if cy is not None else NO_CREST
                        s = max(s, self._face_speed(self.qy[y + 1][x], zy[x], hv, z[y + 1][x], h[y + 1][x], cr))
                    speed[y][x] = s
                    if s > max_speed:
                        max_speed = s
        return FlowState(self.time_s, [row[:] for row in h], speed, max_h, max_speed, total)

    def adaptive_dt(self) -> float:
        """Largest stable timestep for the current state.

        Full CFL bound Δt ≤ α·Δx/(v_max + √(g·h_max)). The celerity term uses the
        peak depth tracked incrementally by ``step()``; the advective term uses
        the peak face velocity tracked by ``_face_flux()`` (conveyance-referenced,
        so physical at wetting fronts). The celerity-only bound of Bates et al.
        (2010) is stable for depth but admits transient velocity oscillations at
        α near 0.7; including the advective term suppresses them. No extra grid
        pass is needed — both maxima fall out of work the step already does.
        """
        return cfl_timestep(self.grid.dx, self._max_h, self._max_face_v,
                            cfl=self.config.solver.cfl)

    def run(self, initial_depth: Optional[Matrix] = None) -> Iterator[FlowState]:
        """Yield a :class:`FlowState` every ``output_interval_s`` up to ``total_time_s``.

        Yields the initial state first, then integrates with a CFL-adaptive
        timestep, snapshotting at fixed output times. The final state at
        ``total_time_s`` is always yielded, even when the total duration is not
        an exact multiple of the output interval.
        """
        if initial_depth is not None:
            self.set_initial_depth(initial_depth)

        solver_cfg = self.config.solver
        yield self._snapshot()
        last_yield_t = self.time_s

        next_out = solver_cfg.output_interval_s
        guard = 0
        max_steps = 5_000_000  # runaway backstop
        while self.time_s < solver_cfg.total_time_s and guard < max_steps:
            guard += 1
            dt = self.adaptive_dt()
            # Land exactly on the next output time / end time.
            dt = min(dt, next_out - self.time_s, solver_cfg.total_time_s - self.time_s)
            if dt <= 0:
                break
            self.step(dt)
            if self.time_s >= next_out - 1e-9:
                yield self._snapshot()
                last_yield_t = self.time_s
                next_out += solver_cfg.output_interval_s

        # Emit the final partial interval — the storm's endgame (often the peak)
        # must not be silently dropped when total_time_s % output_interval_s != 0.
        if self.time_s > last_yield_t + 1e-9:
            yield self._snapshot()

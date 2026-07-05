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
The timestep is CFL-limited and recomputed every step (see physics.stability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from aqua_sim.config import GRAVITY, SimConfig
from aqua_sim.grid import Grid
from aqua_sim.physics.boundary import BoundaryType
from aqua_sim.physics.stability import cfl_timestep

Matrix = list[list[float]]


@dataclass
class FlowState:
    """The simulation state at one instant (a frame)."""

    time_s: float
    depth: Matrix                 # water depth h (m)
    max_depth: float              # peak depth in the domain (m)
    max_speed: float              # peak flow speed (m/s)
    total_volume_m3: float        # water volume in the domain (mass-balance check)


def _zeros(ny: int, nx: int) -> Matrix:
    return [[0.0 for _ in range(nx)] for _ in range(ny)]


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
        self.grid = grid
        self.config = config
        self.boundary = boundary
        self.g = GRAVITY

        nx, ny = grid.nx, grid.ny
        self.h: Matrix = _zeros(ny, nx)          # water depth
        # Face fluxes carry the inertial memory between steps.
        self.qx: Matrix = _zeros(ny, nx + 1)     # x-faces: qx[y][i] left face of cell i
        self.qy: Matrix = _zeros(ny + 1, nx)     # y-faces: qy[j][x] top face of cell j
        self._limit: Matrix = _zeros(ny, nx)     # per-cell outflow limiter (scratch)
        self.time_s = 0.0

    # -- setup ---------------------------------------------------------------

    def set_initial_depth(self, depth: Matrix) -> None:
        """Seed standing water (e.g. a dam-break block or a filled channel)."""
        self.h = [row[:] for row in depth]

    # -- source terms --------------------------------------------------------

    def _source_rate(self) -> float:
        """Net vertical rate (m/s): rainfall minus drainage, active during the storm."""
        storm = self.config.storm
        if self.time_s <= storm.duration_hours * 3600.0:
            rain = storm.rainfall_m_per_s()
        else:
            rain = 0.0
        drain = storm.effective_drainage_mm_per_hr() / 1000.0 / 3600.0
        return rain - drain

    # -- one timestep --------------------------------------------------------

    def _blocked(self, x: int, y: int) -> bool:
        """A cell is a wall if it's an obstacle or outside the area of interest."""
        return self.grid.obstacle[y][x] > 0.0 or not self.grid.mask[y][x]

    def _face_flux(self, za, ha, zb, hb, q_prev, n, dt) -> float:
        eta_a, eta_b = za + ha, zb + hb
        hflow = max(eta_a, eta_b) - max(za, zb)
        if hflow <= self.config.solver.min_depth:
            return 0.0
        slope = (eta_b - eta_a) / self.grid.dx
        return (q_prev - self.g * hflow * dt * slope) / (
            1.0 + self.g * dt * n * n * abs(q_prev) / hflow ** (7.0 / 3.0)
        )

    def step(self, dt: float) -> None:
        nx, ny = self.grid.nx, self.grid.ny
        z, man, h = self.grid.z, self.grid.manning, self.h
        closed = self.boundary is BoundaryType.CLOSED

        # x-direction fluxes (including the two domain boundaries).
        for y in range(ny):
            for i in range(nx + 1):
                left = i - 1
                right = i
                a_wall = left < 0 or self._blocked(left, y)
                b_wall = right >= nx or self._blocked(right, y)
                if a_wall and b_wall:
                    self.qx[y][i] = 0.0
                    continue
                if a_wall:  # left boundary / wall: ghost = dry, same bed as b
                    if closed:
                        self.qx[y][i] = 0.0
                        continue
                    za, ha, n = z[y][right], 0.0, man[y][right]
                    zb, hb = z[y][right], h[y][right]
                elif b_wall:  # right boundary / wall
                    if closed:
                        self.qx[y][i] = 0.0
                        continue
                    za, ha = z[y][left], h[y][left]
                    zb, hb, n = z[y][left], 0.0, man[y][left]
                else:  # interior face between two live cells
                    za, ha = z[y][left], h[y][left]
                    zb, hb = z[y][right], h[y][right]
                    n = 0.5 * (man[y][left] + man[y][right])
                self.qx[y][i] = self._face_flux(za, ha, zb, hb, self.qx[y][i], n, dt)

        # y-direction fluxes.
        for j in range(ny + 1):
            up = j - 1
            down = j
            for x in range(nx):
                a_wall = up < 0 or self._blocked(x, up)
                b_wall = down >= ny or self._blocked(x, down)
                if a_wall and b_wall:
                    self.qy[j][x] = 0.0
                    continue
                if a_wall:
                    if closed:
                        self.qy[j][x] = 0.0
                        continue
                    za, ha, n = z[down][x], 0.0, man[down][x]
                    zb, hb = z[down][x], h[down][x]
                elif b_wall:
                    if closed:
                        self.qy[j][x] = 0.0
                        continue
                    za, ha = z[up][x], h[up][x]
                    zb, hb, n = z[up][x], 0.0, man[up][x]
                else:
                    za, ha = z[up][x], h[up][x]
                    zb, hb = z[down][x], h[down][x]
                    n = 0.5 * (man[up][x] + man[down][x])
                self.qy[j][x] = self._face_flux(za, ha, zb, hb, self.qy[j][x], n, dt)

        # Flux limiter: cap each cell's total outflow at the water it holds so
        # depths never go negative. This keeps the scheme mass-conserving (each
        # face is scaled by its single donor cell) and robust at wet/dry fronts.
        dx = self.grid.dx
        for y in range(ny):
            for x in range(nx):
                if self._blocked(x, y):
                    continue
                out = (max(-self.qx[y][x], 0.0) + max(self.qx[y][x + 1], 0.0)
                       + max(-self.qy[y][x], 0.0) + max(self.qy[y + 1][x], 0.0))
                if out <= 0.0:
                    self._limit[y][x] = 1.0
                    continue
                # Available depth can drain in dt: out * dt <= h * dx.
                self._limit[y][x] = min(1.0, h[y][x] * dx / (out * dt))

        for y in range(ny):
            for i in range(nx + 1):
                q = self.qx[y][i]
                if q == 0.0:
                    continue
                donor = i - 1 if q > 0.0 else i
                if 0 <= donor < nx:
                    self.qx[y][i] = q * self._limit[y][donor]
        for j in range(ny + 1):
            for x in range(nx):
                q = self.qy[j][x]
                if q == 0.0:
                    continue
                donor = j - 1 if q > 0.0 else j
                if 0 <= donor < ny:
                    self.qy[j][x] = q * self._limit[donor][x]

        # Depth update by mass balance + source term (non-negative by construction).
        source = self._source_rate()
        inv_dx = 1.0 / dx
        for y in range(ny):
            for x in range(nx):
                if self._blocked(x, y):
                    h[y][x] = 0.0
                    continue
                net = (self.qx[y][x] - self.qx[y][x + 1]
                       + self.qy[y][x] - self.qy[y + 1][x]) * inv_dx
                new_h = h[y][x] + (net + source) * dt
                h[y][x] = new_h if new_h > 0.0 else 0.0

        self.time_s += dt

    # -- driving loop --------------------------------------------------------

    def _snapshot(self) -> FlowState:
        max_h = 0.0
        max_speed = 0.0
        total = 0.0
        area = self.grid.cell_area_m2()
        md = self.config.solver.min_depth
        for y in range(self.grid.ny):
            for x in range(self.grid.nx):
                hv = self.h[y][x]
                if hv > max_h:
                    max_h = hv
                total += hv * area
                if hv > md:
                    # Estimate speed from the larger adjacent face flux.
                    q = max(abs(self.qx[y][x]), abs(self.qx[y][x + 1]),
                            abs(self.qy[y][x]), abs(self.qy[y + 1][x]))
                    s = q / hv
                    if s > max_speed:
                        max_speed = s
        return FlowState(self.time_s, [row[:] for row in self.h], max_h, max_speed, total)

    def adaptive_dt(self) -> float:
        state_max_h = max((v for row in self.h for v in row), default=0.0)
        # Speed estimate for the CFL bound.
        max_speed = 0.0
        md = self.config.solver.min_depth
        for y in range(self.grid.ny):
            for x in range(self.grid.nx):
                hv = self.h[y][x]
                if hv > md:
                    q = max(abs(self.qx[y][x]), abs(self.qx[y][x + 1]),
                            abs(self.qy[y][x]), abs(self.qy[y + 1][x]))
                    max_speed = max(max_speed, q / hv)
        return cfl_timestep(self.grid.dx, state_max_h, max_speed, cfl=self.config.solver.cfl)

    def run(self, initial_depth: Optional[Matrix] = None) -> Iterator[FlowState]:
        """Yield a :class:`FlowState` every ``output_interval_s`` up to ``total_time_s``.

        Yields the initial state first, then integrates with a CFL-adaptive
        timestep, snapshotting at fixed output times.
        """
        if initial_depth is not None:
            self.set_initial_depth(initial_depth)

        solver_cfg = self.config.solver
        yield self._snapshot()

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
                next_out += solver_cfg.output_interval_s

"""NumPy-vectorized local-inertial solver — the fast backend.

Same physics, same interface, same results as the pure-Python reference
(``physics.swe.ShallowWaterSolver``): local-inertial face fluxes (Bates et al.
2010), interior walls unconditionally no-flow, OPEN/CLOSED domain edges, the
donor-cell flux limiter, CFL from tracked peak depth + peak face velocity, and
face-conveyance-referenced speeds. Equivalence is enforced by
``tests/test_swe_numpy.py``, which drives both solvers through identical
scenarios and requires cell-level agreement to floating-point tolerance.

This backend exists for scale: the reference solver is the readable
specification; this one runs city-scale grids (10^6+ cells) in minutes instead
of hours. Requires numpy (part of the ``geo`` extra); the engine falls back to
the reference solver without it.
"""

from __future__ import annotations

from typing import Iterator, Optional

from aqua_sim.config import GRAVITY, SimConfig
from aqua_sim.grid import Grid, Matrix
from aqua_sim.physics.boundary import BoundaryType
from aqua_sim.physics.stability import cfl_timestep
from aqua_sim.physics.swe import NO_CREST, SUPPORTED_SCHEMES, FlowState


class NumpyShallowWaterSolver:
    """Vectorized twin of :class:`aqua_sim.physics.swe.ShallowWaterSolver`."""

    def __init__(
        self,
        grid: Grid,
        config: SimConfig,
        boundary: BoundaryType = BoundaryType.OPEN,
    ) -> None:
        import numpy as np
        self.np = np

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
        self.nx, self.ny = nx, ny
        self.dx = grid.dx
        self.z = np.asarray(grid.z, dtype=np.float64)
        self.man = np.asarray(grid.manning, dtype=np.float64)
        self.wall = (np.asarray(grid.obstacle, dtype=np.float64) > 0.0) | \
            ~np.asarray(grid.mask, dtype=bool)
        self.h = np.zeros((ny, nx))
        self.qx = np.zeros((ny, nx + 1))
        self.qy = np.zeros((ny + 1, nx))
        # Precomputed face-activity masks: a face is dead if both sides are
        # blocked, or one side is an *interior* wall (edges handled per-step).
        wall = self.wall
        self._xface_live = ~(wall[:, :-1] | wall[:, 1:])      # (ny, nx-1) interior x-faces
        self._yface_live = ~(wall[:-1, :] | wall[1:, :])      # (ny-1, nx) interior y-faces
        self._open = boundary is not BoundaryType.CLOSED
        self._max_h = 0.0
        self._max_face_v = 0.0
        self.time_s = 0.0

        # --- Phase 6A conditioned-surface inputs (all optional) --------------
        self._crest_x = None if grid.crest_x is None else np.asarray(grid.crest_x, float)
        self._crest_y = None if grid.crest_y is None else np.asarray(grid.crest_y, float)
        self._infil_rate = None if grid.infiltration_rate is None \
            else np.asarray(grid.infiltration_rate, float)
        self._infil_cap = None if grid.infiltration_capacity is None \
            else np.asarray(grid.infiltration_capacity, float)
        self._drainage = None if grid.drainage is None else np.asarray(grid.drainage, float)
        self._connections = grid.connections or []
        self._per_cell_sink = (self._infil_rate is not None or self._drainage is not None)
        self._cum_infil = (np.zeros((ny, nx)) if self._infil_rate is not None else None)

    # -- setup ---------------------------------------------------------------

    def set_initial_depth(self, depth: Matrix) -> None:
        # Mirrors the reference exactly: wall cells are zeroed by the first
        # step's depth update, not at seed time.
        np = self.np
        self.h = np.asarray(depth, dtype=np.float64).copy()
        self._max_h = float(self.h.max()) if self.h.size else 0.0

    # -- source terms --------------------------------------------------------

    def _source_rate(self) -> float:
        storm = self.config.storm
        return storm.rainfall_at(self.time_s) - storm.effective_drainage_m_per_s()

    # -- one timestep --------------------------------------------------------

    def _flux(self, za, ha, zb, hb, q_prev, n, dt, live, crest=NO_CREST):
        """Vectorized local-inertial face flux; returns (q_new, v_face).

        ``crest`` (scalar NO_CREST or an array) raises the controlling bed for
        subgrid barriers; NO_CREST is bit-identical to no crest.
        """
        np = self.np
        eta_a, eta_b = za + ha, zb + hb
        hflow = np.maximum(eta_a, eta_b) - np.maximum(np.maximum(za, zb), crest)
        active = live & (hflow > self.config.solver.min_depth)
        hf = np.where(active, hflow, 1.0)  # safe denominator
        slope = (eta_b - eta_a) / self.dx
        q_new = (q_prev - self.g * hf * dt * slope) / (
            1.0 + self.g * dt * n * n * np.abs(q_prev) / hf ** (7.0 / 3.0)
        )
        q_new = np.where(active, q_new, 0.0)
        v = np.where(active, np.abs(q_new) / hf, 0.0)
        return q_new, v

    def step(self, dt: float) -> None:
        np = self.np
        z, man, h, wall = self.z, self.man, self.h, self.wall
        nx, ny = self.nx, self.ny
        max_face_v = 0.0

        cx, cy = self._crest_x, self._crest_y
        # Interior x-faces (between columns i-1 and i → qx[:, 1:nx]).
        n_x = 0.5 * (man[:, :-1] + man[:, 1:])
        qx_new, vx = self._flux(z[:, :-1], h[:, :-1], z[:, 1:], h[:, 1:],
                                self.qx[:, 1:nx], n_x, dt, self._xface_live,
                                cx[:, 1:nx] if cx is not None else NO_CREST)
        self.qx[:, 1:nx] = qx_new
        if vx.size:
            max_face_v = max(max_face_v, float(vx.max()))

        # Interior y-faces.
        n_y = 0.5 * (man[:-1, :] + man[1:, :])
        qy_new, vy = self._flux(z[:-1, :], h[:-1, :], z[1:, :], h[1:, :],
                                self.qy[1:ny, :], n_y, dt, self._yface_live,
                                cy[1:ny, :] if cy is not None else NO_CREST)
        self.qy[1:ny, :] = qy_new
        if vy.size:
            max_face_v = max(max_face_v, float(vy.max()))

        # Domain-edge faces: ghost dry cell at the live cell's bed under OPEN;
        # zero under CLOSED. Wall cells at the edge stay no-flow.
        if self._open:
            live_l = ~wall[:, 0]
            q, v = self._flux(z[:, 0], np.zeros(ny), z[:, 0], h[:, 0],
                              self.qx[:, 0], man[:, 0], dt, live_l,
                              cx[:, 0] if cx is not None else NO_CREST)
            self.qx[:, 0] = q
            if v.size:
                max_face_v = max(max_face_v, float(v.max()))
            live_r = ~wall[:, -1]
            q, v = self._flux(z[:, -1], h[:, -1], z[:, -1], np.zeros(ny),
                              self.qx[:, nx], man[:, -1], dt, live_r,
                              cx[:, nx] if cx is not None else NO_CREST)
            self.qx[:, nx] = q
            if v.size:
                max_face_v = max(max_face_v, float(v.max()))
            live_t = ~wall[0, :]
            q, v = self._flux(z[0, :], np.zeros(nx), z[0, :], h[0, :],
                              self.qy[0, :], man[0, :], dt, live_t,
                              cy[0, :] if cy is not None else NO_CREST)
            self.qy[0, :] = q
            if v.size:
                max_face_v = max(max_face_v, float(v.max()))
            live_b = ~wall[-1, :]
            q, v = self._flux(z[-1, :], h[-1, :], z[-1, :], np.zeros(nx),
                              self.qy[ny, :], man[-1, :], dt, live_b,
                              cy[ny, :] if cy is not None else NO_CREST)
            self.qy[ny, :] = q
            if v.size:
                max_face_v = max(max_face_v, float(v.max()))
        else:
            self.qx[:, 0] = 0.0
            self.qx[:, nx] = 0.0
            self.qy[0, :] = 0.0
            self.qy[ny, :] = 0.0

        # Donor-cell flux limiter: cap each cell's outflow at its water volume.
        out = (np.maximum(-self.qx[:, :-1], 0.0) + np.maximum(self.qx[:, 1:], 0.0)
               + np.maximum(-self.qy[:-1, :], 0.0) + np.maximum(self.qy[1:, :], 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            limit = np.where(out > 0.0, np.minimum(1.0, h * self.dx / (out * dt)), 1.0)
        limit[wall] = 1.0  # wall faces are already zero; keep scaling neutral

        # Scale each face by its donor cell's limit (ghost donors unscaled).
        Ll = np.ones((ny, nx + 1)); Ll[:, 1:] = limit   # donor = left cell
        Lr = np.ones((ny, nx + 1)); Lr[:, :nx] = limit  # donor = right cell
        self.qx = np.where(self.qx > 0.0, self.qx * Ll, self.qx * Lr)
        Lt = np.ones((ny + 1, nx)); Lt[1:, :] = limit   # donor = upper cell
        Lb = np.ones((ny + 1, nx)); Lb[:ny, :] = limit  # donor = lower cell
        self.qy = np.where(self.qy > 0.0, self.qy * Lt, self.qy * Lb)

        # Depth update by mass balance + source; non-negative; walls stay dry.
        # Match the reference's float association (net * inv_dx, hoisted):
        # bit-level agreement can't be guaranteed across scalar/vector pow, but
        # matching association keeps free-run divergence to ulp-seeded noise.
        inv_dx = 1.0 / self.dx
        net = (self.qx[:, :-1] - self.qx[:, 1:]
               + self.qy[:-1, :] - self.qy[1:, :]) * inv_dx
        if not self._per_cell_sink:
            h_new = h + (net + self._source_rate()) * dt
        else:
            # Conditioned path — same op order as the reference: rain in, then
            # capacity-limited infiltration, then per-cell (or scalar) drainage.
            rain = self.config.storm.rainfall_at(self.time_s)
            scalar_drain = self.config.storm.effective_drainage_m_per_s()
            avail = h + (net + rain) * dt
            np.maximum(avail, 0.0, out=avail)
            if self._infil_rate is not None:
                if self._infil_cap is not None:
                    cap_left = np.maximum(self._infil_cap - self._cum_infil, 0.0)
                else:
                    cap_left = np.inf
                inf = np.minimum(np.minimum(self._infil_rate * dt, cap_left), avail)
                inf = np.where(wall, 0.0, inf)   # walls never infiltrate (== reference)
                avail = avail - inf
                self._cum_infil = self._cum_infil + inf
            d_rate = self._drainage if self._drainage is not None else scalar_drain
            h_new = avail - np.minimum(d_rate * dt, avail)
        np.maximum(h_new, 0.0, out=h_new)
        h_new[wall] = 0.0
        self.h = h_new
        max_h = float(h_new.max()) if h_new.size else 0.0

        # Bridge/culvert conduits (sequential, mutating self.h — matches ref).
        if self._connections:
            area = self.grid.cell_area_m2()
            md = self.config.solver.min_depth
            hh, zz = self.h, self.z
            for (x1, y1, x2, y2, cd_area) in self._connections:
                dh = (zz[y1, x1] + hh[y1, x1]) - (zz[y2, x2] + hh[y2, x2])
                if dh > md:
                    dx1, dy1, dx2, dy2 = x1, y1, x2, y2
                elif dh < -md:
                    dx1, dy1, dx2, dy2 = x2, y2, x1, y1
                    dh = -dh
                else:
                    continue
                vol = cd_area * (2.0 * self.g * dh) ** 0.5 * dt
                avail_vol = hh[dy1, dx1] * area
                if vol > avail_vol:
                    vol = avail_vol
                ddepth = vol / area
                hh[dy1, dx1] -= ddepth
                hh[dy2, dx2] += ddepth
                if hh[dy2, dx2] > max_h:
                    max_h = float(hh[dy2, dx2])

        self._max_h = max_h
        self._max_face_v = max_face_v
        self.time_s += dt

    # -- driving loop --------------------------------------------------------

    def _snapshot(self) -> FlowState:
        np = self.np
        z, h = self.z, self.h
        nx, ny = self.nx, self.ny
        md = self.config.solver.min_depth

        # Face conveyance depth recomputed from the current state (interior
        # faces only — edge cells ignore their boundary faces, matching the
        # reference).
        def face_v(qa, za, ha, zb, hb, crest=NO_CREST):
            hflow = np.maximum(za + ha, zb + hb) - np.maximum(np.maximum(za, zb), crest)
            ok = hflow > md
            return np.where(ok, np.abs(qa) / np.where(ok, hflow, 1.0), 0.0)

        cx, cy = self._crest_x, self._crest_y
        vx = face_v(self.qx[:, 1:nx], z[:, :-1], h[:, :-1], z[:, 1:], h[:, 1:],
                    cx[:, 1:nx] if cx is not None else NO_CREST)
        vy = face_v(self.qy[1:ny, :], z[:-1, :], h[:-1, :], z[1:, :], h[1:, :],
                    cy[1:ny, :] if cy is not None else NO_CREST)
        # Cell speed = max over adjacent interior faces.
        speed = np.zeros((ny, nx))
        if nx > 1:
            speed[:, 1:] = np.maximum(speed[:, 1:], vx)   # left face of cells 1..
            speed[:, :-1] = np.maximum(speed[:, :-1], vx)  # right face of cells ..nx-2
        if ny > 1:
            speed[1:, :] = np.maximum(speed[1:, :], vy)
            speed[:-1, :] = np.maximum(speed[:-1, :], vy)
        speed[h <= md] = 0.0

        total = float(h.sum()) * self.grid.cell_area_m2()
        max_h = float(h.max()) if h.size else 0.0
        max_s = float(speed.max()) if speed.size else 0.0
        # Full precision, like the reference — the export layer does rounding.
        return FlowState(self.time_s, h.tolist(), speed.tolist(),
                         max_h, max_s, total)

    def adaptive_dt(self) -> float:
        return cfl_timestep(self.dx, self._max_h, self._max_face_v,
                            cfl=self.config.solver.cfl)

    def run(self, initial_depth: Optional[Matrix] = None) -> Iterator[FlowState]:
        """Identical driving loop to the reference solver."""
        if initial_depth is not None:
            self.set_initial_depth(initial_depth)

        solver_cfg = self.config.solver
        yield self._snapshot()
        last_yield_t = self.time_s

        next_out = solver_cfg.output_interval_s
        guard = 0
        max_steps = 5_000_000
        while self.time_s < solver_cfg.total_time_s and guard < max_steps:
            guard += 1
            dt = self.adaptive_dt()
            dt = min(dt, next_out - self.time_s, solver_cfg.total_time_s - self.time_s)
            if dt <= 0:
                break
            self.step(dt)
            if self.time_s >= next_out - 1e-9:
                yield self._snapshot()
                last_yield_t = self.time_s
                next_out += solver_cfg.output_interval_s

        if self.time_s > last_yield_t + 1e-9:
            yield self._snapshot()


def make_solver(grid: Grid, config: SimConfig,
                boundary: BoundaryType = BoundaryType.OPEN,
                backend: str = "auto"):
    """Pick a solver backend: 'auto' uses numpy when available, else reference."""
    if backend not in ("auto", "numpy", "reference"):
        raise ValueError(f"unknown backend {backend!r}")
    if backend in ("auto", "numpy"):
        try:
            import numpy  # noqa: F401
            return NumpyShallowWaterSolver(grid, config, boundary)
        except ImportError:
            if backend == "numpy":
                raise
    from aqua_sim.physics.swe import ShallowWaterSolver
    return ShallowWaterSolver(grid, config, boundary)

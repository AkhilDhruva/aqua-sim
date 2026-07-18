"""Configuration objects for a simulation run.

Plain dataclasses, no external dependencies (Phase 0). These describe *what* to
simulate; the engine (Phase 2+) consumes them. Units are stated explicitly on
every field because unit confusion is a top source of physical error.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Physical constant.
GRAVITY = 9.81  # m/s^2


@dataclass(frozen=True)
class StormConfig:
    """A rainfall event to drive the simulation.

    Rainfall is either a constant intensity (``rainfall_mm_per_hr`` over
    ``duration_hours``) or, for historical events, a **hyetograph**: a tuple of
    ``(start_hour, mm_per_hr)`` steps, each intensity holding from its start
    hour until the next step's start hour (the last step holds until
    ``duration_hours``). When a hyetograph is given it takes precedence and
    ``rainfall_mm_per_hr`` is only used as a headline/summary figure.
    """

    rainfall_mm_per_hr: float = 50.0   # rainfall intensity R (constant mode)
    duration_hours: float = 2.0        # how long rain falls
    drainage_capacity_mm_per_hr: float = 10.0  # storm-drain sink D at full capacity
    drainage_blockage: float = 0.0     # 0.0 = clear drains, 1.0 = fully clogged
    hyetograph: tuple[tuple[float, float], ...] | None = None  # ((start_hr, mm/hr), ...)

    def effective_drainage_mm_per_hr(self) -> float:
        """Drainage sink after applying the blockage slider."""
        return self.drainage_capacity_mm_per_hr * (1.0 - self.drainage_blockage)

    def rainfall_m_per_s(self) -> float:
        """Convert user-facing mm/hr into SI m/s for the solver source term."""
        return self.rainfall_mm_per_hr / 1000.0 / 3600.0

    def rainfall_at(self, time_s: float) -> float:
        """Rainfall rate (m/s) at simulation time ``time_s``.

        Constant-intensity storms rain at ``rainfall_mm_per_hr`` until
        ``duration_hours``; hyetograph storms step through their
        ``(start_hour, mm_per_hr)`` series. Zero after the storm ends.
        """
        hours = time_s / 3600.0
        if hours > self.duration_hours:
            return 0.0
        if self.hyetograph is None:
            return self.rainfall_m_per_s()
        rate_mm_hr = 0.0
        for start_hr, mm_per_hr in self.hyetograph:
            if hours >= start_hr:
                rate_mm_hr = mm_per_hr
            else:
                break
        return rate_mm_hr / 1000.0 / 3600.0

    def total_rainfall_mm(self) -> float:
        """Total event rainfall (mm) — a sanity figure for validation reports."""
        if self.hyetograph is None:
            return self.rainfall_mm_per_hr * self.duration_hours
        total = 0.0
        steps = list(self.hyetograph) + [(self.duration_hours, 0.0)]
        for (t0, rate), (t1, _) in zip(steps, steps[1:]):
            total += rate * max(min(t1, self.duration_hours) - t0, 0.0)
        return total

    def effective_drainage_m_per_s(self) -> float:
        """Blockage-adjusted drainage sink in SI m/s — both source terms convert
        through the same mm/hr→m/s path so the units cannot drift apart."""
        return self.effective_drainage_mm_per_hr() / 1000.0 / 3600.0


@dataclass(frozen=True)
class SolverConfig:
    """Numerical settings for the shallow-water solver."""

    cfl: float = 0.7           # Courant number (0 < cfl <= 1); safety on the timestep
    min_depth: float = 1e-4    # m; below this a cell is treated as dry
    total_time_s: float = 3600.0
    output_interval_s: float = 30.0
    scheme: str = "local_inertial"  # or "dynamic_swe" (high-fidelity mode)


@dataclass
class SimConfig:
    """Top-level run configuration."""

    storm: StormConfig = field(default_factory=StormConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    aoi_name: str = "unnamed-area-of-interest"

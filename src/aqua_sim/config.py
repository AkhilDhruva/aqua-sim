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
    """A rainfall event to drive the simulation."""

    rainfall_mm_per_hr: float = 50.0   # rainfall intensity R
    duration_hours: float = 2.0        # how long rain falls
    drainage_capacity_mm_per_hr: float = 10.0  # storm-drain sink D at full capacity
    drainage_blockage: float = 0.0     # 0.0 = clear drains, 1.0 = fully clogged

    def effective_drainage_mm_per_hr(self) -> float:
        """Drainage sink after applying the blockage slider."""
        return self.drainage_capacity_mm_per_hr * (1.0 - self.drainage_blockage)

    def rainfall_m_per_s(self) -> float:
        """Convert user-facing mm/hr into SI m/s for the solver source term."""
        return self.rainfall_mm_per_hr / 1000.0 / 3600.0


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

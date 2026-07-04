"""Numerical stability — the CFL condition.

Explicit shallow-water solvers are unstable unless the timestep is short enough
that a surface-gravity wave crosses at most one cell per step. This was absent
from the original design note and is the #1 cause of a flood solver "exploding".

    dt <= cfl * dx / (|u| + sqrt(g * h))          0 < cfl <= 1

The solver recomputes the stable dt every step from current depths/velocities
(adaptive timestepping). Real function, unit-tested — part of the Phase 0 seed.
"""

from __future__ import annotations

import math

from aqua_sim.config import GRAVITY


def cfl_timestep(
    dx: float,
    max_depth: float,
    max_speed: float = 0.0,
    cfl: float = 0.7,
    g: float = GRAVITY,
    max_dt: float = 60.0,
) -> float:
    """Largest stable timestep (seconds) for the given state.

    Args:
        dx: cell size in meters.
        max_depth: maximum water depth in the domain (m).
        max_speed: maximum flow speed |(u, v)| in the domain (m/s).
        cfl: Courant number in (0, 1]; lower is safer/slower.
        g: gravitational acceleration.
        max_dt: cap for the near-dry case where the wave speed -> 0.

    Returns:
        A positive timestep in seconds, never exceeding ``max_dt``.
    """
    if dx <= 0:
        raise ValueError("dx must be positive")
    if not (0 < cfl <= 1):
        raise ValueError("cfl must be in (0, 1]")

    wave_speed = math.sqrt(g * max(max_depth, 0.0))
    total_speed = max_speed + wave_speed
    if total_speed <= 0.0:
        # No water / no motion: no stability constraint, fall back to the cap.
        return max_dt
    return min(cfl * dx / total_speed, max_dt)

"""Manning friction — surfaces slow water down by different amounts.

Concrete streets move water fast; grass and soil resist it. The Manning
formulation relates flow to the roughness coefficient ``n``, depth, and the
water-surface slope. Real function, unit-tested — part of the Phase 0 seed.
"""

from __future__ import annotations

import math

# Representative Manning's n by surface (s/m^(1/3)); used to build the grid layer.
MANNING_BY_SURFACE = {
    "concrete": 0.013,
    "asphalt": 0.016,
    "bare_soil": 0.025,
    "grass": 0.035,
    "dense_vegetation": 0.08,
    "open_water": 0.030,
}


def manning_velocity(depth: float, slope: float, n: float) -> float:
    """Steady-state Manning velocity magnitude (m/s) for sheet flow.

        v = (1 / n) * h^(2/3) * sqrt(S)

    Args:
        depth: water depth h (m). For sheet flow the hydraulic radius ~= depth.
        slope: water-surface slope S (dimensionless, magnitude).
        n: Manning's roughness coefficient.

    Returns:
        Velocity magnitude (m/s); 0 for a dry or flat cell.
    """
    if n <= 0:
        raise ValueError("Manning's n must be positive")
    if depth <= 0 or slope <= 0:
        return 0.0
    return (1.0 / n) * depth ** (2.0 / 3.0) * math.sqrt(slope)

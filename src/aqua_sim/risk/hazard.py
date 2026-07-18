"""Flood hazard classification.

Danger to people and vehicles is governed by **depth x velocity**, not depth
alone: shallow fast-moving water sweeps you off your feet; deep still water does
not. We use a depth-velocity hazard rating in the style of the UK FD2320 work:

    HR = d * (v + 0.5)

with band thresholds mapping HR to a severity class. Real function, unit-tested.
"""

from __future__ import annotations

from enum import IntEnum

#: Debris factor DF in HR = d·(v + DF). Exported into every run manifest so the
#: viewer's shader uses the engine's value — never a hard-coded copy.
DEBRIS_FACTOR = 0.5

#: Upper HR bound of each band (exclusive). Above 'significant' is EXTREME.
HR_BANDS = {"low": 0.75, "moderate": 1.25, "significant": 2.0}

#: Still-water depth treated as critical for infrastructure (subway/basement
#: ingress, vehicle stall) independent of velocity. Display guidance for the
#: viewer's danger normalization; also exported in the manifest.
DEPTH_CRITICAL_M = 0.5


class HazardClass(IntEnum):
    """Severity bands, ordered so higher == more dangerous."""

    NONE = 0
    LOW = 1
    MODERATE = 2
    SIGNIFICANT = 3
    EXTREME = 4


def hazard_rating(depth: float, speed: float, debris_factor: float = DEBRIS_FACTOR) -> float:
    """Depth-velocity hazard rating HR = d * (v + DF)."""
    if depth <= 0:
        return 0.0
    return depth * (max(speed, 0.0) + debris_factor)


def classify_hazard(depth: float, speed: float, debris_factor: float = DEBRIS_FACTOR) -> HazardClass:
    """Map a cell's depth and speed to a :class:`HazardClass`.

    Band thresholds come from :data:`HR_BANDS`; a dry cell is NONE regardless
    of the formula.
    """
    if depth <= 0:
        return HazardClass.NONE
    hr = hazard_rating(depth, speed, debris_factor)
    if hr < HR_BANDS["low"]:
        return HazardClass.LOW
    if hr < HR_BANDS["moderate"]:
        return HazardClass.MODERATE
    if hr < HR_BANDS["significant"]:
        return HazardClass.SIGNIFICANT
    return HazardClass.EXTREME

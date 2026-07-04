"""Flood hazard classification.

Danger to people and vehicles is governed by **depth x velocity**, not depth
alone: shallow fast-moving water sweeps you off your feet; deep still water does
not. We use a depth-velocity hazard rating in the style of the UK FD2320 work:

    HR = d * (v + 0.5)

with band thresholds mapping HR to a severity class. Real function, unit-tested.
"""

from __future__ import annotations

from enum import IntEnum


class HazardClass(IntEnum):
    """Severity bands, ordered so higher == more dangerous."""

    NONE = 0
    LOW = 1
    MODERATE = 2
    SIGNIFICANT = 3
    EXTREME = 4


def hazard_rating(depth: float, speed: float, debris_factor: float = 0.5) -> float:
    """Depth-velocity hazard rating HR = d * (v + DF)."""
    if depth <= 0:
        return 0.0
    return depth * (max(speed, 0.0) + debris_factor)


def classify_hazard(depth: float, speed: float, debris_factor: float = 0.5) -> HazardClass:
    """Map a cell's depth and speed to a :class:`HazardClass`.

    Band thresholds (HR): <0.75 low, <1.25 moderate, <2.0 significant, else extreme.
    A dry cell is NONE regardless of the formula.
    """
    if depth <= 0:
        return HazardClass.NONE
    hr = hazard_rating(depth, speed, debris_factor)
    if hr < 0.75:
        return HazardClass.LOW
    if hr < 1.25:
        return HazardClass.MODERATE
    if hr < 2.0:
        return HazardClass.SIGNIFICANT
    return HazardClass.EXTREME

"""Subterranean sink nodes — subways, underpasses, basements, substations.

The original note said to "instantly calculate the volume pouring in". Instant
inundation is physically meaningless — real inflow is throttled by the size of
the opening. We model each node as an orifice: once surface water rises above the
node's threshold, inflow follows

    Q_in = Cd * A * sqrt(2 * g * head)

capped by the node's storage capacity. That yields a credible *fill time*
("Transit Node 4 fully inundated in ~14 min") rather than an instantaneous dump.
Real function, unit-tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aqua_sim.config import GRAVITY


@dataclass
class SinkNode:
    """A point where surface water can pour into a subterranean space.

    Attributes:
        name: human-readable label used in alerts.
        x, y: grid indices of the node.
        threshold_elevation: water-surface elevation (m) above which inflow starts.
        opening_area_m2: effective opening area A (m^2).
        discharge_coeff: orifice discharge coefficient Cd (~0.6 for a sharp edge).
        capacity_m3: below-ground volume that can flood before it is "full".
    """

    name: str
    x: int
    y: int
    threshold_elevation: float
    opening_area_m2: float
    discharge_coeff: float = 0.6
    capacity_m3: float = 1000.0


def orifice_inflow(node: SinkNode, water_surface_elevation: float, g: float = GRAVITY) -> float:
    """Inflow rate (m^3/s) into a sink node given the surface water elevation.

    Returns 0 while the water surface is at or below the node threshold.
    """
    head = water_surface_elevation - node.threshold_elevation
    if head <= 0:
        return 0.0
    return node.discharge_coeff * node.opening_area_m2 * math.sqrt(2.0 * g * head)


def time_to_fill(node: SinkNode, water_surface_elevation: float, g: float = GRAVITY) -> float | None:
    """Rough seconds-to-full at a *constant* head. ``None`` if no inflow.

    A screening estimate (assumes steady head); the live solver integrates the
    varying head over time for the real figure.
    """
    q = orifice_inflow(node, water_surface_elevation, g)
    if q <= 0:
        return None
    return node.capacity_m3 / q

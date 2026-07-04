"""Boundary conditions for the simulation domain.

Without explicit boundaries water either reflects unphysically off the domain
edge or the domain never drains. Defined here as a small enum; the solver
(Phase 2) applies them at the grid edges and at obstacle faces.
"""

from __future__ import annotations

from enum import Enum


class BoundaryType(Enum):
    """How the solver treats a domain/obstacle edge."""

    OPEN = "open"        # free outflow — water leaves the domain
    CLOSED = "closed"    # reflective wall — obstacles, known barriers
    INFLOW = "inflow"    # fixed discharge/stage — rivers entering the domain

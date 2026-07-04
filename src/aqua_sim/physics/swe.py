"""The shallow-water solver — Phase 2, the scientific core.

Default scheme: local-inertial (LISFLOOD-FP / Bates et al. 2010) — the standard
for urban 2D flood modeling: stable, well-balanced, and far cheaper than the
full dynamic equations. See docs/ARCHITECTURE.md §3 for the formulation, and
`stability.py` for the CFL-adaptive timestep this loop must use.

This module defines the *interface* the rest of the engine codes against. The
numerical kernel is Phase 2 work; the class is intentionally a documented stub
so ingestion, risk, and export can be developed against a stable API first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from aqua_sim.config import SimConfig
from aqua_sim.grid import Grid


@dataclass
class FlowState:
    """The simulation state at one instant."""

    time_s: float
    depth: list[list[float]]              # water depth h (m)
    # velocity components (u, v) added when the momentum solve lands (Phase 2)
    velocity: list[list[tuple[float, float]]] = field(default_factory=list)


class ShallowWaterSolver:
    """Local-inertial 2D shallow-water solver over a ``Grid``.

    Intended usage (Phase 2+):

        solver = ShallowWaterSolver(grid, config)
        for state in solver.run():
            ...  # export frame / feed risk layer

    The loop must, each step: compute the CFL-limited dt (see
    ``physics.stability.cfl_timestep``), apply rainfall/infiltration/drainage
    source terms, solve inter-cell fluxes, update depths by mass balance, and
    enforce boundary conditions.
    """

    def __init__(self, grid: Grid, config: SimConfig) -> None:
        self.grid = grid
        self.config = config

    def run(self) -> Iterator[FlowState]:  # pragma: no cover - Phase 2
        raise NotImplementedError(
            "ShallowWaterSolver is the Phase 2 deliverable. The local-inertial "
            "kernel and CFL-adaptive loop are specified in docs/ARCHITECTURE.md §3."
        )

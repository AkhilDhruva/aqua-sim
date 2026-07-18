"""Infiltration losses — Phase 6 (see docs/ARCHITECTURE.md §3.4).

Pervious surfaces (soil, parks) absorb rainfall; concrete does not. Planned:
SCS Curve Number for screening runs, Green-Ampt for detailed infiltration.
Stubbed until the solver's source/sink terms are wired up.
"""

from __future__ import annotations


def scs_curve_number_loss(rainfall_m: float, curve_number: float) -> float:  # pragma: no cover - Phase 6
    """Planned: SCS-CN cumulative infiltration loss."""
    raise NotImplementedError("Infiltration modeling is planned for Phase 6.")

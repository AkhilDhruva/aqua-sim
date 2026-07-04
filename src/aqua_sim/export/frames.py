"""Frame export — the contract between the Python engine and the Three.js viewer.

Phase 4. A run exports a ``manifest.json`` (grid metadata, time axis, units,
provenance, sink-node definitions, frame list), per-timestep depth/velocity
fields, and ``alerts.json``. Keeping this an explicit, versioned format lets the
solver and viewer evolve independently (see docs/ARCHITECTURE.md §6).
"""

from __future__ import annotations

from aqua_sim.grid import Grid


def write_run(output_dir: str, grid: Grid, frames, alerts) -> None:  # pragma: no cover - Phase 4
    """Planned: serialize a completed run for the viewer."""
    raise NotImplementedError(
        "Frame export is planned for Phase 4. Format is specified in "
        "docs/ARCHITECTURE.md §6."
    )

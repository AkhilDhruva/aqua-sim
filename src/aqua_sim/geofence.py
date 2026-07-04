"""Geofence / area-of-interest masking.

The user defines an AOI; ingestion clips terrain to it and sets ``Grid.mask``.
This Phase 0 helper handles the simple axis-aligned bounding-box case in grid
index space; polygon clipping against real coordinates arrives with DEM
ingestion (Phase 1).
"""

from __future__ import annotations

from aqua_sim.grid import Grid


def apply_bbox_mask(grid: Grid, x0: int, y0: int, x1: int, y1: int) -> None:
    """Mark cells inside the inclusive index box [x0..x1] x [y0..y1] as in-AOI.

    Cells outside the box are masked out (``mask = False``). The solver treats the
    AOI edge as an open/outflow boundary unless told otherwise.
    """
    for y in range(grid.ny):
        for x in range(grid.nx):
            grid.mask[y][x] = (x0 <= x <= x1) and (y0 <= y <= y1)

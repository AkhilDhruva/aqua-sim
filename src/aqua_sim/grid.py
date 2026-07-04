"""The core terrain grid — the single data structure every layer depends on.

Phase 0 uses pure-Python nested lists so the repo is installable and testable
with zero dependencies. Phase 1 will back these fields with NumPy arrays (same
public shape) for performance once real DEM ingestion lands.

Convention: ``field[y][x]`` with ``y`` rows (north-south) and ``x`` columns
(east-west). Cell spacing ``dx == dy`` is in **meters** — the solver requires a
metric, projected grid (reproject geographic data to local UTM at ingest time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

Matrix = list[list[float]]


def _full(ny: int, nx: int, value: float) -> Matrix:
    return [[value for _ in range(nx)] for _ in range(ny)]


@dataclass
class Grid:
    """A metric, georeferenced terrain grid.

    Attributes:
        nx, ny: grid dimensions (columns, rows).
        dx: cell size in meters (square cells assumed: dx == dy).
        z: bare-earth elevation (DTM), meters. The flow floor.
        obstacle: building/structure height above ground (from DSM - DTM), meters.
            0.0 means open ground. The solver routes flow around raised cells.
        manning: Manning's roughness coefficient n per cell (land-cover derived).
        mask: True where the cell is inside the geofenced area of interest.
        crs: coordinate reference system identifier (e.g. "EPSG:32643").
        transform: affine pixel<->world transform (6-tuple), set by ingestion.
        meta: provenance/reproducibility metadata (source, resolution, timestamp).
    """

    nx: int
    ny: int
    dx: float
    z: Matrix
    obstacle: Matrix
    manning: Matrix
    mask: list[list[bool]]
    crs: Optional[str] = None
    transform: Optional[tuple[float, float, float, float, float, float]] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, nx: int, ny: int, dx: float, default_manning: float = 0.03) -> "Grid":
        """Create a flat grid of zeros with a uniform roughness — a blank canvas."""
        return cls(
            nx=nx,
            ny=ny,
            dx=dx,
            z=_full(ny, nx, 0.0),
            obstacle=_full(ny, nx, 0.0),
            manning=_full(ny, nx, default_manning),
            mask=[[True for _ in range(nx)] for _ in range(ny)],
        )

    def elevation_range(self) -> tuple[float, float]:
        """(min, max) bare-earth elevation across the grid — a quick sanity check."""
        flat = [v for row in self.z for v in row]
        return (min(flat), max(flat)) if flat else (0.0, 0.0)

    def cell_area_m2(self) -> float:
        return self.dx * self.dx

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


def zeros(ny: int, nx: int) -> Matrix:
    """An ny×nx matrix of 0.0 — the one matrix constructor shared by the grid
    and the solver, so a future NumPy/Taichi backend swaps in one place."""
    return _full(ny, nx, 0.0)


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
    #: Optional per-cell building coverage fraction [0..1] (set by
    #: ingestion.buildings.apply_buildings; exported to terrain.json).
    coverage: Optional[Matrix] = None
    #: Optional per-cell road/pavement coverage fraction [0..1] (conditioning).
    road_coverage: Optional[Matrix] = None

    # --- Phase 6A conditioned-hydraulic-surface fields (all optional) ---------
    # These are populated by ingestion.conditioning; when all are None the
    # solver runs its original code path (bit-identical to a bare-DTM run).
    #: Per-cell infiltration rate (m/s) and total capacity (m). Green-Ampt-ish:
    #: water infiltrates at the rate until the cumulative reaches capacity.
    infiltration_rate: Optional[Matrix] = None
    infiltration_capacity: Optional[Matrix] = None
    #: Per-cell drainage-inlet sink capacity (m/s) — removes standing water up
    #: to this rate (storm-drain inlets). Supersedes the scalar storm drainage
    #: where present.
    drainage: Optional[Matrix] = None
    #: Subgrid barrier crest elevation (m, absolute) at each cell face; a face
    #: only conveys flow once the water surface tops the crest. ``crest_x`` has
    #: shape ny×(nx+1) (vertical faces), ``crest_y`` (ny+1)×nx (horizontal).
    #: ``None`` / a value at or below the bed means "no barrier".
    crest_x: Optional[Matrix] = None
    crest_y: Optional[Matrix] = None
    #: Bridge/culvert conduits linking (possibly non-adjacent) cells so flow
    #: passes a barrier/embankment instead of falsely damming: list of
    #: ``(x1, y1, x2, y2, cd_area)`` where cd_area = Cd·A (m²) sets conveyance.
    connections: Optional[list[tuple[int, int, int, int, float]]] = None

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

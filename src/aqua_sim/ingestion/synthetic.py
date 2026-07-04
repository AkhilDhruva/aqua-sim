"""Dependency-free synthetic terrain — the Phase 0 stand-in for real data.

This is a *real* implementation of the ``TerrainSource`` contract (not a stub),
so the physics and risk layers can be built and unit-tested against genuine
terrain with no external data or dependencies. It builds a gently sloped plane
with a central valley, so water has somewhere obvious to collect.
"""

from __future__ import annotations

import math

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource


class SyntheticTerrain(TerrainSource):
    """A procedural terrain useful for solver development and benchmarks.

    Args:
        nx, ny: grid dimensions.
        dx: cell size in meters.
        slope: overall tilt (meters of drop per meter of x) so flow has a
            preferred direction.
        valley_depth: depth (m) of a smooth central channel that collects water.
        base_elevation: elevation (m) at the high corner.
        manning: uniform roughness for the whole surface.
    """

    def __init__(
        self,
        nx: int = 64,
        ny: int = 64,
        dx: float = 5.0,
        slope: float = 0.01,
        valley_depth: float = 3.0,
        base_elevation: float = 100.0,
        manning: float = 0.03,
    ) -> None:
        self.nx = nx
        self.ny = ny
        self.dx = dx
        self.slope = slope
        self.valley_depth = valley_depth
        self.base_elevation = base_elevation
        self.manning = manning

    def load(self) -> Grid:
        grid = Grid.empty(self.nx, self.ny, self.dx, default_manning=self.manning)
        cy = (self.ny - 1) / 2.0
        # Half-width of the valley in cells; a cosine profile keeps it smooth.
        half = max(self.ny / 6.0, 1.0)
        for y in range(self.ny):
            # Distance from the central axis, normalized to the valley half-width.
            d = abs(y - cy) / half
            channel = -self.valley_depth * math.cos(min(d, 1.0) * math.pi / 2.0) ** 2
            for x in range(self.nx):
                tilt = -self.slope * self.dx * x  # drop toward increasing x
                grid.z[y][x] = self.base_elevation + tilt + channel
        grid.crs = "SYNTHETIC"
        grid.meta = {
            "source": "SyntheticTerrain",
            "resolution_m": self.dx,
            "note": "procedural test terrain, not georeferenced",
        }
        return grid

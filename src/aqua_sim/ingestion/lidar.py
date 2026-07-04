"""LiDAR point-cloud ingestion — Phase 5 (see docs/DATA_INGESTION.md §4).

Planned pipeline:
    read .las/.laz (PDAL/laspy) -> reproject -> ground-classify (SMRF/PMF)
    -> rasterize ground returns to DTM, all returns to DSM
    -> obstacle = DSM - DTM -> Grid
"""

from __future__ import annotations

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource


class LidarSource(TerrainSource):
    """Load ground-classified terrain from a LiDAR point cloud (.las/.laz)."""

    def __init__(self, path: str, target_dx_m: float = 1.0) -> None:
        self.path = path
        self.target_dx_m = target_dx_m

    def load(self) -> Grid:  # pragma: no cover - Phase 5
        raise NotImplementedError(
            "LidarSource is planned for Phase 5. See docs/DATA_INGESTION.md §4."
        )

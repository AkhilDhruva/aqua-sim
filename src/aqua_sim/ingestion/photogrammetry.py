"""Drone-photogrammetry ingestion — Phase 5, the original 2018 vision.

Swarm drones -> overlapping geotagged photos -> Structure-from-Motion (SfM)
-> dense point cloud -> DTM/DSM -> Grid. See docs/DATA_INGESTION.md §5.

This wrapper consumes the *output* of an SfM pipeline (OpenDroneMap / COLMAP);
the SfM reconstruction itself runs as an external step. Absolute vertical
accuracy depends on Ground Control Points (GCPs) / RTK — without them, flood
depths inherit the drift. The capture spec (≈75% frontlap / 65% sidelap,
consistent GSD, GCPs) is documented for drone operators in DATA_INGESTION.md.
"""

from __future__ import annotations

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource


class PhotogrammetrySource(TerrainSource):
    """Build a Grid from a photogrammetric reconstruction.

    Args:
        point_cloud_path: dense point cloud (.las/.ply) from the SfM/MVS step.
        target_dx_m: output cell size in meters.
    """

    def __init__(self, point_cloud_path: str, target_dx_m: float = 0.5) -> None:
        self.point_cloud_path = point_cloud_path
        self.target_dx_m = target_dx_m

    def load(self) -> Grid:  # pragma: no cover - Phase 5
        raise NotImplementedError(
            "PhotogrammetrySource is planned for Phase 5. It will ground-classify "
            "the dense cloud (e.g. Cloth Simulation Filter) into DTM/DSM. See "
            "docs/DATA_INGESTION.md §5."
        )

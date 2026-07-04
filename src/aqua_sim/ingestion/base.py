"""The ingestion contract.

Every input format — public DEM, LiDAR, drone photogrammetry — is a
``TerrainSource`` that produces the same ``Grid``. The physics and risk layers
are written against ``Grid`` alone and never learn where the terrain came from,
which is what makes input formats pluggable (see docs/DATA_INGESTION.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from aqua_sim.grid import Grid


class TerrainSource(ABC):
    """Abstract terrain source. Implementations must return a metric, CRS-aware
    ``Grid`` with a bare-earth ``z`` (DTM) and an ``obstacle`` layer.
    """

    @abstractmethod
    def load(self) -> Grid:
        """Produce a ``Grid`` ready for simulation.

        Implementations are responsible for: reprojection to a metric CRS,
        resampling to the target cell size, DTM/DSM separation, obstacle burn-in,
        geofence clipping, and populating provenance metadata.
        """
        raise NotImplementedError

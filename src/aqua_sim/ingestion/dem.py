"""Public DEM (GeoTIFF) ingestion — Phase 1 (recommended first real source).

Planned pipeline (see docs/DATA_INGESTION.md §3):
    rasterio read -> reproject to local UTM (pyproj) -> resample to target dx
    -> clip to geofence -> void-fill -> Grid

Introduces the first real dependencies (rasterio, numpy, pyproj); kept out of
the Phase 0 dependency-free core until implemented.
"""

from __future__ import annotations

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource


class DEMSource(TerrainSource):
    """Load a Digital Elevation Model from a GeoTIFF.

    Args:
        path: path to a ``.tif`` DEM.
        target_dx_m: desired output cell size in meters.
        target_crs: metric CRS to reproject into (default: auto-select UTM zone).
    """

    def __init__(self, path: str, target_dx_m: float = 5.0, target_crs: str | None = None) -> None:
        self.path = path
        self.target_dx_m = target_dx_m
        self.target_crs = target_crs

    def load(self) -> Grid:  # pragma: no cover - Phase 1
        raise NotImplementedError(
            "DEMSource is planned for Phase 1. Use SyntheticTerrain for now; see "
            "docs/DATA_INGESTION.md §3 for the intended GeoTIFF pipeline."
        )

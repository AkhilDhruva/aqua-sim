"""Public DEM (GeoTIFF) ingestion — Phase 1 (recommended first real source).

Pipeline (see docs/DATA_INGESTION.md §3):
    rasterio read -> reproject to local UTM (metric) -> resample to target dx
    -> clip to geofence AOI -> void-fill nodata -> Grid

Requires the optional ``geo`` extra (``pip install -e ".[geo]"``): rasterio and
numpy. These are imported lazily inside ``load()`` so the dependency-free core
(config, grid, solver, risk) stays importable without them. Reprojection and the
affine transform come from rasterio itself (GDAL-backed) — no direct pyproj use.

The output is a ``Grid`` with pure-Python nested-list fields — identical to every
other TerrainSource — so the solver and risk layers consume it unchanged.
"""

from __future__ import annotations

from typing import Optional

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing (lon, lat). Manhattan -> 32618 (18N)."""
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


class DEMSource(TerrainSource):
    """Load a Digital Elevation Model from a GeoTIFF into a metric ``Grid``.

    Args:
        path: path to a ``.tif`` DEM (single elevation band).
        target_dx_m: desired output cell size in meters.
        aoi_bounds: optional geofence as ``(min_lon, min_lat, max_lon, max_lat)``
            in WGS84; the grid is clipped to this box.
        target_crs: metric CRS to reproject into (default: auto-select the UTM zone
            of the dataset/AOI centroid).
        default_manning: uniform roughness assigned to every cell (land-cover
            refinement is a later step).
        max_cells: guard against accidentally building a grid too large for the
            reference pure-Python solver; raises with guidance if exceeded.
    """

    def __init__(
        self,
        path: str,
        target_dx_m: float = 5.0,
        aoi_bounds: Optional[tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
        default_manning: float = 0.03,
        max_cells: int = 500_000,
    ) -> None:
        self.path = path
        self.target_dx_m = target_dx_m
        self.aoi_bounds = aoi_bounds
        self.target_crs = target_crs
        self.default_manning = default_manning
        self.max_cells = max_cells

    def load(self) -> Grid:
        import numpy as np
        import rasterio
        from rasterio.transform import Affine  # rasterio's own re-export
        from rasterio.warp import Resampling, reproject, transform_bounds

        with rasterio.open(self.path) as src:
            src_crs = src.crs
            src_nodata = src.nodata

            # Dataset footprint in lon/lat, to pick a UTM zone and clip the AOI.
            lon_min, lat_min, lon_max, lat_max = transform_bounds(
                src_crs, "EPSG:4326", *src.bounds, densify_pts=21)

            if self.target_crs is not None:
                dst_crs = self.target_crs
            else:
                clon = 0.5 * (lon_min + lon_max)
                clat = 0.5 * (lat_min + lat_max)
                dst_crs = f"EPSG:{utm_epsg(clon, clat)}"

            # Target extent (meters) = dataset footprint in dst CRS, optionally
            # intersected with the AOI bounds.
            left, bottom, right, top = transform_bounds(
                src_crs, dst_crs, *src.bounds, densify_pts=21)
            if self.aoi_bounds is not None:
                a_left, a_bottom, a_right, a_top = transform_bounds(
                    "EPSG:4326", dst_crs, *self.aoi_bounds, densify_pts=21)
                left, bottom = max(left, a_left), max(bottom, a_bottom)
                right, top = min(right, a_right), min(top, a_top)
                if right <= left or top <= bottom:
                    raise ValueError("AOI bounds do not overlap the DEM extent.")

            dx = self.target_dx_m
            width = max(int(round((right - left) / dx)), 1)
            height = max(int(round((top - bottom) / dx)), 1)
            if width * height > self.max_cells:
                raise ValueError(
                    f"Requested grid is {width}x{height} = {width*height} cells "
                    f"(> max_cells={self.max_cells}). Increase target_dx_m or shrink "
                    f"the AOI for the reference solver; the production Taichi kernel "
                    f"lifts this limit."
                )

            dst_transform = Affine.translation(left, top) * Affine.scale(dx, -dx)
            fill = np.float32(-9999.0)
            dst = np.full((height, width), fill, dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src_crs,
                src_nodata=src_nodata,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                dst_nodata=float(fill),
                resampling=Resampling.bilinear,
            )

        # A cell is valid only if it is finite AND not the fill value — NaN
        # nodata (common in float32 3DEP products) passes a `!= fill` test and
        # would otherwise poison every downstream computation.
        valid = np.isfinite(dst) & (dst != fill)
        if not valid.any():
            raise ValueError("No valid elevation data after reprojection/clip.")
        # Void-fill: set nodata cells to the minimum valid elevation and mask them
        # out so the solver treats them as outside the area of interest.
        min_valid = float(dst[valid].min())
        z = np.where(valid, dst, min_valid).astype(float)

        grid = Grid.empty(width, height, dx, default_manning=self.default_manning)
        grid.z = z.tolist()
        grid.mask = valid.tolist()
        grid.crs = str(dst_crs)
        grid.transform = (dst_transform.a, dst_transform.b, dst_transform.c,
                          dst_transform.d, dst_transform.e, dst_transform.f)
        grid.meta = {
            "source": "DEMSource",
            "source_path": self.path,
            "resolution_m": dx,
            "target_crs": str(dst_crs),
            "wgs84_bounds": [lon_min, lat_min, lon_max, lat_max],
            "aoi_bounds": list(self.aoi_bounds) if self.aoi_bounds else None,
            "note": "reprojected to metric UTM; bare-earth DEM used as flow surface",
        }
        return grid

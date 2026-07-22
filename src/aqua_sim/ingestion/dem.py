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
        path: str | list[str],
        target_dx_m: float = 5.0,
        aoi_bounds: Optional[tuple[float, float, float, float]] = None,
        target_crs: Optional[str] = None,
        default_manning: float = 0.03,
        max_cells: int = 500_000,
    ) -> None:
        # Multiple paths are mosaicked — each tile is reprojected onto the
        # common target grid and empty cells are filled first-path-wins (order
        # your preferred tile first where tiles overlap). Metro AOIs routinely
        # straddle 1°×1° USGS tile seams.
        self.paths = [path] if isinstance(path, str) else list(path)
        if not self.paths:
            raise ValueError("DEMSource requires at least one DEM path.")
        self.target_dx_m = target_dx_m
        self.aoi_bounds = aoi_bounds
        self.target_crs = target_crs
        self.default_manning = default_manning
        self.max_cells = max_cells

    @property
    def path(self) -> str:
        return self.paths[0]

    def load(self) -> Grid:
        import numpy as np
        import rasterio
        from rasterio.transform import Affine  # rasterio's own re-export
        from rasterio.warp import Resampling, reproject, transform_bounds

        # Union footprint over all source tiles (in lon/lat) picks the UTM zone
        # and the default extent; the AOI intersects it down.
        lon_min = lat_min = float("inf")
        lon_max = lat_max = float("-inf")
        for p in self.paths:
            with rasterio.open(p) as src:
                b = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
                lon_min, lat_min = min(lon_min, b[0]), min(lat_min, b[1])
                lon_max, lat_max = max(lon_max, b[2]), max(lat_max, b[3])

        if self.target_crs is not None:
            dst_crs = self.target_crs
        else:
            clon = 0.5 * (lon_min + lon_max)
            clat = 0.5 * (lat_min + lat_max)
            dst_crs = f"EPSG:{utm_epsg(clon, clat)}"

        if len(self.paths) == 1:
            # Single tile: transform its bounds directly to the target CRS —
            # no lossy round trip through the WGS84 bounding box.
            with rasterio.open(self.paths[0]) as src:
                left, bottom, right, top = transform_bounds(
                    src.crs, dst_crs, *src.bounds, densify_pts=21)
        else:
            # Multi-tile: bbox-of-bboxes via WGS84 (a slight overcover of the
            # true union is fine — uncovered corners become masked nodata).
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", dst_crs, lon_min, lat_min, lon_max, lat_max, densify_pts=21)
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
                f"(> max_cells={self.max_cells}). Increase target_dx_m, shrink the "
                f"AOI, or raise max_cells (the NumPy backend handles multi-million-"
                f"cell grids; the pure-Python reference does not)."
            )

        dst_transform = Affine.translation(left, top) * Affine.scale(dx, -dx)
        fill = np.float32(-9999.0)
        dst = np.full((height, width), fill, dtype=np.float32)
        # Mosaic: reproject each tile into its own layer, then fill dst where
        # it is still nodata. (Reprojecting straight into dst would stamp the
        # second tile's out-of-coverage nodata over the first tile's data.)
        for p in self.paths:
            with rasterio.open(p) as src:
                layer = np.full((height, width), fill, dtype=np.float32)
                reproject(
                    source=rasterio.band(src, 1),
                    destination=layer,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    dst_nodata=float(fill),
                    resampling=Resampling.bilinear,
                )
            take = (dst == fill) & np.isfinite(layer) & (layer != fill)
            dst[take] = layer[take]

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
            "source_path": list(self.paths),  # always a list — type-stable
            "resolution_m": dx,
            "target_crs": str(dst_crs),
            "wgs84_bounds": [lon_min, lat_min, lon_max, lat_max],
            "aoi_bounds": list(self.aoi_bounds) if self.aoi_bounds else None,
            "note": "reprojected to metric UTM; bare-earth DEM used as flow surface",
        }
        return grid

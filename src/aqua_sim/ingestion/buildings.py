"""Building-footprint burn-in: polygons -> the Grid's obstacle layer.

Buildings are impassable walls to the solver (see physics/swe.py) — water must
channel around them through the street canyons. This module rasterizes footprint
polygons onto an existing metric ``Grid`` and writes their heights into
``grid.obstacle``.

Input: a GeoJSON FeatureCollection of building footprints (Polygon /
MultiPolygon), e.g. NYC OpenData "Building Footprints" or Microsoft US Building
Footprints. Heights are taken from a feature property when present
(``height_property``), else a nominal default — for flood routing what matters
is the wall, not its exact height.

Requires the ``geo`` extra (rasterio); imported lazily like DEMSource.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from aqua_sim.grid import Grid

#: Nominal obstacle height when the footprint has no height attribute (m).
DEFAULT_BUILDING_HEIGHT_M = 10.0


def _iter_shapes(features: Iterable[dict[str, Any]], height_property: str | None,
                 default_height: float):
    """Yield (geometry, height) pairs rasterio.features.rasterize accepts."""
    for feat in features:
        geom = feat.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        height = default_height
        props = feat.get("properties") or {}
        if height_property and props.get(height_property) is not None:
            try:
                height = max(float(props[height_property]), 0.0)
            except (TypeError, ValueError):
                height = default_height
        if height > 0.0:
            yield geom, height


def burn_buildings(
    grid: Grid,
    geojson_path: str,
    height_property: str | None = "heightroof",
    default_height: float = DEFAULT_BUILDING_HEIGHT_M,
    source_crs: str = "EPSG:4326",
) -> int:
    """Burn building footprints into ``grid.obstacle``. Returns cells burned.

    The GeoJSON's coordinates are reprojected from ``source_crs`` into the
    grid's CRS before rasterization, so standard lon/lat footprints work against
    the UTM grid ``DEMSource`` produces. A cell whose center falls inside a
    footprint gets the footprint's height (max wins where footprints overlap).
    """
    import numpy as np
    from rasterio import features as rfeatures
    from rasterio.transform import Affine
    from rasterio.warp import transform_geom

    if grid.transform is None:
        raise ValueError("Grid has no affine transform — burn buildings after "
                         "DEM ingestion, not onto synthetic grids.")
    if grid.crs is None:
        raise ValueError("Grid has no CRS.")

    with open(geojson_path) as f:
        collection = json.load(f)
    feats = collection.get("features", [])

    transform = Affine(*grid.transform)
    out = np.zeros((grid.ny, grid.nx), dtype=np.float32)
    shapes = []
    for geom, height in _iter_shapes(feats, height_property, default_height):
        projected = transform_geom(source_crs, grid.crs, geom)
        shapes.append((projected, height))
    if shapes:
        # merge_alg default replaces; heights are per-shape burn values.
        rfeatures.rasterize(shapes, out=out, transform=transform)

    burned = 0
    for y in range(grid.ny):
        row = out[y]
        for x in range(grid.nx):
            v = float(row[x])
            if v > 0.0:
                grid.obstacle[y][x] = max(grid.obstacle[y][x], v)
                burned += 1
    grid.meta["buildings_source"] = geojson_path
    grid.meta["buildings_cells"] = burned
    return burned

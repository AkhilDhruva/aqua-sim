"""LiDAR point-cloud ingestion: classified returns -> 3D building massing.

The physics grid's buildings normally come from an official footprint
dataset (``ingestion/buildings.py``). This module derives the same
``BuildingCollection`` from an airborne LiDAR survey instead, which gives
what footprints cannot: the real roof shape of every building — setbacks,
podiums and towers — at the survey's ground resolution.

Pipeline (all rasters share one north-up metric grid, e.g. the 1 m DEM's):

1. ``rasterize_returns`` — per cell, the highest above-ground return and two
   pulse counts (all returns; returns that are *not* the last of a
   multi-return pulse). Points are reprojected from the cloud's CRS first.
2. ``detect_buildings`` — normalized height ``nDSM = zmax - DTM``; a cell is
   building when ``nDSM >= min_height_m`` and its neighbourhood's
   multi-return ratio is below ``max_multi_ratio`` (canopy lets part of each
   pulse through to lower surfaces, roofs mostly do not). Morphological
   opening removes thin structures (cranes, poles, wires), closing and hole
   filling repair roof dropouts, and connected regions smaller than
   ``min_area_m2`` (vehicles, kiosks) or standing over deep water (ships)
   are discarded. Cells on or within ``bridge_buffer_m`` of a classified
   bridge deck are never building: water flows under elevated structures.
3. ``building_parts`` — each connected region becomes a set of nested
   *level* polygons: level k covers every cell whose roof is at or above
   height band k, so extruding every level from the ground reproduces the
   stepped massing exactly, and dropping a tiny rooftop fragment only lowers
   that spot to the level below it (it never punches a hole).

Survey caveat recorded in the provenance: buildings are as of the survey
date. A building finished afterwards is absent and one demolished since is
present.

Units are meters throughout; heights are relative to the DTM supplied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from aqua_sim.grid import Grid
from aqua_sim.ingestion.base import TerrainSource
from aqua_sim.ingestion.buildings import Building, BuildingCollection

#: The USGS-hosted New York City survey (Entwine Point Tiles).
NYC_LIDAR_DATASET = {
    "name": "USGS 3DEP LiDAR Point Cloud — New York City (NY_NewYorkCity)",
    "ept_url": "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/NY_NewYorkCity/ept.json",
    "acquisition": "2013-08 to 2014-04 (point GPS times)",
    "companion_dtm": "USGS 3DEP 1 m DEM, project NY_CMPG_2013 (same acquisition)",
    "license": "USGS 3DEP — public domain",
    "native_crs": "EPSG:3857 horizontal (EPT reprojection of EPSG:26918), NAVD88 heights (m)",
    "classification_note": ("This survey leaves buildings and vegetation together in class 1 "
                            "(unclassified); buildings are separated geometrically, see "
                            "aqua_sim.ingestion.lidar.detect_buildings."),
}

#: ASPRS classes holding above-ground features in the NYC survey.
ABOVE_GROUND_CLASSES = (1,)
#: ASPRS bridge-deck class. Decks, and the trusses and cables above them,
#: are elevated: water flows beneath, so they must never become walls.
BRIDGE_CLASSES = (17,)


@dataclass
class ReturnRasters:
    """Per-cell rasters from ``rasterize_returns`` (row 0 = north)."""

    zmax: "object"        # float32, NaN where no return
    count: "object"       # int32, returns per cell
    multi: "object"       # int32, returns that are not the last of a multi-return pulse
    transform: tuple      # affine (a, b, c, d, e, f)
    crs: str
    points_read: int = 0
    points_used: int = 0
    bridge: "object" = None   # int32, bridge-deck returns per cell (None if not binned)


@dataclass
class BuildingRasters:
    mask: "object"        # bool, building cells
    ndsm: "object"        # float32, roof height above DTM (m), 0 outside mask
    labels: "object"      # int32, connected region id (0 = none)
    stats: dict


def grid_for_bounds(bounds: Sequence[float], res: float, snap_origin: Sequence[float] = (0.0, 0.0)):
    """North-up grid covering ``(left, bottom, right, top)``, snapped so cell
    edges fall on ``snap_origin + k * res`` (pass the DEM's origin so the
    LiDAR cells coincide with DEM pixels). Returns ``(transform, (ny, nx))``."""
    import math

    left, bottom, right, top = bounds
    ox, oy = snap_origin
    left = ox + math.floor((left - ox) / res) * res
    top = oy + math.ceil((top - oy) / res) * res
    right = ox + math.ceil((right - ox) / res) * res
    bottom = oy + math.floor((bottom - oy) / res) * res
    nx = int(round((right - left) / res))
    ny = int(round((top - bottom) / res))
    return (res, 0.0, left, 0.0, -res, top), (ny, nx)


def rasterize_returns(laz_paths: Iterable[str], transform: tuple, shape: tuple,
                      src_crs: str = "EPSG:3857", dst_crs: str = "EPSG:26918",
                      classes: Sequence[int] = ABOVE_GROUND_CLASSES,
                      bridge_classes: Sequence[int] = BRIDGE_CLASSES) -> ReturnRasters:
    """Bin the selected classes' returns into a grid: max Z and pulse counts,
    plus a per-cell count of bridge-deck returns."""
    import laspy
    import numpy as np
    from pyproj import Transformer

    ny, nx = shape
    res, _, left, _, neg_res, top = transform
    zmax = np.full(ny * nx, -np.inf, dtype=np.float64)
    count = np.zeros(ny * nx, dtype=np.int64)
    multi = np.zeros(ny * nx, dtype=np.int64)
    bridge = np.zeros(ny * nx, dtype=np.int64)
    tr = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    read = used = 0
    for path in laz_paths:
        las = laspy.read(path)
        read += len(las.points)
        cls_all = np.asarray(las.classification)
        keep = np.isin(cls_all, classes) | np.isin(cls_all, bridge_classes)
        if not keep.any():
            continue
        x, y = tr.transform(np.asarray(las.x)[keep], np.asarray(las.y)[keep])
        cls = cls_all[keep]
        z = np.asarray(las.z)[keep]
        rn = np.asarray(las.return_number)[keep]
        nr = np.asarray(las.number_of_returns)[keep]
        col = np.floor((x - left) / res).astype(np.int64)
        row = np.floor((top - y) / -neg_res).astype(np.int64)
        inside = (col >= 0) & (col < nx) & (row >= 0) & (row < ny)
        if not inside.any():
            continue
        is_b = np.isin(cls, bridge_classes)
        sel = inside & ~is_b
        bsel = inside & is_b
        # In-place scatter-adds: a full-grid bincount per file would allocate
        # ny*nx counters for every one of thousands of small tiles.
        if bsel.any():
            np.add.at(bridge, row[bsel] * nx + col[bsel], 1)
        if not sel.any():
            continue
        idx = row[sel] * nx + col[sel]
        used += int(sel.sum())
        np.maximum.at(zmax, idx, z[sel])
        np.add.at(count, idx, 1)
        np.add.at(multi, idx, ((rn < nr) & (nr > 1))[sel].astype(np.int64))
    zmax[~np.isfinite(zmax)] = np.nan
    return ReturnRasters(zmax=zmax.reshape(ny, nx).astype(np.float32),
                         count=count.reshape(ny, nx).astype(np.int32),
                         multi=multi.reshape(ny, nx).astype(np.int32),
                         transform=tuple(transform), crs=dst_crs,
                         points_read=read, points_used=used,
                         bridge=bridge.reshape(ny, nx).astype(np.int32))


def read_dtm(dem_paths: Sequence[str], transform: tuple, shape: tuple, crs: str):
    """Mosaic DEM tile(s) onto exactly this grid (NaN where no data)."""
    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, reproject

    ny, nx = shape
    out = np.full((ny, nx), np.nan, dtype=np.float32)
    for p in dem_paths:
        layer = np.full((ny, nx), np.nan, dtype=np.float32)
        with rasterio.open(p) as src:
            reproject(source=rasterio.band(src, 1), destination=layer,
                      src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata,
                      dst_transform=Affine(*transform), dst_crs=crs, dst_nodata=np.nan,
                      resampling=Resampling.bilinear)
        take = np.isnan(out) & np.isfinite(layer) & (layer > -1e30)
        out[take] = layer[take]
    return out


def detect_buildings(rr: ReturnRasters, dtm, min_height_m: float = 2.5,
                     max_multi_ratio: float = 0.35, min_area_m2: float = 50.0,
                     max_hole_m2: float = 400.0, min_ground_m: float = -2.0,
                     spike_tol_m: float = 1.5, bridge_buffer_m: float = 3.0) -> BuildingRasters:
    """Separate buildings from canopy, vehicles and noise; see module doc."""
    import numpy as np
    from scipy import ndimage as ndi

    res = rr.transform[0]
    cell_area = res * res
    ndsm = rr.zmax - dtm
    has = np.isfinite(ndsm)
    # Multi-return ratio over a 3x3 neighbourhood, so a single noisy cell
    # neither creates nor removes canopy.
    k = np.ones((3, 3))
    n3 = ndi.convolve(rr.count.astype(np.float64), k, mode="constant")
    m3 = ndi.convolve(rr.multi.astype(np.float64), k, mode="constant")
    ratio = np.where(n3 > 0, m3 / np.maximum(n3, 1), 1.0)
    cand = has & (ndsm >= min_height_m) & (ratio < max_multi_ratio)
    s3 = np.ones((3, 3), dtype=bool)
    near_bridge = None
    if rr.bridge is not None and (rr.bridge > 0).any():
        # Bridge decks plus a small buffer: removes the trusses, cables and
        # towers that the survey left in class 1 above a classified deck.
        it = max(int(round(bridge_buffer_m / res)), 1)
        near_bridge = ndi.binary_dilation(rr.bridge > 0, structure=s3, iterations=it)
        cand &= ~near_bridge
    opened = ndi.binary_opening(cand, structure=s3)
    closed = ndi.binary_closing(opened, structure=s3)
    # Fill enclosed holes (roof dropouts, light wells) up to max_hole_m2.
    filled = ndi.binary_fill_holes(closed)
    holes = filled & ~closed
    hl, nh = ndi.label(holes)
    if nh:
        sizes = ndi.sum(holes, hl, index=np.arange(1, nh + 1)) * cell_area
        small = np.zeros(nh + 1, dtype=bool)
        small[1:] = sizes <= max_hole_m2
        closed = closed | small[hl]
    if near_bridge is not None:
        closed &= ~near_bridge      # closing/hole-filling must not re-bridge a deck
    labels, n = ndi.label(closed, structure=s3)
    stats = {"candidates": int(cand.sum()), "regions_raw": int(n),
             "bridge_cells": int((rr.bridge > 0).sum()) if rr.bridge is not None else 0}
    if n:
        idx = np.arange(1, n + 1)
        area = ndi.sum(closed, labels, index=idx) * cell_area
        ground = ndi.median(np.nan_to_num(dtm, nan=-99.0), labels, index=idx)
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = (area >= min_area_m2) & (ground >= min_ground_m)
        stats.update(dropped_small=int(((area < min_area_m2)).sum()),
                     dropped_over_water=int(((area >= min_area_m2) & (ground < min_ground_m)).sum()))
        mask = keep[labels]
    else:
        mask = closed
    labels, n = ndi.label(mask, structure=s3)
    # Roof height: fill dropout cells from their region's median, then
    # despeckle: a cell supported by fewer than 3 of its 8 neighbours within
    # ``spike_tol_m`` of its height (an antenna, a lone mast return) takes the
    # 3x3 median. Corners of real roofs and setbacks always have >= 3 such
    # neighbours, so they keep their measured height; a plain median filter
    # would shave every convex corner down to the level below.
    h = np.where(mask & has, ndsm, np.nan).astype(np.float32)
    if n:
        med = np.zeros(n + 1, dtype=np.float32)
        med[1:] = ndi.median(np.nan_to_num(h, nan=0.0), labels, index=np.arange(1, n + 1))
        h = np.where(mask & ~np.isfinite(h), med[labels], h)
    h = np.where(mask, h, 0.0).astype(np.float32)
    support = np.zeros(h.shape, dtype=np.int8)
    pad = np.pad(h, 1, mode="constant")
    ny_, nx_ = h.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                nb = pad[1 + dy:1 + dy + ny_, 1 + dx:1 + dx + nx_]
                support += (np.abs(nb - h) <= spike_tol_m).astype(np.int8)
    spike = mask & (support < 3)
    hf = ndi.median_filter(h, size=3)
    h = np.where(spike, hf, h)
    h = np.where(mask, np.maximum(h, min_height_m), 0.0).astype(np.float32)
    stats["despeckled_cells"] = int(spike.sum())
    stats.update(regions=int(n), building_cells=int(mask.sum()),
                 building_area_m2=round(float(mask.sum() * cell_area), 1))
    return BuildingRasters(mask=mask, ndsm=h, labels=labels.astype(np.int32), stats=stats)


def _simplify_ring(ring, tol: float):
    """Douglas-Peucker on a closed ring (first == last)."""
    import numpy as np

    pts = np.asarray(ring, dtype=float)
    if len(pts) <= 5 or tol <= 0:
        return [tuple(p) for p in pts]
    pts = pts[:-1]
    # Anchor at the vertex farthest from the first, so the split is stable.
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(axis=1)))
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[far] = True
    stack = [(0, far), (far, len(pts))]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a = pts[i]
        b = pts[j % len(pts)]
        seg = pts[i + 1:j]
        ab = b - a
        L = float(np.hypot(*ab))
        if L == 0:
            d = np.hypot(*(seg - a).T)
        else:
            d = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / L
        m = int(np.argmax(d))
        if d[m] > tol:
            k = i + 1 + m
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    out = [tuple(p) for p in pts[keep]]
    if len(out) < 3:
        return [tuple(p) for p in np.asarray(ring, dtype=float)]
    out.append(out[0])
    return out


def building_parts(br: BuildingRasters, transform: tuple, band_m: float = 3.0,
                   min_level_cells: int = 12, simplify_m: float = 0.6,
                   id_prefix: str = "lidar") -> list[Building]:
    """Nested level polygons per region (see module doc), as ``Building`` records."""
    import numpy as np
    from rasterio import features as rfeatures
    from rasterio.transform import Affine
    from scipy import ndimage as ndi

    res = transform[0]
    out: list[Building] = []
    objs = ndi.find_objects(br.labels)
    for rid, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        sub_lab = br.labels[sl] == rid
        sub_h = np.where(sub_lab, br.ndsm[sl], 0.0)
        band = np.floor(sub_h / band_m).astype(np.int32)
        present, counts = np.unique(band[sub_lab], return_counts=True)
        # Keep the lowest level always; higher levels only when enough cells
        # sit exactly in that band (tiny rooftop fragments fold downward).
        levels = [int(present[0])] + [int(b) for b, c in zip(present[1:], counts[1:]) if c >= min_level_cells]
        r0, c0 = sl[0].start, sl[1].start
        a, b, c, d, e, f = transform
        sub_tf = Affine(a, b, c + c0 * a, d, e, f + r0 * e)
        for li, lv in enumerate(levels):
            nxt = levels[li + 1] if li + 1 < len(levels) else None
            region = sub_lab & (band >= lv)
            top_cells = region & ((band < nxt) if nxt is not None else True)
            height = float(np.mean(sub_h[top_cells])) if top_cells.any() else float(lv * band_m)
            polys = []
            for geom, val in rfeatures.shapes(region.astype(np.uint8), mask=region,
                                              transform=sub_tf, connectivity=4):
                if val != 1:
                    continue
                rings = [_simplify_ring(r, simplify_m) for r in geom["coordinates"]]
                polys.append(rings)
            if polys:
                out.append(Building(id=f"{id_prefix}-{rid}-L{li}", height_m=round(height, 2),
                                    ground_m=None, polygons=polys))
    return out


def buildings_from_lidar(laz_paths: Sequence[str], dem_paths: Sequence[str],
                         bounds: Sequence[float], crs: str = "EPSG:26918",
                         res: float = 1.0, src_crs: str = "EPSG:3857",
                         provenance: Optional[dict] = None,
                         raster_out: Optional[str] = None,
                         detect_kwargs: Optional[dict] = None,
                         parts_kwargs: Optional[dict] = None) -> BuildingCollection:
    """End to end: LAZ tiles + DTM tile(s) -> ``BuildingCollection`` in ``crs``.

    ``bounds`` are ``(left, bottom, right, top)`` in ``crs``. The grid snaps to
    the first DEM's pixel origin so building cells coincide with DTM pixels.
    ``raster_out`` (a ``.tif`` path) optionally saves the nDSM for inspection.
    """
    import rasterio

    with rasterio.open(dem_paths[0]) as src:
        origin = (src.transform.c, src.transform.f)
    transform, shape = grid_for_bounds(bounds, res, snap_origin=origin)
    rr = rasterize_returns(laz_paths, transform, shape, src_crs=src_crs, dst_crs=crs)
    dtm = read_dtm(dem_paths, transform, shape, crs)
    br = detect_buildings(rr, dtm, **(detect_kwargs or {}))
    parts = building_parts(br, transform, **(parts_kwargs or {}))
    if raster_out:
        from rasterio.transform import Affine
        with rasterio.open(raster_out, "w", driver="GTiff", height=shape[0], width=shape[1],
                           count=1, dtype="float32", crs=crs, transform=Affine(*transform),
                           compress="deflate", nodata=0.0) as dst:
            dst.write(br.ndsm, 1)
    prov = dict(provenance or NYC_LIDAR_DATASET)
    prov.update({
        "method": "LiDAR nDSM (max above-ground return - DTM), canopy/vehicle/noise filtered, "
                  "nested height-band polygons",
        "raster_res_m": res, "crs": crs, "bounds": list(bounds),
        "points_read": rr.points_read, "points_used": rr.points_used,
        "laz_files": len(list(laz_paths)), "dem_paths": [os.path.basename(p) for p in dem_paths],
        "detection": br.stats, "parts": len(parts),
        "parameters": {**(detect_kwargs or {}), **(parts_kwargs or {})},
        "caveat": "buildings as of the survey date (2013-2014)",
    })
    return BuildingCollection(buildings=parts, crs=crs, provenance=prov)


def write_geojson(collection: BuildingCollection, path: str) -> None:
    """Write the collection as a GeoJSON FeatureCollection in its own CRS
    (named in a top-level ``crs`` member), gzip-compressed if ``path`` ends
    in ``.gz``."""
    import gzip
    import json

    feats = []
    for b in collection.buildings:
        feats.append({"type": "Feature",
                      "properties": {"id": b.id, "height_m": b.height_m},
                      "geometry": {"type": "MultiPolygon",
                                   "coordinates": [[[[round(x, 2), round(y, 2)] for x, y in ring]
                                                    for ring in poly] for poly in b.polygons]}})
    doc = {"type": "FeatureCollection",
           "crs": {"type": "name", "properties": {"name": collection.crs}},
           "provenance": collection.provenance, "features": feats}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt") as f:
        json.dump(doc, f)


def read_geojson(path: str, grid: Optional[Grid] = None) -> BuildingCollection:
    """Read a collection written by ``write_geojson``; reproject to ``grid.crs``
    when it differs."""
    import gzip
    import json

    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        doc = json.load(f)
    crs = doc["crs"]["properties"]["name"]
    tr = None
    if grid is not None and grid.crs and grid.crs != crs:
        from pyproj import Transformer
        tr = Transformer.from_crs(crs, grid.crs, always_xy=True)
    out = []
    for ft in doc["features"]:
        polys = ft["geometry"]["coordinates"]
        if tr is not None:
            polys = [[[tr.transform(x, y) for x, y in ring] for ring in poly] for poly in polys]
        else:
            polys = [[[tuple(p) for p in ring] for ring in poly] for poly in polys]
        out.append(Building(id=ft["properties"]["id"], height_m=float(ft["properties"]["height_m"]),
                            ground_m=None, polygons=polys))
    return BuildingCollection(buildings=out, crs=grid.crs if tr is not None else crs,
                              provenance=doc.get("provenance", {}))


class LidarSource(TerrainSource):
    """Bare-earth terrain from LiDAR. Not implemented: use the USGS 1 m DEM
    produced from the same survey (``DEMSource``); this module supplies the
    above-ground part (buildings) from the point cloud."""

    def __init__(self, path: str, target_dx_m: float = 1.0) -> None:
        self.path = path
        self.target_dx_m = target_dx_m

    def load(self) -> Grid:  # pragma: no cover
        raise NotImplementedError(
            "Ground terrain comes from the USGS 1 m DEM (DEMSource); "
            "LiDAR supplies buildings via buildings_from_lidar()."
        )

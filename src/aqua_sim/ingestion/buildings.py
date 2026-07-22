"""Building ingestion: official footprint datasets -> physics + visualization.

Reads building footprints from any fiona-readable source (GeoPackage,
Shapefile, GeoJSON — including extracts of the official **NYC Open Data
"Building Footprints"** dataset, ID ``nqwf-w8eh``), reprojects them into a
run's metric grid CRS, converts all dimensions to meters, and produces:

  1. a **physics** integration — per-cell building *coverage fraction*, and,
     at fine resolutions (``dx <= CLOSED_CELL_MAX_DX_M``), closed no-flow
     obstacle cells (see docs/ARCHITECTURE.md: interior walls are
     unconditionally no-flow);
  2. a **visualization** export — tiled, scene-local extruded prisms written
     as ``buildings.json`` (see export in this module), kept fully separate
     from the solver's obstacle grid.

Coarse grids (e.g. the 30 m five-borough screening run) deliberately get NO
binary obstacles: a 30 m cell 20 %-covered by a tower is not a wall, and
marking it one overblocks the street network. There, coverage is exported and
buildings stay presentation-only (subgrid porosity is the Phase 6 upgrade).

Units: the NYC dataset's ``HEIGHTROOF`` (roof height above ground) and
``GROUNDELEV`` (ground elevation, NAVD88) are in **feet**; geometry in the
native EPSG:2263 is in US survey feet. Reprojection to the grid CRS yields
meters; attribute feet are converted explicitly (``FT_TO_M``).

Provenance: dataset name/ID, official URL, transport URL, license, CRS chain,
vertical datum, feature counts, and the source file's SHA-256 digest all
travel with the grid and into the run manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from aqua_sim.grid import Grid

FT_TO_M = 0.3048
#: Nominal height when the footprint has no usable height attribute (m).
DEFAULT_BUILDING_HEIGHT_M = 10.0
#: At or below this cell size, covered cells become closed no-flow obstacles.
CLOSED_CELL_MAX_DX_M = 10.0
#: Coverage fraction at/above which a fine-grid cell is treated as closed.
CLOSED_COVERAGE_THRESHOLD = 0.5

#: Official dataset identity for provenance (the NYC footprints case).
NYC_FOOTPRINTS_DATASET = {
    "name": "NYC Open Data — Building Footprints",
    "dataset_id": "nqwf-w8eh",
    "official_url": "https://data.cityofnewyork.us/Housing-Development/Building-Footprints/nqwf-w8eh",
    "license": "NYC Open Data — free public use (https://opendata.cityofnewyork.us/overview/)",
    "native_crs": "EPSG:2263 (NY State Plane Long Island, US survey ft)",
    "vertical_datum": "NAVD88 (GROUNDELEV, feet)",
    "attribute_units": "HEIGHTROOF/GROUNDELEV in feet",
}


@dataclass
class Building:
    """One footprint in grid-CRS meters, physics- and viz-ready."""

    id: str
    height_m: float
    ground_m: Optional[float]          # NAVD88 ground elevation (m), if present
    rings: list[list[tuple[float, float]]]  # outer ring + holes, grid-CRS meters
    year: Optional[int] = None
    bin: Optional[str] = None


@dataclass
class BuildingCollection:
    buildings: list[Building]
    crs: str
    provenance: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.buildings)


def _sha256(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _first_key(props: dict, *names):
    for n in names:
        for k in (n, n.upper(), n.lower()):
            if k in props and props[k] is not None:
                return props[k]
    return None


def _geom_rings(geom: dict) -> Iterable[list[list[float]]]:
    """Yield polygon ring-lists from a Polygon/MultiPolygon geometry."""
    t = geom.get("type")
    if t == "Polygon":
        yield geom["coordinates"]
    elif t == "MultiPolygon":
        yield from geom["coordinates"]


class BuildingsSource:
    """Load an official building-footprint dataset for a specific run grid.

    Args:
        path: fiona-readable source (``.gpkg``, ``.shp``, ``.geojson``).
        dataset_info: provenance identity block (defaults to the NYC
            footprints dataset); pass your own for other jurisdictions.
        transport_url: where this particular file was actually retrieved from
            (mirror or official) — recorded alongside the official URL.
        height_attr / ground_attr: attribute names carrying roof height above
            ground and ground elevation (NYC schema by default).
        attrs_in_feet: convert those attributes ft -> m (True for NYC).
    """

    def __init__(
        self,
        path: str,
        dataset_info: Optional[dict] = None,
        transport_url: Optional[str] = None,
        height_attr: str = "HEIGHTROOF",
        ground_attr: str = "GROUNDELEV",
        attrs_in_feet: bool = True,
        default_height_m: float = DEFAULT_BUILDING_HEIGHT_M,
    ) -> None:
        self.path = path
        self.dataset_info = dict(dataset_info or NYC_FOOTPRINTS_DATASET)
        self.transport_url = transport_url
        self.height_attr = height_attr
        self.ground_attr = ground_attr
        self.attrs_in_feet = attrs_in_feet
        self.default_height_m = default_height_m

    def load_for_grid(self, grid: Grid,
                      aoi_bounds: Optional[tuple[float, float, float, float]] = None
                      ) -> BuildingCollection:
        """Read, AOI-clip, reproject to ``grid.crs``, and metricize.

        ``aoi_bounds`` is a WGS84 lon/lat box; by default the grid's own
        extent is used, so only buildings intersecting the run domain are
        processed at all (bbox pre-filter at the source layer).
        """
        import fiona
        from rasterio.warp import transform_bounds, transform_geom

        if grid.crs is None or grid.transform is None:
            raise ValueError("Grid must be georeferenced (CRS + transform) — "
                             "ingest the DEM before the buildings.")

        a, _, left, _, e, top = grid.transform
        right = left + grid.nx * a
        bottom = top + grid.ny * e  # e is negative
        if aoi_bounds is None:
            aoi_bounds = transform_bounds(grid.crs, "EPSG:4326",
                                          left, bottom, right, top, densify_pts=21)

        unit = FT_TO_M if self.attrs_in_feet else 1.0
        buildings: list[Building] = []
        total = 0
        with fiona.open(self.path) as src:
            src_crs = src.crs_wkt or (f"EPSG:{src.crs.to_epsg()}" if src.crs else None)
            total = len(src)
            # bbox filter in the SOURCE CRS: clip before any processing.
            src_bbox = transform_bounds("EPSG:4326", src_crs, *aoi_bounds,
                                        densify_pts=21)
            for feat in src.filter(bbox=src_bbox):
                geom = dict(feat["geometry"]) if feat["geometry"] else None
                if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
                    continue
                props = dict(feat["properties"])
                raw_h = _first_key(props, self.height_attr)
                try:
                    height_m = max(float(raw_h) * unit, 0.0) if raw_h is not None \
                        else self.default_height_m
                except (TypeError, ValueError):
                    height_m = self.default_height_m
                if height_m <= 0.0:
                    height_m = self.default_height_m
                raw_g = _first_key(props, self.ground_attr)
                try:
                    ground_m = float(raw_g) * unit if raw_g is not None else None
                except (TypeError, ValueError):
                    ground_m = None
                projected = transform_geom(src_crs, grid.crs, geom)
                rings: list[list[tuple[float, float]]] = []
                for poly in _geom_rings(projected):
                    for ring in poly:
                        rings.append([(float(x), float(y)) for x, y in ring])
                if not rings:
                    continue
                year = _first_key(props, "CNSTRCT_YR")
                buildings.append(Building(
                    id=str(_first_key(props, "DOITT_ID", "fid") or len(buildings)),
                    height_m=height_m,
                    ground_m=ground_m,
                    rings=rings,
                    year=int(year) if year else None,
                    bin=str(_first_key(props, "BIN") or "") or None,
                ))

        provenance = {
            **self.dataset_info,
            "transport_url": self.transport_url,
            "source_file": os.path.basename(self.path),
            "source_sha256": _sha256(self.path),
            "source_mtime_utc": __import__("datetime").datetime.utcfromtimestamp(
                os.path.getmtime(self.path)).isoformat() + "Z",
            "grid_crs": grid.crs,
            "aoi_bounds_wgs84": list(aoi_bounds),
            "features_in_source": total,
            "features_in_aoi": len(buildings),
            "height_conversion": "ft -> m (x0.3048)" if self.attrs_in_feet else "meters",
        }
        return BuildingCollection(buildings=buildings, crs=grid.crs,
                                  provenance=provenance)


# ---------------------------------------------------------------------------
# Physics integration
# ---------------------------------------------------------------------------

def rasterize_coverage(collection: BuildingCollection, grid: Grid,
                       supersample: int = 4):
    """Per-cell building coverage fraction in [0, 1] (numpy array ny×nx).

    Rasterizes footprints at ``supersample``× the grid resolution and
    block-averages — an unbiased estimate of the covered area fraction, which
    a plain cell-center hit test is not.
    """
    import numpy as np
    from rasterio import features as rfeatures
    from rasterio.transform import Affine

    a, b, c, d, e, f = grid.transform
    fine = Affine(a / supersample, b, c, d, e / supersample, f)
    shape = (grid.ny * supersample, grid.nx * supersample)
    shapes = []
    for bld in collection.buildings:
        coords = [[list(pt) for pt in ring] for ring in bld.rings]
        shapes.append(({"type": "Polygon", "coordinates": coords}, 1))
    if not shapes:
        return np.zeros((grid.ny, grid.nx))
    mask = rfeatures.rasterize(shapes, out_shape=shape, transform=fine,
                               dtype="uint8", all_touched=False)
    coverage = mask.reshape(grid.ny, supersample, grid.nx, supersample) \
                   .mean(axis=(1, 3)).astype(float)
    return coverage


def apply_buildings(grid: Grid, collection: BuildingCollection,
                    closed_threshold: float = CLOSED_COVERAGE_THRESHOLD) -> dict:
    """Integrate buildings into the physics grid, resolution-appropriately.

    * Always: attaches the coverage-fraction field to ``grid.coverage`` and
      the dataset provenance to ``grid.meta['buildings']``.
    * ``grid.dx <= CLOSED_CELL_MAX_DX_M``: cells with coverage >=
      ``closed_threshold`` become closed obstacle cells (height = tallest
      contributing building) — the solver treats them as no-flow walls.
    * Coarser grids: NO obstacle cells (naive binary blocking at screening
      resolution misrepresents the street network); buildings remain
      presentation-only and the run must be labeled screening-resolution.

    Returns a summary dict (mode, cells closed, mean coverage).
    """
    import numpy as np

    coverage = rasterize_coverage(collection, grid)
    grid.coverage = coverage.tolist()

    closed = 0
    fine = grid.dx <= CLOSED_CELL_MAX_DX_M
    if fine and len(collection):
        # Height raster: max building height per cell (for wall height).
        from rasterio import features as rfeatures
        from rasterio.transform import Affine
        hshapes = [({"type": "Polygon",
                     "coordinates": [[list(pt) for pt in ring] for ring in b.rings]},
                    b.height_m) for b in collection.buildings]
        heights = rfeatures.rasterize(
            hshapes, out_shape=(grid.ny, grid.nx),
            transform=Affine(*grid.transform), dtype="float64", all_touched=False,
            merge_alg=__import__("rasterio.enums", fromlist=["MergeAlg"]).MergeAlg.replace)
        solid = coverage >= closed_threshold
        for y in range(grid.ny):
            row_solid = solid[y]
            for x in range(grid.nx):
                if row_solid[x]:
                    h = float(heights[y][x]) or DEFAULT_BUILDING_HEIGHT_M
                    grid.obstacle[y][x] = max(grid.obstacle[y][x], h)
                    closed += 1

    summary = {
        "mode": "closed_cells" if fine else "coverage_only (screening resolution)",
        "dx_m": grid.dx,
        "closed_cells": closed,
        "closed_threshold": closed_threshold if fine else None,
        "buildings": len(collection),
        "mean_coverage": float(np.mean(coverage)) if coverage.size else 0.0,
    }
    grid.meta["buildings"] = {**collection.provenance, "integration": summary}
    return summary


# ---------------------------------------------------------------------------
# Visualization export (separate contract from terrain.json)
# ---------------------------------------------------------------------------

def export_buildings_json(collection: BuildingCollection, grid: Grid,
                          run_dir: str, tile_m: float = 250.0,
                          presets: Optional[list[dict]] = None,
                          min_height_m: float = 0.0) -> dict:
    """Write ``buildings.json``: tiled, scene-local extruded-prism input.

    Coordinates are **scene-local meters**: ``(easting - left, top - northing)``
    — i.e. the grid's UTM origin is subtracted server-side so the viewer never
    holds full UTM eastings in float32. Each building carries its DEM base
    elevation (visual base per the plan) and its grid-cell bbox so the viewer
    can compute flood stats for the clicked building from the loaded frames.

    ``min_height_m`` keeps only buildings at least that tall — used for the
    coarse regional overview, which renders the skyline rather than tens of
    thousands of tiny prisms.
    """
    a, _, left, _, e, top = grid.transform
    dx = grid.dx

    def dem_base(cx_m: float, cy_m: float) -> float:
        i = min(max(int(cx_m / dx), 0), grid.nx - 1)
        j = min(max(int(cy_m / dx), 0), grid.ny - 1)
        return grid.z[j][i]

    tiles: dict[tuple[int, int], dict] = {}
    exported = 0
    for b in collection.buildings:
        if b.height_m < min_height_m:
            continue
        exported += 1
        outer = b.rings[0]
        lx = [p[0] - left for p in outer]
        ly = [top - p[1] for p in outer]
        cx, cy = sum(lx) / len(lx), sum(ly) / len(ly)
        key = (int(cx // tile_m), int(cy // tile_m))
        t = tiles.setdefault(key, {"key": list(key), "buildings": []})
        i0 = max(int(min(lx) / dx), 0); i1 = min(int(max(lx) / dx), grid.nx - 1)
        j0 = max(int(min(ly) / dx), 0); j1 = min(int(max(ly) / dx), grid.ny - 1)
        t["buildings"].append({
            "id": b.id, "bin": b.bin, "h": round(b.height_m, 2),
            "ground": round(b.ground_m, 2) if b.ground_m is not None else None,
            "base": round(dem_base(cx, cy), 2),
            "year": b.year,
            "cells": [i0, j0, i1, j1],
            "rings": [[[round(p[0] - left, 2), round(top - p[1], 2)]
                       for p in ring] for ring in b.rings],
        })

    doc = {
        "format_version": "1.0",
        "crs": grid.crs,
        "origin_utm": [left, top],
        "axis": "x=east(m from left), y=south(m from top); scene maps x-W/2, y-H/2",
        "grid": {"nx": grid.nx, "ny": grid.ny, "dx": dx},
        "tile_m": tile_m,
        "tile_count": len(tiles),
        "building_count": exported,
        "min_height_m": min_height_m,
        "provenance": collection.provenance,
        "presets": presets or [],
        "tiles": sorted(tiles.values(), key=lambda t: (t["key"][1], t["key"][0])),
    }
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "buildings.json"), "w") as f:
        json.dump(doc, f)
    return doc


def make_presets(grid: Grid, anchors: list[tuple[str, float, float, float]]) -> list[dict]:
    """Camera presets from (name, lon, lat, distance_m) anchors -> scene coords."""
    from rasterio.warp import transform as warp_transform

    a, _, left, _, e, top = grid.transform
    W, H = grid.nx * grid.dx, grid.ny * grid.dx
    out = []
    for name, lon, lat, dist in anchors:
        xs, ys = warp_transform("EPSG:4326", grid.crs, [lon], [lat])
        sx = (xs[0] - left) - W / 2.0
        sz = (top - ys[0]) - H / 2.0
        if -W <= sx <= W and -H <= sz <= H:  # keep only anchors near this domain
            out.append({"name": name, "x": round(sx, 1), "z": round(sz, 1),
                        "dist": dist})
    return out


# Backward-compatible helper (kept for existing tests/callers): burn a plain
# GeoJSON's footprints straight into the obstacle layer.
def burn_buildings(
    grid: Grid,
    geojson_path: str,
    height_property: str | None = "heightroof",
    default_height: float = DEFAULT_BUILDING_HEIGHT_M,
    source_crs: str = "EPSG:4326",
) -> int:
    """Burn building footprints from a GeoJSON into ``grid.obstacle``.

    The simple path (no coverage logic): a cell whose center is inside a
    footprint gets the footprint's height. Prefer :class:`BuildingsSource` +
    :func:`apply_buildings` for official datasets and coarse grids.
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

    shapes = []
    for feat in feats:
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
            shapes.append((transform_geom(source_crs, grid.crs, geom), height))

    out = np.zeros((grid.ny, grid.nx), dtype=np.float32)
    if shapes:
        rfeatures.rasterize(shapes, out=out, transform=Affine(*grid.transform))

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

"""NYC Hydraulic Surface Conditioning (Phase 6A).

Turns a bare LiDAR DTM into a *simulation-ready hydraulic terrain* by fusing the
official NYC/NYS/USGS planimetric and LiDAR layers into the fields the solver
consumes (see physics/swe.py Phase-6A inputs):

    conditioned elevation   (breakline/hydro-enforcement — future refinement)
    no-flow building mask    -> grid.obstacle              (ingestion.buildings)
    subgrid barrier crests   -> grid.crest_x / crest_y     (curbs/medians/walls)
    building + road coverage -> grid.coverage / road_coverage
    Manning roughness        -> grid.manning               (surface class)
    infiltration capacity    -> grid.infiltration_rate/_capacity (land cover)
    drainage sinks           -> grid.drainage              (inlets)
    bridge/culvert conduits  -> grid.connections

Every layer is a fiona-readable ``FeatureLayer`` (GeoPackage / Shapefile /
GeoJSON) reprojected to the run's metric CRS, feet→meters, NAVD88 vertical, with
source version/date/URL and a SHA-256 content digest recorded in provenance.

Data availability: the official hosts (NYC OpenData, gis.ny.gov, gis.nyc.gov)
are frequently unreachable from locked-down build environments; this module is
the *engine* — point each layer at a downloaded file and it conditions the grid.
The canonical source registry (:data:`NYC_SOURCES`) records where each layer
comes from so provenance is complete regardless of transport.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from aqua_sim.grid import Grid, Matrix, zeros

FT_TO_M = 0.3048

# --- Canonical official source registry (for provenance) --------------------
# URLs are the systems of record; a run records which transport file was used
# plus its SHA-256, so results trace back to inputs even via a mirror.
NYC_SOURCES = {
    "dtm_dsm_lidar": {
        "name": "NYC 2017 1-ft Topobathymetric LiDAR DTM/DSM + classified cloud",
        "url": "https://gis.ny.gov/nys-dem",
        "vertical_datum": "NAVD88", "units": "feet", "native_crs": "EPSG:2263",
    },
    "building_footprints": {
        "name": "NYC Building Footprints (nqwf-w8eh)",
        "url": "https://data.cityofnewyork.us/Housing-Development/Building-Footprints/nqwf-w8eh",
        "vertical_datum": "NAVD88", "units": "feet", "native_crs": "EPSG:2263",
    },
    "roadbed": {
        "name": "NYC Planimetric Database: Roadbed (updated Dec 2025)",
        "url": "https://catalog.data.gov/dataset/nyc-planimetric-database-roadbed",
        "native_crs": "EPSG:2263",
    },
    "sidewalk": {"name": "NYC Planimetric: Sidewalk",
                 "url": "https://data.cityofnewyork.us/City-Government/Sidewalk/vfx9-tbb6",
                 "native_crs": "EPSG:2263"},
    "pavement_edge": {"name": "NYC Planimetric: Pavement Edge",
                      "url": "https://data.cityofnewyork.us/City-Government/Pavement-Edge/", "native_crs": "EPSG:2263"},
    "median": {"name": "NYC Planimetric: Median",
               "url": "https://data.cityofnewyork.us/City-Government/Median/", "native_crs": "EPSG:2263"},
    "retaining_wall": {"name": "NYC Planimetric: Retaining Wall",
                       "url": "https://data.cityofnewyork.us/City-Government/Retaining-Wall/", "native_crs": "EPSG:2263"},
    "transport_structure": {"name": "NYC Planimetric: Transportation Structure (bridges/viaducts)",
                            "url": "https://data.cityofnewyork.us/City-Government/Transportation-Structure/", "native_crs": "EPSG:2263"},
    "hydrography": {"name": "NYC Planimetric: Hydrography / Shoreline",
                    "url": "https://data.cityofnewyork.us/City-Government/Hydrography/", "native_crs": "EPSG:2263"},
    "landcover": {"name": "NYC 6-in Land Cover / Impervious",
                  "url": "https://data.cityofnewyork.us/Environment/Land-Cover-Raster-Data-2017-6in-Resolution/", "native_crs": "EPSG:2263"},
    "drainage_inlet": {"name": "NYC DEP Green Infrastructure / catch-basin inlets (where public)",
                       "url": "https://data.cityofnewyork.us/Environment/", "native_crs": "EPSG:2263"},
    "map_tiles": {"name": "NYC Map Tiles (planimetric basemap, quarterly)",
                  "url": "https://gis.nyc.gov/tiles/"},
    "orthoimagery": {"name": "NYS/NYC Orthoimagery (2024)",
                     "url": "https://gis.ny.gov/new-york-city-orthoimagery-downloads"},
}

# --- Surface-class hydraulic properties (Manning n; infiltration mm/hr) ------
# Roads/sidewalks/buildings are impervious (fast, no infiltration); pervious
# land infiltrates. Values are standard urban-hydrology figures.
SURFACE_PROPS = {
    "road":     {"manning": 0.013, "infil_mm_hr": 0.0,  "impervious": True},
    "sidewalk": {"manning": 0.012, "infil_mm_hr": 0.0,  "impervious": True},
    "pavement": {"manning": 0.014, "infil_mm_hr": 0.0,  "impervious": True},
    "building": {"manning": 0.015, "infil_mm_hr": 0.0,  "impervious": True},
    "water":    {"manning": 0.030, "infil_mm_hr": 0.0,  "impervious": True},
    "grass":    {"manning": 0.035, "infil_mm_hr": 25.0, "impervious": False},
    "tree":     {"manning": 0.120, "infil_mm_hr": 50.0, "impervious": False},
    "bare_soil":{"manning": 0.025, "infil_mm_hr": 15.0, "impervious": False},
    "default":  {"manning": 0.030, "infil_mm_hr": 10.0, "impervious": False},
}
#: Default total infiltration capacity for pervious land (m). ~50 mm sponge.
DEFAULT_INFIL_CAPACITY_M = 0.05


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FeatureLayer:
    """One official GIS layer to condition into the grid.

    Args:
        path: fiona-readable file (.gpkg/.shp/.geojson).
        kind: registry key in :data:`NYC_SOURCES` (drives provenance + defaults).
        source_crs: override when the file lacks embedded CRS.
        height_attr: attribute giving a barrier/wall height (feet, NYC).
    """
    path: str
    kind: str
    source_crs: Optional[str] = None
    height_attr: Optional[str] = None
    transport_url: Optional[str] = None

    def provenance(self) -> dict:
        reg = NYC_SOURCES.get(self.kind, {"name": self.kind, "url": None})
        return {
            "kind": self.kind, "name": reg.get("name"),
            "official_url": reg.get("url"), "transport_url": self.transport_url,
            "source_file": os.path.basename(self.path),
            "source_sha256": _sha256(self.path),
            "vertical_datum": reg.get("vertical_datum"),
            "native_crs": reg.get("native_crs"),
        }

    def read_metric(self, grid: Grid, aoi_bounds):
        """Yield (geometry-in-grid-CRS, properties) clipped to the AOI."""
        import fiona
        from rasterio.warp import transform_bounds, transform_geom
        with fiona.open(self.path) as src:
            src_crs = self.source_crs
            if not src_crs and src.crs:
                epsg = src.crs.to_epsg()
                src_crs = f"EPSG:{epsg}" if epsg else src.crs_wkt
            if not src_crs:
                raise ValueError(f"{self.path!r}: no CRS; set source_crs=")
            bbox = transform_bounds("EPSG:4326", src_crs, *aoi_bounds, densify_pts=21)
            for feat in src.filter(bbox=bbox):
                geom = feat["geometry"]
                if not geom:
                    continue
                yield transform_geom(src_crs, grid.crs, dict(geom)), dict(feat["properties"])


@dataclass
class ConditioningReport:
    provenance: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def _grid_bounds_wgs84(grid: Grid):
    from rasterio.warp import transform_bounds
    a, _, left, _, e, top = grid.transform
    right = left + grid.nx * a
    bottom = top + grid.ny * e
    return transform_bounds(grid.crs, "EPSG:4326", left, bottom, right, top, densify_pts=21)


def _rasterize(shapes, grid, dtype="uint8", all_touched=True, merge="replace"):
    import numpy as np
    from rasterio import features as rfeatures
    from rasterio.enums import MergeAlg
    from rasterio.transform import Affine
    if not shapes:
        return np.zeros((grid.ny, grid.nx), dtype=dtype)
    return rfeatures.rasterize(
        shapes, out_shape=(grid.ny, grid.nx), transform=Affine(*grid.transform),
        dtype=dtype, all_touched=all_touched,
        merge_alg=MergeAlg.add if merge == "add" else MergeAlg.replace)


def burn_surface_classes(grid: Grid, layers: dict[str, FeatureLayer],
                         aoi_bounds=None) -> ConditioningReport:
    """Rasterize surface-class layers → per-cell Manning + infiltration +
    road coverage. ``layers`` maps a SURFACE_PROPS class (e.g. 'road',
    'sidewalk', 'water', 'landcover') to a :class:`FeatureLayer`.

    Priority: impervious surfaces (road/sidewalk/pavement/water) override
    pervious land cover where they overlap (they physically pave it).
    """
    import numpy as np
    if grid.crs is None or grid.transform is None:
        raise ValueError("Condition after DEM ingestion (grid needs CRS+transform).")
    aoi_bounds = aoi_bounds or _grid_bounds_wgs84(grid)

    manning = np.full((grid.ny, grid.nx), SURFACE_PROPS["default"]["manning"])
    infil_mm = np.full((grid.ny, grid.nx), SURFACE_PROPS["default"]["infil_mm_hr"])
    road_cov = np.zeros((grid.ny, grid.nx))
    prov = {}
    # Apply pervious land cover first, then pave impervious over it.
    order = ["landcover", "grass", "tree", "bare_soil", "water",
             "pavement", "sidewalk", "road"]
    for cls in order:
        layer = layers.get(cls)
        if layer is None:
            continue
        prov[cls] = layer.provenance()
        shapes = [(geom, 1) for geom, _ in layer.read_metric(grid, aoi_bounds)]
        mask = _rasterize(shapes, grid).astype(bool)
        props = SURFACE_PROPS.get(cls, SURFACE_PROPS["default"])
        manning[mask] = props["manning"]
        infil_mm[mask] = props["infil_mm_hr"]
        if cls in ("road", "pavement"):
            road_cov = np.maximum(road_cov, mask.astype(float))

    grid.manning = manning.tolist()
    grid.infiltration_rate = (infil_mm / 1000.0 / 3600.0).tolist()
    grid.infiltration_capacity = [
        [DEFAULT_INFIL_CAPACITY_M if infil_mm[y][x] > 0 else 0.0
         for x in range(grid.nx)] for y in range(grid.ny)]
    grid.road_coverage = road_cov.tolist()
    grid.meta.setdefault("conditioning", {})["surfaces"] = prov
    return ConditioningReport(provenance=prov,
                              stats={"impervious_cells": int((infil_mm == 0).sum())})


def burn_barriers(grid: Grid, layers: list[FeatureLayer], aoi_bounds=None,
                  default_height_m: float = 0.15) -> ConditioningReport:
    """Condition curbs/medians/retaining walls into the subgrid face-crest layer.

    Robust method: rasterize each barrier line to the connected set of grid
    cells it crosses (``all_touched``), giving each a barrier height (max where
    they overlap). A face's crest is then the max of its two adjacent barrier
    cells' surface heights (bed + barrier height): flow across that face only
    once the water tops the barrier. Because a barrier is a *connected* run of
    cells each blocked on all four faces, it cannot leak diagonally from
    reprojection jitter — the failure mode of a per-sample face walk.

    Curb/median default 0.15 m; retaining walls use their height attribute
    (feet→m) or a 2 m default.
    """
    import numpy as np
    from rasterio import features as rfeatures
    from rasterio.transform import Affine
    aoi_bounds = aoi_bounds or _grid_bounds_wgs84(grid)
    ny, nx = grid.ny, grid.nx
    z = np.asarray(grid.z)

    shapes = []
    prov = {}
    for layer in layers:
        prov[layer.kind] = layer.provenance()
        default_h = (2.0 if layer.kind == "retaining_wall" else default_height_m)
        for geom, props in layer.read_metric(grid, aoi_bounds):
            h_m = default_h
            if layer.height_attr and props.get(layer.height_attr) is not None:
                try:
                    h_m = max(float(props[layer.height_attr]) * FT_TO_M, 0.0) or default_h
                except (TypeError, ValueError):
                    h_m = default_h
            shapes.append((geom, h_m))
    shapes.sort(key=lambda s: s[1])  # ascending: tallest rasterized last (== max)

    if shapes:
        barh = rfeatures.rasterize(shapes, out_shape=(ny, nx),
                                   transform=Affine(*grid.transform),
                                   dtype="float64", all_touched=True)
    else:
        barh = np.zeros((ny, nx))
    # Per-cell barrier surface elevation; -inf where no barrier.
    cell_crest = np.where(barh > 0.0, z + barh, float("-inf"))
    # Face crest = max of the two adjacent cells' barrier surface.
    crest_x = np.full((ny, nx + 1), float("-inf"))
    crest_x[:, 1:nx] = np.maximum(cell_crest[:, :-1], cell_crest[:, 1:])
    crest_x[:, 0] = cell_crest[:, 0]
    crest_x[:, nx] = cell_crest[:, -1]
    crest_y = np.full((ny + 1, nx), float("-inf"))
    crest_y[1:ny, :] = np.maximum(cell_crest[:-1, :], cell_crest[1:, :])
    crest_y[0, :] = cell_crest[0, :]
    crest_y[ny, :] = cell_crest[-1, :]

    grid.crest_x = crest_x.tolist()
    grid.crest_y = crest_y.tolist()
    grid.meta.setdefault("conditioning", {})["barriers"] = prov
    return ConditioningReport(provenance=prov,
                              stats={"barrier_cells": int((barh > 0.0).sum())})


def add_drainage_inlets(grid: Grid, layer: FeatureLayer,
                        per_inlet_capacity_mm_hr: float = 50.0,
                        aoi_bounds=None) -> ConditioningReport:
    """Point inlets → per-cell drainage sink (mm/hr → m/s). Each cell containing
    an inlet drains standing water up to the inlet capacity."""
    import numpy as np
    aoi_bounds = aoi_bounds or _grid_bounds_wgs84(grid)
    a, _, left, _, e, top = grid.transform
    drain = np.zeros((grid.ny, grid.nx))
    rate = per_inlet_capacity_mm_hr / 1000.0 / 3600.0
    n = 0
    for geom, _ in layer.read_metric(grid, aoi_bounds):
        for x, y in _iter_points(geom):
            i, j = int((x - left) / a), int((top - y) / (-e))
            if 0 <= i < grid.nx and 0 <= j < grid.ny:
                drain[j][i] += rate
                n += 1
    grid.drainage = drain.tolist()
    grid.meta.setdefault("conditioning", {})["drainage"] = {
        **layer.provenance(), "inlets_in_aoi": n,
        "per_inlet_capacity_mm_hr": per_inlet_capacity_mm_hr}
    return ConditioningReport(provenance=layer.provenance(), stats={"inlets": n})


def _iter_points(geom):
    t = geom.get("type")
    if t == "Point":
        yield tuple(geom["coordinates"][:2])
    elif t == "MultiPoint":
        for p in geom["coordinates"]:
            yield tuple(p[:2])


def add_culverts(grid: Grid, connections_lonlat: Iterable[tuple],
                 default_cd_area: float = 3.0) -> ConditioningReport:
    """Register bridge/culvert/underpass conduits from (lon1,lat1,lon2,lat2[,cd_area])
    so flow passes an embankment instead of false-damming. Coordinates are
    mapped to grid cells; conveyance defaults to a ~3 m² Cd·A culvert."""
    from rasterio.warp import transform as warp_transform
    a, _, left, _, e, top = grid.transform
    conns = list(grid.connections or [])
    added = 0
    for row in connections_lonlat:
        lon1, lat1, lon2, lat2 = row[:4]
        cd = row[4] if len(row) > 4 else default_cd_area
        xs, ys = warp_transform("EPSG:4326", grid.crs, [lon1, lon2], [lat1, lat2])
        i1, j1 = int((xs[0] - left) / a), int((top - ys[0]) / (-e))
        i2, j2 = int((xs[1] - left) / a), int((top - ys[1]) / (-e))
        if all(0 <= i < grid.nx for i in (i1, i2)) and all(0 <= j < grid.ny for j in (j1, j2)):
            conns.append((i1, j1, i2, j2, float(cd)))
            added += 1
    grid.connections = conns
    grid.meta.setdefault("conditioning", {}).setdefault("culverts", {})["count"] = added
    return ConditioningReport(stats={"culverts": added})

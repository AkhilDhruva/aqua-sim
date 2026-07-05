# aqua-sim — Data Sourcing (Manhattan AOI)

Companion to [`DATA_INGESTION.md`](./DATA_INGESTION.md) (which covers *formats* and
the DTM/DSM distinction). This doc answers two concrete questions:

1. **What is a DEM**, plainly.
2. **Which public datasets** we use for the Manhattan area of interest, since you
   don't have your own drone/LiDAR yet.

## 1. What is a DEM?

A **Digital Elevation Model** is a raster — a grid of pixels — where each pixel
holds a **ground elevation in meters**, georeferenced to real-world coordinates.
Picture a greyscale image where brightness = height: bright = high ground, dark =
low ground. That grid *is* the terrain the water flows over.

Every DEM has three properties that matter for flood modeling:

- **Resolution** — meters per pixel. 1 m resolves individual streets and curbs;
  30 m only resolves neighborhoods. Finer = better, but heavier to simulate.
- **Vertical accuracy** — how close each elevation is to truth. Flood depth is
  `water level − ground`, so a 30 cm elevation error is a 30 cm depth error.
- **Surface type** — **DTM** (bare earth, what we flood) vs **DSM** (tops of
  buildings/trees). See `DATA_INGESTION.md` §1. We flood the DTM and treat
  buildings as obstacles.

Under the hood, high-quality public DEMs are usually **derived from LiDAR**: an
aircraft sweeps the city with a laser, and the classified ground returns are
rasterized into a bare-earth DEM. So "use a public DEM" and "use public LiDAR"
are the same move here — the DEM is the processed LiDAR.

## 2. Manhattan datasets (public, no drone needed)

Ordered best-first for this AOI. All are ingested through
`ingestion.DEMSource` (GeoTIFF) or `ingestion.LidarSource` (point cloud) into the
same metric `Grid`.

| Dataset | Resolution | Type | Format | Why |
|---|---|---|---|---|
| **USGS 3DEP** (NYC) | 1 m | DEM (LiDAR-derived, bare earth ≈ DTM) | GeoTIFF | Best default: national, documented accuracy, 1 m detail. Via The National Map / OpenTopography. |
| **NYC OpenData — 1 ft DEM** | ~0.3 m | DEM (bare earth) | GeoTIFF/IMG | City's own product; very high resolution for street-scale flooding. |
| **NYC 2017 Topobathymetric LiDAR** (USGS/NOAA) | point cloud | LiDAR (ground + surface) | LAS/LAZ | Rawest source; lets us build both DTM *and* DSM (buildings) ourselves, and it includes near-shore bathymetry. |
| **Copernicus GLO-30** | 30 m | DSM (global) | GeoTIFF | Global fallback / regional screening only — too coarse for street detail. |

Practical notes:
- **CRS / units:** these arrive in geographic or State Plane coordinates. Ingestion
  reprojects to a **local UTM zone** (Manhattan → UTM 18N) so cell spacing is in
  meters, which the physics requires.
- **Datum:** vertical datum matters for coastal/storm-surge context (NAVD88 vs
  local mean sea level). Recorded in `Grid.meta` for reproducibility.
- **Buildings:** the 1 m DTM already excludes buildings; we burn building
  footprints (NYC OpenData "Building Footprints") back in as obstacles so water
  routes around them.
- **Licensing:** USGS 3DEP and NYC OpenData are open / public-domain — fine to use
  and redistribute results. Provenance is captured in every run's manifest.

## 3. Validation data (to move from "plausible" to "defensible")

Analytic benchmarks are in the code today (mass conservation, lake-at-rest — see
`tests/test_swe.py`). For real-world validation of the Manhattan AOI, candidate
events:

- **Hurricane Ida remnants (Sep 2021)** — extreme flash flooding in NYC, well
  documented, with reported flood locations to compare against.
- **FEMA National Flood Hazard Layer** — regulatory flood zones for the AOI; a
  sanity check that our high-risk cells overlap known flood zones.

Validation plan: reproduce a recorded rainfall hyetograph over the real DEM and
compare simulated inundation extent / flood-prone cells against the observed map.

## 4. How this maps to the code

`ingestion.DEMSource` (Phase 1) is **implemented and tested**. It reads a GeoTIFF,
reprojects to metric UTM, resamples to a target cell size, clips to a WGS84 AOI
box, void-fills nodata, and returns the same `Grid` every other source produces —
so the solver, risk layer, and exporter run against it unchanged.

Install the geospatial extra and run on real terrain:

```bash
pip install -e ".[geo]"                      # rasterio, numpy, pyproj

# 1. Fetch a public USGS 3DEP tile of Manhattan (needs egress to USGS):
python -m aqua_sim.ingestion.fetch manhattan_3dep.tif

# 2. Run the full pipeline on it:
python -m aqua_sim run output/manhattan --dem manhattan_3dep.tif
```

Or in code:

```python
from aqua_sim.ingestion.dem import DEMSource
grid = DEMSource("manhattan_3dep.tif", target_dx_m=10.0,
                 aoi_bounds=(-74.02, 40.70, -73.93, 40.78)).load()
# grid.crs == "EPSG:32618" (UTM 18N), grid.dx == 10.0 m, ready for the solver.
```

**Egress note:** `elevation.nationalmap.gov` may be blocked by a restrictive
network policy (the egress proxy returns HTTP 403). In that case, fetch the tile
where outbound access to USGS is allowed and copy the `.tif` in; everything after
the download runs fully offline.

**Still synthetic (until a real tile is dropped in):**
`scenario.build_manhattan_demo()` builds a *Manhattan-scaled* terrain (island,
central ridge, enclosed low-lying basin) so the pipeline demonstrates flooding and
a subway breach with zero downloads. It is clearly labeled non-georeferenced;
`build_scenario_from_dem(path, aoi_bounds=...)` is the real-terrain path.

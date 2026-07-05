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

- **Today:** `scenario.build_manhattan_demo()` uses a *synthetic Manhattan-scaled*
  terrain (an island with a central ridge and an enclosed low-lying basin) so the
  full pipeline runs with zero downloads. It is clearly labeled non-georeferenced.
- **Phase 1 swap-in:** implement `ingestion.DEMSource.load()` to read a USGS 3DEP
  GeoTIFF tile of the AOI, reproject to UTM 18N, clip to the geofence, and return
  the same `Grid`. Nothing downstream changes — the solver, risk layer, and
  exporter already run against `Grid`.

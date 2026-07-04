# aqua-sim — Data Ingestion & Input Formats

Companion to [`PLANNING.md`](./PLANNING.md) and [`ARCHITECTURE.md`](./ARCHITECTURE.md).

This is the doc that answers Akhil's open question: *"what format do we feed —
LiDAR or photogrammetry — and how does it get converted to a useful form?"*

The short answer: **all sources converge to the same product — a metric,
georeferenced `Grid` with a bare-earth surface (DTM) plus a building/obstacle
layer.** The solver never sees a `.laz` or a drone photo; it sees a `Grid`. That
one rule keeps every input format pluggable.

```
                 ┌── Public DEM (GeoTIFF) ──┐
 raw sources ────┼── LiDAR (.las/.laz) ─────┼──► normalize ──► Grid ──► solver
                 └── Drone photos (SfM) ─────┘   (DTM + obstacles, metric, CRS)
```

## 1. The single most important distinction: DTM vs DSM

Get this wrong and every flood result is wrong.

- **DSM — Digital *Surface* Model.** Top of everything: rooftops, tree canopy,
  cars. This is what raw photogrammetry and first-return LiDAR give you.
- **DTM — Digital *Terrain* Model.** Bare earth with buildings/vegetation
  removed. This is the surface water actually flows over.

**We flood the DTM, and treat buildings as obstacles** (derived from
`DSM − DTM`, i.e. the height of stuff sitting on the ground). Flooding a raw DSM
makes water sit on rooftops and treats tree canopy as a hill — a classic,
result-invalidating mistake. So every ingestion path must produce **both** a DTM
and a building/obstacle layer.

## 2. The common target: `TerrainSource` → `Grid`

Every input format is implemented as a `TerrainSource` (see
`src/aqua_sim/ingestion/base.py`) that yields a `Grid` with:

- `z` — DTM elevation (m), in a **projected, metric CRS** (local UTM).
- `obstacle` — building footprints / heights burned in.
- `manning` — roughness from land cover (optional at ingest; defaultable).
- `mask` — the geofenced area of interest.
- metadata — CRS, affine transform, resolution, source provenance, timestamp.

Because the contract is identical, the physics and risk layers are written **once**
and never care where the terrain came from.

## 3. Source A — Public DEM (recommended starting point, Phase 1)

The fastest path to a working solver, requiring **zero field data**.

- **Formats:** GeoTIFF (`.tif`) primarily; also `.img`, `.asc`.
- **Sources:** USGS 3DEP (1 m / 10 m, US), Copernicus GLO-30 (global 30 m),
  SRTM (30 m), national LiDAR-derived DTM products.
- **Pipeline:** `rasterio` read → reproject to local UTM (`pyproj`) → resample to
  target cell size → clip to geofence → fill/void-repair → `Grid`.
- **Caveat:** many public DEMs are effectively DTMs already (good), but coarse
  (10–30 m). Fine enough for regional screening, too coarse to resolve individual
  streets. That's exactly why drone/LiDAR matters for the final vision.

**Why start here:** it lets P2 (physics), P3 (risk), and P4 (viewer) be built and
validated now, while the harder drone pipeline (P5) matures in parallel.

## 4. Source B — LiDAR point clouds (Phase 5)

The gold standard for bare-earth accuracy, especially under vegetation.

- **Formats:** `.las` / `.laz` (compressed). Standard, well-tooled.
- **Tooling:** `PDAL` (pipeline), `laspy` (Python read).
- **Pipeline:**
  1. Read + reproject to metric CRS.
  2. **Ground classification** (PDAL `filters.smrf` or `filters.pmf`) to separate
     ground from non-ground returns.
  3. Rasterize ground returns → **DTM**; rasterize all/first returns → **DSM**.
  4. `DSM − DTM` → building/obstacle heights.
  5. → `Grid`.
- **Strengths:** LiDAR penetrates canopy (multiple returns), giving clean bare
  earth. Absolute accuracy is typically excellent when the survey is georeferenced.
- **Sourcing:** aerial LiDAR is increasingly available as open data for many
  cities; drone-mounted LiDAR is an option for bespoke high-resolution capture.

## 5. Source C — Drone photogrammetry (Phase 5, the 2018 vision)

Akhil's original concept: **swarm drones capture overlapping photos →
Structure-from-Motion reconstructs 3D → stitched terrain → risk zoning.** This is
the most bespoke and highest-resolution path, and the most operationally demanding.

- **Input:** a set of overlapping geotagged images (JPEG/TIFF + EXIF GPS), ideally
  with a few surveyed **Ground Control Points (GCPs)** or RTK/PPK drone positioning.
- **SfM/MVS tooling:** OpenDroneMap (open source, scriptable), COLMAP, or
  commercial (Metashape/Pix4D). Output: dense point cloud + orthomosaic + DSM.
- **Pipeline (produces the same `Grid`):**
  1. **Capture** with sufficient overlap: ≈ 75% frontlap / 65% sidelap, consistent
     altitude → uniform Ground Sampling Distance (GSD).
  2. **SfM** → sparse cloud + camera poses; **MVS** → dense point cloud.
  3. **Georeference** using GCPs / RTK — *this sets absolute vertical accuracy.*
  4. **Ground classification** on the dense cloud (e.g. Cloth Simulation Filter)
     → DTM; full cloud → DSM.
  5. Rasterize DTM + `DSM − DTM` obstacles → `Grid`.

### Why vertical accuracy is the whole game here
Flood depth is the difference between water level and ground. A 30 cm vertical
error in the terrain is a 30 cm error in every depth reading — enough to flip a
"dry" street to "flooded." So for photogrammetry:

- **GCPs / RTK are not optional** for quantitative flood work. Photo-only SfM has
  good *relative* geometry but can drift in *absolute* elevation.
- **Water and glass** reconstruct poorly (no texture / reflections) — expect holes
  over ponds and rivers that need void-filling.
- **Moving objects** (cars, people) create noise → filter out.

### LiDAR vs photogrammetry — the honest comparison

| | Drone photogrammetry | LiDAR |
|---|---|---|
| Bare earth under trees | Poor (canopy blocks view) | Good (multiple returns) |
| Absolute vertical accuracy | Good *only with* GCPs/RTK | Typically excellent |
| Also produces imagery/texture | Yes (orthomosaic) | No (intensity only) |
| Cost / accessibility | Cheaper hardware, heavy compute | Pricier sensor, lighter processing |
| Best for | Texture-rich open urban terrain | Vegetated / high-accuracy needs |

**Recommendation:** support both; where feasible **fuse** them — LiDAR for the
bare-earth DTM, photogrammetry for imagery/context and building detail. Both are
just `TerrainSource` implementations behind the same `Grid` contract.

## 6. Geofencing / Area of Interest

The user defines an AOI polygon (or bbox). Ingestion **clips** terrain to it and
sets the `mask`; the solver treats the AOI edge as an open/outflow boundary
(unless it's a known wall). Keeping the domain tight to the AOI is also the main
lever on compute cost.

## 7. Reproducibility rules

Every ingested dataset records: source type, original CRS, target CRS, resolution,
processing steps, and a capture/processing timestamp — stored in the `Grid`
metadata and echoed into the run manifest. A flood result you can't trace back to
its terrain provenance is not defensible.

## 8. Phase plan for ingestion

1. **P1:** GeoTIFF DEM source — the dependency-light path to a real `Grid`.
2. **P5a:** LiDAR source (PDAL) — ground classification → DTM/DSM.
3. **P5b:** Photogrammetry source — wrap OpenDroneMap output (point cloud/DSM) →
   DTM/DSM; document the capture spec (overlap, GCPs, GSD) for drone operators.

Until then, `ingestion/synthetic.py` provides a dependency-free `TerrainSource`
so the solver can be developed against the real contract with no data at all.

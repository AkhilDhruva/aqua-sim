# aqua-sim — Flood Zone Risk Simulator

A hydrodynamic **digital twin** that turns real terrain (public DEM, LiDAR, or
drone photogrammetry) into a physically-grounded 2D flood simulation, then ranks
the **risk zones** and subterranean **sink nodes** inside a geofenced area:

> *If it rains like this, where does the water go, how deep does it get, how fast
> is it moving, and which specific assets flood — and when?*

This is a **decision tool**, not a video-game water effect. Every design choice
biases toward physical correctness and reproducibility.

## ⚑ Resuming work? Read [`Last operation results.md`](Last%20operation%20results.md) first

That file is the running checkpoint: current state, what is proven, what data is
blocked, and the prioritized next steps. **Read it before continuing, and update
it at the end of each session.**

## Lower Manhattan replica (latest)

Battery to 14th Street, from public data only: USGS 1 m terrain and 3D
buildings derived from the city's LiDAR, with real roof setbacks. The bridges
and the FDR viaduct are kept out of the walls. The Ida storm run is recorded in
[`examples/runs/lm_lidar_4m`](examples/runs/lm_lidar_4m/README.md) and the
input data in [`examples/data`](examples/data/README.md).

## Documentation (start here)

- **[Last operation results.md](Last%20operation%20results.md)** — ⚑ session
  checkpoint: status, runs produced, blocked data, next steps.
- **[docs/PLANNING.md](docs/PLANNING.md)** — vision, scope, key decisions, and the
  phased roadmap. The single source of truth.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — system layers, the
  shallow-water physics engine, the pre-computed (offline-solve) design, data flow.
- **[docs/CONDITIONING.md](docs/CONDITIONING.md)** — Phase 6A hydraulic surface
  conditioning: barriers, infiltration, drainage inlets, culverts.
- **[docs/DATA_INGESTION.md](docs/DATA_INGESTION.md)** — DEM / LiDAR /
  drone-photogrammetry input formats and how they converge to one terrain grid.
- **[docs/DATA_SOURCING.md](docs/DATA_SOURCING.md)** — what a DEM is, and the
  official NYC datasets (USGS 3DEP, NYC LiDAR, building footprints) we use.
- **[docs/VALIDATION.md](docs/VALIDATION.md)** — Hurricane Ida 2021 historical
  validation: method, score, and honest limits.
- **[viz/README.md](viz/README.md)** — dashboard layers, contract, attribution.

## Status: conditioned, building-aware, historically validated (P0–P6A)

The **shallow-water solver is real and validated** in two backends held in
cell-for-cell lockstep, real multi-tile GeoTIFF DEM ingestion works, official
NYC building footprints become no-flow walls at street resolution, the Phase 6A
conditioning engine fuses planimetric layers into a hydraulic surface, and the
**Three.js telemetry dashboard** (`viz/`) renders any exported run — kinetic
depth×velocity shader, 3D buildings, sensors, alert matrix, layer toggles.

**Historical validation:** driven by the documented Hurricane-Ida (2021)
hyetograph over real USGS 3DEP terrain, the model is scored against the subway
stations that actually flooded — see [docs/VALIDATION.md](docs/VALIDATION.md)
and `python -m aqua_sim.validation.ida2021`.

The solver is a local-inertial (LISFLOOD-FP / Bates 2010) scheme, is
**mass-conserving to floating-point** and **well-balanced** (lake-at-rest), with a
flux limiter guaranteeing non-negative depths and CFL-adaptive timestepping —
all unit-tested in `tests/test_swe.py`.

## Quick start (Python 3.10+)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m aqua_sim                 # self-check of the wired components
python -m aqua_sim run output/run  # full Manhattan demo: solver -> risk -> frames
pytest                             # test suite (physics validation + end-to-end)
```

Run on a **real DEM** (adds rasterio/numpy):

```bash
pip install -e ".[geo]"
python -m aqua_sim.ingestion.fetch manhattan_3dep.tif        # USGS 3DEP (needs egress)
python -m aqua_sim run output/mn --dem manhattan_3dep.tif    # reproject -> solve -> frames
```

Run a **named area** — tiles are mosaicked automatically, and each run is
labeled *street* vs *screening* resolution so the two are never confused:

```bash
# Complete New York area: Staten Island -> Long Island, 3 USGS tiles
python -m aqua_sim run output/ny --area ny \
  --dem USGS_13_n41w075.tif --dem USGS_13_n41w074.tif --dem USGS_13_n41w073.tif

# Five boroughs, finer, with official building footprints as no-flow walls
python -m aqua_sim run output/metro --area metro --dx 30 \
  --dem USGS_13_n41w074.tif --dem USGS_13_n41w075.tif \
  --buildings nyc_building_footprints.gpkg
```

| `--area` | Extent | Default `--dx` |
|---|---|---|
| `ny` | Staten Island → Long Island | 250 m (screening) |
| `metro` | Five boroughs | 120 m (screening) |
| `manhattan` | Manhattan + waterfront | 20 m |

At `--dx ≤ 10` building footprints become closed no-flow cells (water routes
through the streets); above that they stay coverage-only — see
[docs/CONDITIONING.md](docs/CONDITIONING.md).

### Getting the terrain

DEM tiles come from the public USGS 3DEP S3 bucket (no API key). Tiles are
1°×1°, named by their **north-west corner** — New York City needs `n41w074`
(Manhattan/Brooklyn/Queens/Bronx), `n41w075` (Staten Island, west), and
`n41w073` (Long Island, east):

```bash
python - <<'EOF'
from aqua_sim.ingestion.fetch import fetch_3dep_s3
for lon in (75, 74, 73):
    fetch_3dep_s3(f"USGS_13_n41w0{lon}.tif", lat_north=41, lon_west=lon, resolution="13")
EOF
```

`resolution="13"` is 1/3 arc-second (~10 m, ~170 MB/tile); `"1"` is 1 arc-second
(~30 m, ~20 MB). Note `n41w072` (Montauk / the outer forks) has **no** staged
USGS product, so the eastern tip of Long Island is outside coverage.

### Conditioning a hydraulic surface (Phase 6A)

A bare DTM floods like an open field. To make it behave like a *city* — curbs
and retaining walls that hold water, roads that channel it, inlets that drain
it, culverts that stop embankments from false-damming — condition the grid with
official planimetric layers before solving:

```python
from aqua_sim.ingestion.dem import DEMSource
from aqua_sim.ingestion.buildings import BuildingsSource, apply_buildings
from aqua_sim.ingestion.conditioning import (
    FeatureLayer, burn_surface_classes, burn_barriers,
    add_drainage_inlets, add_culverts)

grid = DEMSource("nyc_topobathy_dtm.tif", target_dx_m=2.0, aoi_bounds=AOI).load()
apply_buildings(grid, BuildingsSource("building_footprints.gpkg").load_for_grid(grid))
burn_surface_classes(grid, {                       # Manning + infiltration
    "landcover": FeatureLayer("landcover.gpkg", "landcover"),
    "road":      FeatureLayer("roadbed.gpkg", "roadbed"),
    "sidewalk":  FeatureLayer("sidewalk.gpkg", "sidewalk")})
burn_barriers(grid, [                              # subgrid crests
    FeatureLayer("median.gpkg", "median"),
    FeatureLayer("retaining_wall.gpkg", "retaining_wall", height_attr="HEIGHT")])
add_drainage_inlets(grid, FeatureLayer("catch_basins.gpkg", "drainage_inlet"))
add_culverts(grid, [(-73.99, 40.72, -73.989, 40.721, 3.0)])
```

Every layer is reprojected to the run's metric CRS, converted feet→meters, and
recorded in provenance with its official URL and a SHA-256 digest.

### Historical validation

```bash
python -m aqua_sim.validation.ida2021 USGS_13_n41w074.tif viz/ida_run 30
```

Runs the documented Hurricane-Ida hyetograph over real terrain under a drainage
sensitivity matrix and scores detection against the subway stations that
actually flooded. Writes `validation.json`.

### Choosing a solver backend

`auto` (default) uses the vectorized NumPy backend when NumPy is importable and
the pure-Python reference otherwise. The **resolved** backend is recorded in run
provenance, because the two can differ at float-noise level:

```python
SolverConfig(backend="numpy")      # or "reference" (readable spec), or "auto"
```

Both are held in cell-for-cell lockstep (≤1e-9) by `tests/test_swe_numpy.py`.

View a run in the **3D telemetry dashboard**:

```bash
python -m http.server 8000
# open http://localhost:8000/viz/          (sample run included in the repo)
# or    http://localhost:8000/viz/?run=../output/mn
```

A run writes `manifest.json` (with a provenance block), `terrain.json`,
`frame_001.json … frame_NNN.json`, and `alerts.json` into the output folder.

## Project layout

```
aqua-sim/
├── Last operation results.md # ⚑ session checkpoint — READ FIRST
├── docs/                     # PLANNING, ARCHITECTURE, CONDITIONING, VALIDATION, DATA_*
│   └── validation/           #    scored Ida-2021 report (committed)
├── src/aqua_sim/
│   ├── config.py             # run config (storm incl. hyetograph, solver) + units
│   ├── grid.py               # the core metric Grid — every layer's input/output
│   ├── scenario.py           # AREAS registry, area/DEM/demo builders, run_scenario
│   ├── geofence.py           # area-of-interest masking
│   ├── ingestion/            # 1. sources → Grid
│   │   ├── base.py           #    TerrainSource contract
│   │   ├── synthetic.py      #    dependency-free test terrain
│   │   ├── dem.py            #    GeoTIFF DEM (multi-tile mosaic) → UTM Grid
│   │   ├── fetch.py          #    download USGS 3DEP tiles (S3 + ArcGIS)
│   │   ├── buildings.py      #    official footprints → walls/coverage + viewer export
│   │   ├── conditioning.py   #    Phase 6A hydraulic surface conditioning
│   │   ├── lidar.py          #    LiDAR .las/.laz        (planned)
│   │   └── photogrammetry.py #    drone SfM              (planned)
│   ├── physics/              # 2. shallow-water solver
│   │   ├── swe.py            #    local-inertial SWE — the readable reference
│   │   ├── swe_numpy.py      #    vectorized twin (~20x), lockstep-tested
│   │   ├── stability.py      #    CFL timestep
│   │   ├── friction.py       #    Manning roughness
│   │   ├── boundary.py       #    open / closed / inflow
│   │   └── infiltration.py   #    loss models (Green-Ampt planned)
│   ├── risk/                 # 3. hazard, sink nodes, alerts
│   │   ├── hazard.py         #    depth×velocity hazard classes
│   │   ├── sink_nodes.py     #    orifice breach inflow
│   │   └── alerts.py         #    time-stamped, ranked risk log
│   ├── validation/           # historical + hydraulic validation
│   │   ├── ida2021.py        #    Hurricane Ida scored validation
│   │   └── hydraulic.py      #    cross-sections, multi-resolution comparison
│   └── export/frames.py      # 4. run export (manifest + frames + provenance)
├── viz/                      # 4. Three.js telemetry dashboard
│   ├── index.html            #    command-center UI (layers, presets, alerts)
│   ├── app.js                #    scene, kinetic GLSL water shader, picking
│   ├── buildings-layer.js    #    tiled/LOD extruded buildings + flood stats
│   ├── palettes.js           #    4 switchable color ramps + thresholds
│   ├── sample_run/           #    bundled demo run (committed, works on clone)
│   └── vendor/               #    vendored Three.js (offline, no CDN)
├── tests/                    # 77 tests: physics, equivalence, ingestion, e2e
└── pyproject.toml            # `dev` and `geo` extras
```

Dependencies are introduced **phase by phase** — the core is pure Python, so the
repo is always installable and testable at head; `[geo]` adds rasterio/numpy for
real DEM, building, and conditioning ingestion.

## Conventions

- **Runs are gitignored, code and scored reports are committed.** Every run
  embeds a `run_id` — a SHA-256 of its provenance *including a terrain content
  digest* — so a regenerated run is verifiable against a recorded one.
- **Screening vs street resolution** is stated in every run's AOI name.
  Buildings become no-flow walls only at `dx ≤ 10 m`.
- **No Google-derived geometry** in the model or exports; Google 3D Tiles is a
  disabled, presentation-only viewer toggle needing a key and live attribution.
- **Honesty over polish** — blocked data is reported as blocked, failed
  validation attempts stay in the docs, and limitations live in the artifacts.

## Remote workflow

```bash
git pull
git checkout -b feature/x
# ... work ...
git add -A && git commit -m "..."
git push -u origin feature/x
```

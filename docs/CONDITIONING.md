# Phase 6A — NYC Hydraulic Surface Conditioning

Turning a bare LiDAR DTM into a **simulation-ready hydraulic terrain** by fusing
the official NYC/NYS/USGS planimetric + LiDAR layers into the fields the solver
consumes. *"Create a conditioned hydraulic surface, not merely place map lines
over the DEM."*

Module: `src/aqua_sim/ingestion/conditioning.py` · Physics: `physics/swe.py`
(Phase-6A inputs) · Validation: `validation/hydraulic.py`,
`tests/test_conditioned_physics.py`.

## The pipeline

```
official layers ──▶ FeatureLayer (reproject → metric CRS, NAVD88, feet→m, SHA-256)
                    │
   DTM (LiDAR) ─────┼─▶ grid.z                 conditioned elevation
   footprints ──────┼─▶ grid.obstacle          no-flow building mask
   curbs/medians/   │
     retaining walls┼─▶ grid.crest_x/crest_y   subgrid barrier crests
   roadbed/sidewalk ┼─▶ grid.manning,          surface roughness +
     land cover     │   grid.infiltration_*,     infiltration capacity +
                    │   grid.road_coverage        road/building coverage
   drainage inlets ─┼─▶ grid.drainage          per-cell storm-drain sinks
   bridge/culvert  ─┴─▶ grid.connections       conduits (no false dams)
```

## Solver physics (all optional — a bare DTM runs the original path bit-identically)

| Field | Effect in the solver |
|---|---|
| `crest_x`, `crest_y` | Face crest raises the controlling bed: a face conveys flow only once the water surface **tops the barrier** (`hflow = max(η) − max(z_a, z_b, crest)`). Curbs/medians/retaining walls block and route flow; overtopping spills. |
| `infiltration_rate` + `infiltration_capacity` | Per-cell rain loss, rate-limited and capped by remaining capacity (Green-Ampt-ish). |
| `drainage` | Per-cell storm-drain-inlet sink (falls back to the scalar storm drainage where absent). |
| `connections` | Bridge/culvert/underpass conduits move water between (possibly non-adjacent) cells by head — an embankment with a culvert passes flow instead of false-damming. |

All four are implemented in **both** solver backends (reference + NumPy) and the
dual-backend equivalence test covers the conditioned path too
(`tests/test_conditioned_physics.py::test_conditioned_backends_agree`). When
none are set the depth update and face flux are **bit-identical** to a bare-DTM
run, so every prior equivalence/benchmark test is preserved.

## Resolution policy (matches the buildings policy)

- **dx ≤ 10 m** — building footprints become closed no-flow cells; barriers are
  meaningful; run is street-scale.
- **Coarse / screening (e.g. 30 m)** — buildings stay coverage-only (no binary
  walls); barriers/curbs are sub-cell and mostly presentation. Runs are labeled
  *screening resolution*.

## Official data stack (systems of record)

Every layer is provenance-stamped with the official URL, the transport file, and
a SHA-256 digest (`ingestion.conditioning.NYC_SOURCES`):

| Layer | Source |
|---|---|
| 2017 1-ft topobathymetric LiDAR DTM/DSM (NAVD88) | https://gis.ny.gov/nys-dem |
| Building Footprints (`nqwf-w8eh`) | https://data.cityofnewyork.us/Housing-Development/Building-Footprints/nqwf-w8eh |
| Roadbed (planimetric, Dec 2025) | https://catalog.data.gov/dataset/nyc-planimetric-database-roadbed |
| Sidewalk / Pavement Edge / Median / Retaining Wall / Transportation Structure | NYC Planimetric Database |
| Hydrography / Shoreline | NYC Planimetric Database |
| Land Cover / Impervious (6-in) | NYC OpenData |
| Drainage inlets (where public) | NYC DEP |
| Map tiles (basemap, quarterly) | https://gis.nyc.gov/tiles/ |
| Orthoimagery (2024) | https://gis.ny.gov/new-york-city-orthoimagery-downloads |

**Egress note:** every official host above — and OpenStreetMap tiles — is blocked
from the current build sandbox (verified). This module is therefore the *engine*:
point each `FeatureLayer` at a downloaded file and it conditions the grid; the
tested proofs run on constructible official-schema data (EPSG:2263, feet). **No
Google Earth / Street View / Photorealistic 3D-Tiles geometry is used anywhere**
in the model or exports; Google 3D is a viewer presentation-only toggle,
disabled without a key + live attribution.

## Validation (the plan's checklist)

| Requirement | Where |
|---|---|
| Inspect street cross-sections | `validation.hydraulic.cross_section` |
| Buildings & retaining walls block flow | `test_conditioned_physics::test_crest_blocks_flow_until_overtopped`, `test_conditioning::test_conditioned_grid_runs_and_routes` |
| Roads channel flow | `test_conditioned_physics::test_road_channels_flow_faster_than_rough_ground` |
| Bridges/culverts don't false-dam | `test_conditioned_physics::test_culvert_prevents_false_dam` |
| Compare 1–2 m vs 8 m; mass balance + arrival time | `validation.hydraulic.compare_resolutions`, `test_hydraulic_validation` |

## Usage

```python
from aqua_sim.ingestion.dem import DEMSource
from aqua_sim.ingestion.buildings import BuildingsSource, apply_buildings
from aqua_sim.ingestion.conditioning import (
    FeatureLayer, burn_surface_classes, burn_barriers,
    add_drainage_inlets, add_culverts)

grid = DEMSource("nyc_topobathy_dtm.tif", target_dx_m=2.0, aoi_bounds=AOI).load()
apply_buildings(grid, BuildingsSource("building_footprints.gpkg").load_for_grid(grid))
burn_surface_classes(grid, {
    "landcover": FeatureLayer("landcover.gpkg", "landcover"),
    "road":      FeatureLayer("roadbed.gpkg", "roadbed"),
    "sidewalk":  FeatureLayer("sidewalk.gpkg", "sidewalk")})
burn_barriers(grid, [
    FeatureLayer("median.gpkg", "median"),
    FeatureLayer("retaining_wall.gpkg", "retaining_wall", height_attr="HEIGHT")])
add_drainage_inlets(grid, FeatureLayer("catch_basins.gpkg", "drainage_inlet"))
add_culverts(grid, [(-73.99, 40.72, -73.989, 40.721, 3.0)])
# grid is now a conditioned hydraulic surface — run it through the solver.
```

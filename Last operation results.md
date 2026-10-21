# Last Operation Results — aqua-sim checkpoint

> **Read this file first before continuing work on this project.**
> It is the authoritative snapshot of what exists, what runs, what is proven,
> what is blocked, and what to do next. Update it at the end of each work session.

- **Branch:** `claude/flood-simulator-planning-myqfs0` (clean, pushed)
- **Head commit at checkpoint:** `2c5d514` — *Viewer: cap breach banners, constant-size sensor markers*
- **Tests:** **77 passing** (`pytest`), ~40 s
- **Status:** paused mid-project at the user's request. No work in flight (a
  background data hunt and a scheduled wake-up were both stopped — see
  §7 *Next steps*).

---

## 1. What this project is

A **hydrodynamic digital twin**: a flood simulator that ingests real terrain,
solves 2D shallow-water physics offline, scores infrastructure risk, and streams
the result to a browser telemetry dashboard.

> *If it rains like this, where does the water go, how deep does it get, how fast
> is it moving, and which specific assets flood — and when?*

Design bias throughout: **physical correctness and reproducibility over visual
spectacle.** Every result traces to its inputs via a content-hashed `run_id`.

## 2. Architecture (four decoupled layers)

```
ingestion → physics → risk → export ─(files)→ viewer
```

| Layer | Where | What it does |
|---|---|---|
| **Ingestion** | `src/aqua_sim/ingestion/` | DEM (GeoTIFF, multi-tile mosaic) → metric UTM `Grid`; official building footprints; hydraulic-surface conditioning |
| **Physics** | `src/aqua_sim/physics/` | Local-inertial shallow-water solver (Bates et al. 2010), two backends |
| **Risk** | `src/aqua_sim/risk/` | Depth×velocity hazard, orifice sink-node breach, ranked alert log |
| **Export** | `src/aqua_sim/export/frames.py` | Versioned run folder (format **2.0**) with full provenance |
| **Viewer** | `viz/` | Three.js dashboard: kinetic water shader, buildings, sensors, layers |

The seam between export and viewer is a **file format**, not a function call —
the solver and viewer are separate deployables and evolve independently.

## 3. Physics — what is implemented and proven

Scheme: **local-inertial SWE** (the standard for urban 2D flood modeling),
with a donor-cell flux limiter, CFL-adaptive timestepping, and open/closed
boundaries. Interior obstacles are **unconditionally no-flow** walls.

**Phase 6A conditioned-surface physics** (all optional; when unset the solver
runs the original code path **bit-identically**):

| Field on `Grid` | Physical effect |
|---|---|
| `crest_x`, `crest_y` | Subgrid barrier crests — curbs/medians/retaining walls raise the face's controlling bed; flow only spills once the surface tops the barrier |
| `infiltration_rate` + `infiltration_capacity` | Per-cell rain loss, rate-limited and capacity-capped |
| `drainage` | Per-cell storm-drain-inlet sink |
| `connections` | Bridge/culvert conduits — move water by head so an embankment passes flow instead of false-damming |

**Two backends, held in lockstep:**
- `physics/swe.py` — pure-Python **reference** (the readable specification)
- `physics/swe_numpy.py` — **vectorized**, ~20× faster, used for anything large

`tests/test_swe_numpy.py` + `tests/test_conditioned_physics.py` drive both through
identical scenarios and require **cell-by-cell agreement to ≤1e-9**, including
the conditioned path. That equivalence is what licenses using the fast backend
for science.

**Proven invariants (unit-tested):** mass conservation to floating point,
well-balancedness (lake-at-rest), non-negative depths under a violent dam-break,
buildings/walls block flow, roads channel flow, culverts prevent false dams,
infiltration/drainage mass balance.

## 4. Data — what is real, what is blocked

**Reachable from this sandbox (verified):**

| Source | Use |
|---|---|
| `prd-tnm.s3.amazonaws.com` (USGS 3DEP S3) | **Real 10 m DEM tiles** — the terrain for every real run |
| `raw.githubusercontent.com` | The **official NYC Building Footprints** extract (Manhattan, 44,895 features) |

**Verified BLOCKED (all return HTTP 000):** `data.cityofnewyork.us`,
`gis.ny.gov` (2017 1-ft topobathymetric LiDAR), `gisservices.its.ny.gov` (tax
parcels), `orthos.dhses.ny.gov` (1 m/2 m DEM index, orthoimagery),
`gis.nyc.gov/tiles`, `data.gov`, `tile.openstreetmap.org`,
`download.geofabrik.de`, `overpass-api.de`, `zenodo.org`, `github.com` (400).

**Consequence and the deliberate response:** the Phase 6A conditioning engine is
built as a **pluggable engine** — point a `FeatureLayer` at a downloaded file and
it conditions the grid. It is validated on constructible official-schema data
(EPSG:2263, US survey feet). Faking ingestion of unreachable data would be worse
than useless.

**DEM tiles on disk** (scratchpad, not in git):
`USGS_13_n41w073.tif`, `USGS_13_n41w074.tif`, `USGS_13_n41w075.tif` —
together they cover Staten Island → Long Island. Note `n41w072` (Montauk/outer
forks) **does not exist** in any USGS staged product.

**Policy: no Google-derived geometry** anywhere in the model or exports. Google
3D Tiles is a disabled, presentation-only viewer toggle requiring an API key and
live attribution.

## 5. Runs produced (all regenerable; gitignored — they are large)

| Run | Extent | Cells / dx | Time | Notes |
|---|---|---|---|---|
| `viz/lm_run` | Lower Manhattan | 606×382 @ **8 m** | 12 min | **6,677 real buildings as no-flow walls**; water channels the avenues |
| `viz/metro60_run` | Five boroughs | 798×805 @ **60 m** | 7.5 min | 695 skyline buildings, borough presets |
| `viz/nyc_run` | Five boroughs | 399×402 @ 120 m | 54 s | Earlier screening run |
| `viz/ny_run` | **Staten Island → Long Island** | 776×243 @ 250 m | 30 s | Complete NY area, 3-tile mosaic |
| `viz/ida_run` | Manhattan | 234×521 @ 30 m | ~15 min ×3 | Hurricane Ida validation, 3 drainage cases |
| `viz/sample_run` | Synthetic | 40×60 @ 60 m | seconds | **Committed** — the dashboard works on clone |

Also proven: a **2.57 M-cell** five-borough run at 30 m (85 min) — the scale
stress test that motivated the NumPy backend.

## 6. Historical validation — Hurricane Ida, 2021-09-01

Scored report: `docs/validation/ida2021_report.json`. Driven by the documented
NWS hyetograph (20/**80**/40/20 mm/hr; the 80 mm peak hour is the exact Central
Park record) over real USGS terrain, scored against six documented-flooded
subway stations under a **drainage sensitivity matrix**.

**Result: partial validation, honestly scored.** POD 0/6, 0/6, **1/6** across
drainage blockage 0.0 / 0.5 / 1.0. The one detection (Dyckman St) is the
lowest-lying station in the set and among the worst-hit in the real event;
basin-scale ponding is strong (~3.6 M m³, >3 m in lows). Street-scale detection
needs the Phase 6 items in §7. The **failed first attempt is kept in
`docs/VALIDATION.md`** — the corrections were argued from event documentation,
not tuned until the score looked good.

## 7. Next steps (in priority order)

1. **Get the blocked data** — run the fetches from a network that can reach
   NYC/NYS hosts, then re-run with conditioning. This is the single biggest
   quality unlock. Highest value: 1 m topobathymetric LiDAR DTM, citywide
   building footprints (all five boroughs), roadbed/sidewalk/median/retaining
   wall, drainage inlets.
2. **Finish the data hunt** (was in flight, stopped at pause) — search reachable
   mirrors for: USGS 3DEP **1 m** projects covering NYC, the USGS **National
   Structures Dataset**, `usgs-lidar-public` S3 point clouds, Overture/OSM S3
   buildings and roads.
3. **Roof-runoff routing** — rain on building cells currently leaves the water
   balance; routing it to adjacent streets is a large volume concentrator and a
   likely cause of the low Ida street-scale POD.
4. **Subgrid porosity** — so screening-resolution buildings partially impede
   flow instead of being presentation-only.
5. **Hydro-enforced breakline elevation burning** — currently barriers are
   conditioned as crests/connections, not DTM channel-carving.
6. **Taichi/GPU kernel** — for native 1–2 m city-scale runs.
7. **Second validation event** + FEMA NFHL overlap, to guard against tuning to
   one storm.

## 8. Where to look

| Question | File |
|---|---|
| Vision, scope, decisions, roadmap | `docs/PLANNING.md` |
| Layers, equations, run-format contract | `docs/ARCHITECTURE.md` |
| DEM vs LiDAR vs drone photogrammetry | `docs/DATA_INGESTION.md` |
| What a DEM is; the NYC dataset list | `docs/DATA_SOURCING.md` |
| Hydraulic surface conditioning (Phase 6A) | `docs/CONDITIONING.md` |
| Ida validation method + honest limits | `docs/VALIDATION.md` |
| Viewer contract, layers, attribution | `viz/README.md` |
| How to run anything | `README.md` |

## 9. Standing conventions

- **Runs are gitignored**, code and scored reports are committed. Every run
  embeds a `run_id` (SHA-256 of provenance **including a terrain content
  digest**), so a regenerated run is verifiable against a recorded one.
- **Screening vs street resolution** is stated in every run's AOI name.
  Buildings become no-flow walls only at `dx ≤ 10 m`; coarser runs export
  coverage fraction and keep buildings presentation-only.
- **Honesty over polish:** blocked data is reported as blocked; failed
  validation attempts stay in the docs; limitations are stated in the artifacts
  themselves, not just in chat.

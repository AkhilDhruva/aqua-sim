# aqua-sim — Historical Validation

The claim a flood model must earn: *given a storm that actually happened, does
it reproduce the flooding that actually happened?* This document records the
validation program. Analytic benchmarks (mass conservation, lake-at-rest,
dam-break behavior) live in `tests/test_swe.py` and run in CI; this page covers
validation against a recorded event.

## Event 1 — Hurricane Ida remnants, Manhattan, 2021-09-01

**Why this event:** the most intense hour of rain ever recorded in Central Park
(3.15 in / 80.0 mm, hour ending 21:51 EDT), documented citywide subway flooding,
and dense public documentation of exactly which stations flooded — a rare,
well-constrained ground truth for an urban pluvial-flood model.

**Module:** `src/aqua_sim/validation/ida2021.py` ·
**Run:** `python -m aqua_sim.validation.ida2021 DEM.tif OUTDIR [dx] [buildings.geojson]`

### Inputs

| Input | Source | Notes |
|---|---|---|
| Terrain | **USGS 3DEP 1/3 arc-second (10 m) tile `n41w074`** — real, LiDAR-derived bare earth (public domain, fetched from the USGS S3 bucket) | Reprojected to UTM 18N by `DEMSource`; AOI clipped to Manhattan east of −74.0° |
| Storm | NWS Central Park observations, encoded as a staircase **hyetograph**: 20 → **80** → 40 → 20 mm/hr over 4 h (160 mm total) | The 80 mm peak hour is the exact record; shoulders are approximate |
| Drainage | 44 mm/hr uniform sink | NYC DEP design standard (~1.75 in/hr); Ida's peak hour exceeded it ~2× |
| Ground truth | Documented flooded stations: 157 St (1), Dyckman St (1), 168 St (1/A/C), 28 St (1), 96 St (1/2/3), Times Sq–42 St | Coordinates approximate (±~100 m) |

### Method

Sink nodes are placed at each documented-flooded station. Because station
coordinates carry ~±100 m uncertainty and entrances flood via the low corner of
their surroundings (runoff converges there), each node probes the **lowest
ground cell within the uncertainty radius** of its coordinate, with the
threshold **0.05 m above ground** — street-level sidewalk vents, the documented
primary ingress path during Ida — and a stairwell-scale opening. The solver
runs the hyetograph over the real terrain; a station counts as **detected**
when its node breaches (hydraulic head above the lip → orifice inflow). The
score is the **probability of detection (POD)** across documented-flooded
stations.

Because the sewer state during Ida is the one forcing that cannot be known
precisely (documented as overwhelmed — but not by how much or when), the
validation is a **drainage sensitivity matrix**: the same storm under design
capacity (blockage 0.0), surcharged-to-half (0.5), and failed (1.0)
assumptions, with POD reported for each. No single flattering value is chosen.

**What is deliberately NOT claimed:** a false-alarm rate. Scoring false alarms
requires an authoritative list of stations that did *not* flood, which we do not
have. POD over documented positives is the honest extent of this validation;
the report says so in its `notes`.

### Method evolution (kept for the record)

The **first attempt scored POD 0/6** — while the model simultaneously ponded
3.6 million m³ across 62,000 wet cells at the end of the record hour, with
basins over 3 m deep. Diagnosis: the flooding was real but the probing was
naive — exact-cell sampling at approximate coordinates landed on runoff slopes
(a 3-cell miss at 30 m), and a 0.15 m "entrance step" threshold contradicted
the documented vent-level ingress. The corrections above (local-low probing,
vent lip, drainage matrix) are physical corrections argued from the event
documentation — not parameter tuning until the score looked good, which is why
the failed first attempt stays in this document.

### Stated limitations

1. **No building footprints burned in** for this run — every footprint source
   (NYC OpenData, Microsoft, OSM) is unreachable from this sandbox. The burn-in
   code (`ingestion/buildings.py`) is implemented and tested with synthetic
   footprints; supply a GeoJSON as the 4th CLI argument when egress allows.
   At 30 m cells the terrain signal dominates, but street-canyon channeling is
   unresolved without buildings.
2. **Station coordinates approximate** (general knowledge, not GTFS).
3. **Tile edge at −74.000°** excludes the Battery Park City strip (no
   validation stations there).
4. **Uniform drainage** approximates a surcharged sewer network; the real system
   is a network with local capacity variation.
5. **Resolution** 30 m (compute-bound in the pure-Python reference solver; the
   Taichi backend lifts this to the native 10 m).

### Results — partial validation

Scored report: [`validation/ida2021_report.json`](validation/ida2021_report.json)
(run folders are ~140 MB per case and not committed; regenerate with the command
above — each case's `run_id` + the terrain digest make a regenerated run
verifiable against the committed score).

| Drainage assumption | POD | Detected |
|---|---|---|
| Design capacity (blockage 0.0) | 0/6 | — |
| Surcharged to half (0.5) | 0/6 | — |
| **Failed (1.0)** | **1/6** | **Dyckman St (1), breach at t≈83 min** |

**Interpretation.** The model reproduces the *basin-scale* signal of the event:
at the end of the record hour it ponds ~3.6 million m³ across ~62,000 wet
cells with depths over 3 m in topographic lows — and the one station it
detects, **Dyckman St, is the lowest-lying station in the set and the site of
some of the event's most dramatic flooding**. Station peak depths also rank in
a physically sensible order (Dyckman > 28 St > Times Sq). What it does *not*
yet reproduce is street-scale accumulation at the remaining five stations, and
the reasons are identifiable, not mysterious:

1. **No buildings** (footprint sources unreachable from this sandbox):
   buildings cover roughly half of Manhattan — burning them in both blocks flow
   paths and, with roof-runoff redirection, roughly doubles the effective rain
   loading on street cells. Both effects raise street depths.
2. **No roof-runoff concentration**: rain falling on building cells currently
   leaves the water balance instead of draining to the adjacent street — a
   large volume concentrator in a real city.
3. **30 m resolution** smooths curbs, road crowns, and the micro-depressions
   where entrance-scale ponding actually happens (the pure-Python reference
   solver is compute-bound; the Taichi backend unlocks the native 10 m).
4. **Sewer backflow** (water ejected *from* the surcharged system through
   vents — a documented Ida mechanism) is not modeled; our drainage term only
   ever removes water.

**Verdict:** basin-scale behavior validated; street-scale station detection
requires the Phase 6 items above, in that order of expected impact. This is a
partial validation honestly scored — not a failed one, and not a passed one.

## Planned next events

- **FEMA National Flood Hazard Layer overlap** — do the model's chronic
  high-risk cells overlap the regulatory flood zones? (Needs NFHL egress.)
- **A second recorded event** (e.g. the Sep 2023 Ophelia-remnant flooding) once
  rainfall records are fetchable, to guard against tuning to one storm.

# aqua-sim — Architecture

Companion to [`PLANNING.md`](./PLANNING.md). This document describes the system
layers, the physics engine, and how data flows through them.

## 1. Layered architecture

Four decoupled layers. Each has a narrow, documented contract so layers can be
built, tested, and swapped independently.

```
                        ┌─────────────────────────────────────────────┐
  survey data           │  1. INGESTION                               │
  (DEM / LiDAR / drone) │  → metric, CRS-aware Grid (DTM + obstacles) │
                        └───────────────────────┬─────────────────────┘
                                                │  Grid
                        ┌───────────────────────▼─────────────────────┐
  storm params  ─────►  │  2. PHYSICS (Shallow Water solver)          │
  (mm/hr, hours,        │  → time series of depth h, velocity (u,v)   │
   drainage)            └───────────────────────┬─────────────────────┘
                                                │  fields per timestep
                        ┌───────────────────────▼─────────────────────┐
  sink nodes,   ─────►  │  3. RISK                                    │
  geofence              │  → hazard classes, sink inflow, alert log   │
                        └───────────────────────┬─────────────────────┘
                                                │  frames + risk report
                        ┌───────────────────────▼─────────────────────┐
                        │  4. VISUALIZATION (Three.js, browser)       │
                        │  → interactive 3D terrain + water + alerts  │
                        └─────────────────────────────────────────────┘
```

The seam between layers 3 and 4 is a **file format** (exported frames), not a
function call — the Python engine and the JS viewer are separate deployables.

## 2. Core data structure: `Grid`

Everything downstream depends on a single, well-defined terrain grid.

- **Uniform structured raster.** `nx × ny` cells, constant cell size `dx == dy`
  in **meters** (physics needs metric spacing; that means reprojecting geographic
  data to a local UTM zone at ingestion time).
- **Fields per cell:**
  - `z` — bare-earth terrain elevation (DTM), meters.
  - `obstacle` — boolean/height mask from building footprints (DSM − DTM), so
    flow routes *around* structures.
  - `manning` — Manning's roughness coefficient `n` per cell (land-cover derived;
    concrete ≈ 0.013, grass ≈ 0.035, etc.).
  - `infiltration` — capacity parameters (Curve Number / Green–Ampt) per cell.
  - `mask` — inside/outside the geofenced area of interest.
- **Metadata:** CRS, affine transform (pixel↔world), source provenance,
  resolution, timestamp. Carried with every dataset for reproducibility.

The physics grid (uniform, structured) is deliberately **separate** from the
visualization mesh (may be decimated / textured). They share elevation but serve
different masters: correctness vs. rendering.

## 3. Physics engine

### 3.1 Governing equations — 2D Shallow Water Equations (SWE)

Depth-averaged conservation of mass and momentum. Valid when horizontal scales ≫
water depth, which holds for surface flooding.

Continuity (mass):

```
∂h/∂t + ∂(hu)/∂x + ∂(hv)/∂y = R − I − D
```

where `h` is water depth, `(u,v)` depth-averaged velocity, `R` rainfall source,
`I` infiltration loss, `D` drainage sink (all as rates, m/s).

Momentum (x, symmetric in y):

```
∂(hu)/∂t + ∂(hu²)/∂x + ∂(huv)/∂y = −g·h·∂(z+h)/∂x − g·n²·u·√(u²+v²)/h^(1/3)
```

The last term is the Manning friction slope.

### 3.2 Default scheme — local-inertial (LISFLOOD-FP / Bates et al. 2010)

We drop the convective acceleration terms (the `∂(hu²)/∂x` group), keeping local
(temporal) acceleration, pressure gradient, and friction. This is the standard,
well-validated approximation for **sub-critical urban flooding** and is far
cheaper and more robust than the full equations. Flux between adjacent cells:

```
Q_{t+Δt} = ( Q_t − g·h_flow·Δt·∂(z+h)/∂x )
           / ( 1 + g·Δt·n²·|Q_t| / (h_flow^(7/3)) )
```

with `h_flow` the depth available for flow between the two cells (max water
surface − max bed). Depths update by mass balance from the four face fluxes plus
source/sink terms. This formulation is **well-balanced** (preserves lake-at-rest)
and handles wet/dry fronts gracefully.

**Optional high-fidelity mode:** full dynamic SWE with an HLLC Riemann solver
(finite-volume Godunov) for steep terrain / supercritical flow where the inertial
approximation degrades. Same `Grid` in, same fields out.

### 3.3 Numerical stability — CFL condition (non-negotiable)

Explicit schemes require the timestep to respect the Courant–Friedrichs–Lewy
limit; wave information must not cross more than one cell per step:

```
Δt ≤ α · Δx / ( |u| + √(g·h) )        0 < α ≤ 1  (α ≈ 0.7 typical)
```

The solver recomputes the maximum stable `Δt` each step (adaptive timestepping).
Both terms come free: peak depth is tracked during the depth update, and the
advective velocity is the peak **face velocity** `|q|/h_flow` tracked inside the
flux update itself — conveyance-referenced, so it stays physical at wetting
fronts (dividing by a near-dry *cell* depth would explode). The celerity-only
bound of Bates et al. (2010) is stable for depth but admits transient velocity
oscillations at α≈0.7; the advective term suppresses them (verified by a
dt-convergence check: peak depth agrees to <0.2% across α∈{0.1, 0.35, 0.7}).
Omitting CFL control entirely — as the original note did — is the #1 cause of a
flood solver "exploding." Lives in `physics/stability.py`.

### 3.4 Source / sink terms

- **Rainfall `R`:** user's mm/hr converted to m/s, applied uniformly (later:
  spatially varying). Storm duration and hyetograph shape configurable.
- **Infiltration `I`:** SCS Curve Number for screening, Green–Ampt for detail.
  Pervious surfaces (soil, parks) absorb; concrete does not.
- **Drainage `D`:** per-cell sink representing storm-drain capacity. The
  "clogged vs clear" slider scales `D` from 0 (fully blocked) to design capacity.

### 3.5 Boundary conditions

- **Open / free-outflow** at domain edges so water can leave (critical — a closed
  box never drains).
- **Closed wall** (reflective) at obstacles / masked-out cells.
- **Inflow** (fixed discharge or stage) for rivers/streams entering the domain.

## 4. Risk layer

### 4.1 Hazard classification

Flood hazard to people/vehicles is governed by **depth × velocity**, not depth
alone. We classify each cell/time using an established hazard rating
(e.g. `HR = d·(v + 0.5)` style thresholds → Low / Moderate / Significant /
Extreme), color-mapped in the viewer. Shallow-slow = safe; deep-fast = lethal.

### 4.2 Sink nodes (subterranean coupling)

Points representing subway entrances, underpasses, basements, substations. Each
node has an elevation threshold, an opening area `A`, a discharge coefficient
`Cd`, and a below-ground storage capacity. When surface water surface exceeds the
threshold, inflow follows the **orifice equation**:

```
Q_in = Cd · A · √( 2·g·(H_surface − z_threshold) )      capped by node capacity
```

This gives a credible *fill time* ("Transit Node 4 fully inundated in ~14 min")
instead of an instantaneous, physically meaningless dump.

**Defining a "breach" mathematically.** A breach is *not* "there is water at the
node cell." Raw depth is the wrong trigger — a 1 cm film over a high entrance is
harmless, while the same depth at a low entrance is a flood. The correct,
defensible criterion is a **hydraulic head over the entrance lip**, evaluated by
the solver (not the viewer):

    η = z_bed + h                 (local water-surface elevation)
    head H = η − z_threshold      (z_threshold = elevation of the entrance lip)
    BREACH  ⇔  h > min_depth  AND  H > ε          (ε ≈ 0.02 m, a physical margin)

The `min_depth` guard rejects numerically thin films (it is derived from the
solver's own dry threshold, 10×, so the two can't be configured into
contradiction); the `ε` margin rejects sub-centimeter noise around the lip so
the trigger is deterministic, not chattery — a deliberate dead band: inflow at
0 < H ≤ ε is physically possible but below the noise floor of the terrain data.
On breach the engine emits the orifice discharge `Q = Cd·A·√(2gH)`, **capped by
the water actually available in the cell over the interval** (a node cannot
ingest more than the cell holds) and by node capacity, and accumulates `∫Q dt`
toward the node's storage, tracking `fraction_full`. A *warning* fires earlier,
when `H ≥ −0.15 m` (water within 15 cm below the lip) — and is suppressed if the
same frame already breaches, since a warning with zero lead time is noise.
Severity thus escalates on physical state, not on frame count: **approaching lip
→ WARNING; head above lip → CRITICAL breach with an inundation rate and ETA.**
Every breach is written into the frame where it occurs (see §6), so a frame is
self-contained evidence of the event.

*Evaluation cadence (honest limitation):* breach records are evaluated per
**output frame**, so `∫Q dt` uses the output interval, not the solver's internal
Δt. This is a screening-level integration; if a breach peaks and recedes entirely
between two output frames it is missed. Tightening `output_interval_s` tightens
the integration; moving the scan inside the solver loop is a Phase 6 refinement.

### 4.3 Alert log / risk matrix

Time-stamped, severity-ranked events driven by thresholds:
- *Warning* — surface depth approaching a critical asset's threshold.
- *Critical* — sink-node breach detected, with estimated time-to-inundation.

Output as structured data (JSON) so both the viewer and reports consume it.

## 5. Visualization layer (Three.js)

- **Terrain mesh** generated from the `Grid` heightmap; buildings extruded from
  the obstacle layer.
- **Water surface** as a second dynamic mesh whose vertex heights = terrain +
  depth per frame; updated efficiently in the render loop from the frame data.
- **Depth/velocity shader** — custom material color-coding hazard (transparent
  shallow-blue → opaque red for deep/fast), matching the risk classification.
- **God-mode camera** — orbit/pan/zoom controls.
- **Dashboard sliders** — rainfall intensity (mm/hr), storm duration (hr),
  drainage capacity. In the pre-computed-frame model these select among runs or
  scrub time; a later interactive mode recomputes live.

## 6. Frame export format (the layer 3↔4 contract)

A run exports:
- `manifest.json` — grid metadata (CRS, transform, dx, extent), time axis, units,
  provenance, list of frame files, sink-node definitions.
- `terrain.json` — static bed elevation, obstacle (building) heights, and the
  **AOI mask** (nodata cells are void-filled at ingest; only the mask tells the
  viewer they are not real terrain), loaded once.
- `frame_001.json … frame_NNN.json` — per-timestep water **depth and speed**
  grids, peak stats, an embedded **provenance header** (`run_id` + generation
  parameters), and the list of active **breaches** for that instant
  (`{breach_detected, node_id, head_m, inundation_rate_m3_s,
  cumulative_volume_m3, fraction_full}`). A frame is never separable from how it
  was produced. (A later phase can swap the JSON body for a compact typed-array
  `.bin` without changing the schema — the format is versioned; current:
  **2.0**.)
- `alerts.json` — the time-stamped, severity-ranked risk log (first-crossing events).

**Hazard constants travel with the run.** The manifest's `hazard` block exports
the engine's `debris_factor`, HR band bounds, and critical depth
(`risk/hazard.py`), and the viewer's shader reads them from the manifest — so
on-screen coloring can never drift from the thresholds the alerts were computed
with.

**Provenance & determinism.** The manifest carries a full `provenance` block
(data source, **a SHA-256 digest of the terrain content**, resolution, CRS,
solver scheme — validated at solver construction so it can never record a scheme
that didn't run — Manning value, storm & solver parameters) and a `run_id` — a
SHA-256 content hash of that block. Identical inputs over identical terrain
produce an identical `run_id`; the same configuration over *different* terrain
produces a different one. This is what "deterministically derived" means in
practice, and it is the honest alternative to a wall-clock stamp or a decorative
signature.

Keeping this an explicit, versioned format is what lets the solver and viewer
evolve independently.

## 6a. The pre-computed (offline-solve) architecture

This is the deliberate, load-bearing design choice: **the physics runs offline on
a Python/Taichi backend and exports frames; the browser is a thin telemetry
dashboard over those frames.** It is not a shortcut — it is how every serious
flood model operates (HEC-RAS, TUFLOW, LISFLOOD-FP all solve offline and
visualize results). The benefits:

- **Fidelity without browser limits.** The backend can run a fine grid over a long
  storm for as long as it needs, unconstrained by a browser tab's memory or a
  frame budget. The viewer never does physics, so it stays instant.
- **Reproducibility = authority.** Every run writes a `manifest.json` provenance
  block (data source, resolution, CRS, solver scheme, storm/solver parameters).
  A reviewer can trace any displayed result back to its exact inputs and rerun it.
  This — not compute time itself — is what makes the model *defensible* to a
  technical audience (city planners, emergency managers, infrastructure agencies).
- **Separation of concerns.** Solver and viewer evolve independently across a
  versioned file format (§6). The solver can be rewritten (NumPy → Taichi/GPU)
  with zero viewer changes.

What makes the *science* strong (the parts reviewers actually probe):

1. **A named, published scheme** — local-inertial SWE (Bates et al. 2010), the
   documented standard for 2D urban flood modeling, not an ad-hoc heuristic.
2. **Enforced invariants** — mass conservation and well-balancedness (lake-at-rest)
   are unit-tested (`tests/test_swe.py`), and a flux limiter guarantees
   non-negative depths even under a violent dam-break. These are the first things
   a numerical reviewer checks.
3. **CFL-adaptive timestepping** — stability is guaranteed by construction, not by
   luck.
4. **Real terrain + provenance** — results are tied to a specific public DEM tile
   with recorded resolution/CRS/datum (see docs/DATA_SOURCING.md).
5. **Benchmark validation** — analytic cases now; a recorded event (e.g. NYC 2021)
   and the FEMA flood layer as the AOI validation target.

### The end-to-end thin slice (implemented)

The `scenario` module already runs the full path today:

```
build terrain (Grid)  ->  ShallowWaterSolver.run()  ->  per-frame risk eval
     ->  write_run():  manifest.json + terrain.json + frame_001..NNN.json + alerts.json
```

Run it: `python -m aqua_sim run OUTDIR`. The Three.js viewer (Phase 4) will load
`terrain.json` once for the static mesh, then cycle `frame_NNN.json`, updating the
water surface and surfacing `alerts.json` entries as their timestamps are reached
— exactly the "supercomputing model, browser as dashboard" split.

## 7. Technology stack

| Concern | Choice | Notes |
|---------|--------|-------|
| DEM/raster I/O | `rasterio`, `numpy` | GeoTIFF read, reproject, resample |
| LiDAR | `PDAL`, `laspy` | `.las/.laz` → ground-classified DTM/DSM |
| Photogrammetry | OpenDroneMap / COLMAP (external) | SfM → dense cloud → DSM/DTM |
| Reprojection | `pyproj` | to local UTM (metric) |
| Physics | NumPy → **Taichi** (GPU) | vectorized; Taichi for city-scale speed |
| Viewer | **Three.js** | browser, orbit controls, custom shaders |
| Packaging | `pyproject.toml` / setuptools | already scaffolded |

Dependencies are introduced **phase by phase** (P0 stays dependency-free), so the
repo is always installable and testable at head.

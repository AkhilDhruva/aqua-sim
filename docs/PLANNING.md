# aqua-sim — Flood Zone Risk Simulator: Master Plan

**Status:** Planning · **Owner:** Akhil Dhruva · **Stage:** Pre-implementation architecture

This document is the single source of truth for *what we are building and why*.
It supersedes and hardens the original "Project Deluge" handoff note. Companion
docs go deeper on specific layers:

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — system layers, physics engine, data flow.
- [`DATA_INGESTION.md`](./DATA_INGESTION.md) — DEM / LiDAR / drone-photogrammetry input formats.

---

## 1. Vision

A hydrodynamic **digital twin** of a chosen geographic area that answers one
question with defensible numbers:

> *If it rains like **this**, where does the water go, how deep does it get, how
> fast is it moving, and which specific assets flood — and when?*

The long-term shape (Akhil's original 2018 idea): **swarm drones →
photogrammetry / LiDAR → stitched 3D terrain → geofenced area of interest →
flood-risk zoning.** The engine turns raw survey data into a physically-grounded
simulation and a ranked list of at-risk locations.

We are building a **decision tool**, not a video-game water effect. Every design
choice below biases toward *physical correctness and reproducibility* over visual
spectacle. Results should be validatable against textbook benchmarks and, later,
real recorded floods.

## 2. Scope

### In scope
- Ingest real terrain (DEM to start; LiDAR + drone photogrammetry as pluggable
  sources) into a metric, georeferenced elevation grid.
- Simulate 2D surface water flow from a rainfall event using the shallow-water
  family of equations, with friction, infiltration, and drainage.
- Identify and rank **risk zones** and **sink nodes** (subways, underpasses,
  basements) inside a user-defined geofence.
- Export simulation frames for an interactive 3D viewer.

### Out of scope (for now — noted so we don't scope-creep)
- Full 3D Navier–Stokes / free-surface CFD (unnecessary and intractable at city scale).
- Pipe-network / sewer hydraulic modeling (SWMM-class). We approximate drainage
  as a sink term; true coupled 1D–2D is a much later phase.
- Real-time streaming weather ingestion. We drive with user-set storm parameters.
- Structural / financial damage estimation. We output hazard, not dollar loss.

## 3. Key decisions (with rationale)

These are the choices that fork the architecture. Each is a recommendation, not
a lock — flag any you disagree with and we revise before building.

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| D1 | First input source | **Public DEM (GeoTIFF)** | Decouples solver development from the drone pipeline; USGS 3DEP / Copernicus give real 1–10 m terrain today. Drone & LiDAR plug in behind the same interface later. |
| D2 | Physics scheme | **Local-inertial SWE (LISFLOOD-FP / Bates 2010)** as default, full dynamic SWE (HLLC) as optional high-fidelity mode | Local-inertial is the industry standard for *urban* 2D flood modeling: stable, ~O(10×) cheaper than full SWE, well-validated. Full SWE reserved for steep/supercritical cases. |
| D3 | Where physics runs | **Python backend** (NumPy → Taichi/GPU), exports frames; browser only visualizes | Keeps the science validatable and version-controlled. A browser WebGL solver is a later *interactive* mode, not the source of truth. |
| D4 | Terrain surface | **DTM (bare earth) as the flow floor; buildings from DSM burned in as obstacles** | Water flows on the ground, around buildings. Using a raw DSM as the flow surface is a classic and serious modeling error. See `DATA_INGESTION.md`. |
| D5 | Physics grid | **Uniform structured raster grid**, separate from the visualization mesh | Structured grids make the SWE solver simple and fast. Adaptive/quadtree refinement is a later optimization. |
| D6 | Timestepping | **Adaptive Δt enforcing the CFL condition** | Explicit SWE solvers are *unconditionally unstable* without CFL-limited steps. This was missing from the original note and is non-negotiable. |
| D7 | Viewer | **Three.js**, reads pre-computed frames | Browser-accessible, no install for stakeholders. Decoupled from the solver via a documented frame format. |

## 4. Improvements over the original "Project Deluge" note

The original handoff captured the right *ambition*. These are the technical gaps
we are closing so the thing actually runs and produces trustworthy output:

1. **Numerical stability (CFL).** The original prescribes an explicit SWE update
   with no stability condition. Without an adaptive, CFL-limited timestep the
   simulation diverges. Added as a first-class concern (D6, `physics/stability.py`).
2. **DTM vs DSM.** "Ingest a DEM" hides the single most important data decision.
   Flow must run on bare-earth terrain with buildings as obstacles — not on a
   surface model that treats rooftops as ground (D4).
3. **Wet/dry fronts & well-balancedness.** Real terrain is mostly dry at t=0.
   Naive schemes produce spurious velocities and negative depths at the water's
   edge. We adopt a well-balanced scheme (hydrostatic reconstruction / the
   inertial formulation) that preserves "lake at rest."
4. **Losses beyond friction.** The note has Manning's roughness but no
   **infiltration**. We add an infiltration model (SCS Curve Number → Green–Ampt)
   and an explicit drainage sink, so the "clogged vs clear drains" slider maps to
   real physics.
5. **Boundary conditions.** No mention of what happens at the domain edge. We add
   open/outflow, closed-wall, and inflow boundaries — otherwise water either
   reflects unphysically or the domain never drains.
6. **Georeferencing & units.** Cell size must be in **meters**, CRS-aware
   (reproject to local UTM), so gravity, velocity, and rainfall (mm/hr → m/s)
   are physically meaningful. Added to the ingestion contract.
7. **Sink-node physics.** "Instantly calculate volume pouring in" becomes a
   proper **orifice/weir inflow** `Q = Cd·A·√(2gh)` capped by node capacity, so
   inundation timing is credible.
8. **Validation.** Added a benchmark suite (analytic dam-break / Ritter, EA test
   cases) so results are defensible to technical reviewers — city planners and
   emergency managers — on scientific merit.

> Note on framing: the original note pitched the UI toward impressing specific
> audiences. We optimize instead for **scientific defensibility** — correct,
> reproducible, benchmarked results. That is what earns trust with any technical
> stakeholder, and it is the only thing worth building.

## 5. Roadmap & milestones

Phases are ordered to get a **testable core early** and defer data-acquisition
risk. Each milestone has a concrete "done" check.

| Phase | Goal | Status / done when |
|-------|------|-----------|
| **P0 — Foundations** | Repo structure, config, dependency-free synthetic terrain, module contracts | ✅ **Done** — skeleton + docs + tests green |
| **P2 — Physics core** | Local-inertial SWE: rainfall source, Manning friction, CFL-adaptive Δt, open/closed boundaries, flux limiter | ✅ **Done** — mass-conserving (to fp), well-balanced, non-negative under dam-break; validated in `tests/test_swe.py` |
| **P3 — Risk layer** | Sink nodes (orifice inflow), depth×velocity hazard classes, alert log | ✅ **Done** — scenario emits time-stamped, ranked WARNING/CRITICAL alerts with inundation ETA |
| **P3.5 — Frame export** | Provenance-rich `manifest.json` + `frame_NNN.json` + `alerts.json` | ✅ **Done** — `python -m aqua_sim run OUTDIR` writes a full run folder |
| **P1 — Real ingestion** | GeoTIFF DEM (USGS 3DEP, Manhattan) → metric, CRS-aware `Grid`; geofence clip | ⏭ **Next** — real DEM tile loads into a `Grid` at correct metric cell size |
| **P4 — Visualization** | Three.js viewer over the run folder: orbit camera, depth/velocity shader, storm sliders | Stakeholder loads a run in-browser and scrubs time |
| **P5 — Drone/LiDAR ingestion** | Photogrammetry (SfM→DSM/DTM) and LiDAR (PDAL) as pluggable sources | Drone image set or `.laz` produces a `Grid` behind the same interface as P1 |
| **P6 — Hardening** | Taichi/GPU kernel, tiling for city scale, infiltration model, real-event validation (NYC 2021 / FEMA layer) | Runs a fine city-scale AOI in reasonable time; benchmark suite passes |

**Thin-slice status:** the end-to-end path (terrain → solver → risk → exported
frames → one sink-node alert) is **built and running today** on a Manhattan-scaled
synthetic AOI. The remaining thin-slice work is swapping the synthetic terrain for
a real Manhattan DEM (P1) and building the browser viewer (P4).

## 6. Risks & open questions

- **Data availability & accuracy.** Drone photogrammetry needs GCPs/RTK for
  absolute vertical accuracy; poor vertical accuracy ruins flood results. LiDAR
  is cleaner but harder to source. See `DATA_INGESTION.md`.
- **Compute scale.** Full-city, fine-resolution, long-duration storms are heavy.
  Mitigations: local-inertial scheme, tiling, GPU (Taichi), coarser grid for
  screening runs.
- **Validation data.** We need at least one recorded flood event to validate
  against beyond analytic cases. Open: which area of interest / event?
- **Drainage realism.** A sink-term drainage model is an approximation of a real
  sewer network. Good enough for screening; flag where it isn't.

**Resolved with Akhil:**
1. **AOI / geofence → Manhattan.** A scale where 2D urban flood modeling is the
   right tool. The demo scenario is Manhattan-shaped (island, central ridge,
   enclosed low-lying basin) pending the real DEM tile.
2. **No drone/LiDAR data yet → public DEM.** Use USGS 3DEP 1 m (LiDAR-derived) for
   Manhattan; drone/LiDAR ingestion deferred to P5. See docs/DATA_SOURCING.md.
3. **Pre-computed frame architecture confirmed.** Offline Python/Taichi solve →
   exported frames → thin Three.js dashboard. No interactive browser solver needed
   for the foreseeable milestones; credibility comes from provenance + validation
   (see ARCHITECTURE.md §6a).

## 7. How to contribute / next action

Next action is **P1**: implement the GeoTIFF → `Grid` loader against the
`TerrainSource` contract stubbed in `src/aqua_sim/ingestion/`. The synthetic
source (`ingestion/synthetic.py`) already implements that contract with zero
dependencies, so P2's solver can be developed and tested before any real data
arrives.

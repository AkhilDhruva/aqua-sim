# aqua-sim — Flood Zone Risk Simulator

A hydrodynamic **digital twin** that turns real terrain (public DEM, LiDAR, or
drone photogrammetry) into a physically-grounded 2D flood simulation, then ranks
the **risk zones** and subterranean **sink nodes** inside a geofenced area:

> *If it rains like this, where does the water go, how deep does it get, how fast
> is it moving, and which specific assets flood — and when?*

This is a **decision tool**, not a video-game water effect. Every design choice
biases toward physical correctness and reproducibility.

## Documentation (start here)

- **[docs/PLANNING.md](docs/PLANNING.md)** — vision, scope, key decisions, and the
  phased roadmap. The single source of truth.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — system layers, the
  shallow-water physics engine, the pre-computed (offline-solve) design, data flow.
- **[docs/DATA_INGESTION.md](docs/DATA_INGESTION.md)** — DEM / LiDAR /
  drone-photogrammetry input formats and how they converge to one terrain grid.
- **[docs/DATA_SOURCING.md](docs/DATA_SOURCING.md)** — what a DEM is, and the
  public Manhattan datasets (USGS 3DEP, NYC LiDAR) we use.

## Status: physics core running (P0–P3.5 done)

The **shallow-water solver is real and validated**, the risk layer emits alerts,
and a run exports the exact `frame_NNN.json` frames the browser viewer will read.
Real DEM ingestion (P1) and the Three.js viewer (P4) are next. Roadmap in
`docs/PLANNING.md`.

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

Run on a **real DEM** (adds rasterio/numpy/pyproj):

```bash
pip install -e ".[geo]"
python -m aqua_sim.ingestion.fetch manhattan_3dep.tif        # USGS 3DEP (needs egress)
python -m aqua_sim run output/mn --dem manhattan_3dep.tif    # reproject -> solve -> frames
```

A run writes `manifest.json` (with a provenance block), `terrain.json`,
`frame_001.json … frame_NNN.json`, and `alerts.json` into the output folder.

## Project layout

```
aqua-sim/
├── docs/                     # PLANNING, ARCHITECTURE, DATA_INGESTION
├── src/aqua_sim/
│   ├── config.py             # run configuration (storm, solver) + units
│   ├── grid.py               # the core metric terrain Grid (every layer's input)
│   ├── scenario.py           # end-to-end runner + Manhattan demo AOI
│   ├── geofence.py           # area-of-interest masking
│   ├── ingestion/            # 1. terrain sources → Grid
│   │   ├── base.py           #    TerrainSource contract
│   │   ├── synthetic.py      #    real, dependency-free test terrain (Phase 0)
│   │   ├── dem.py            #    GeoTIFF DEM -> UTM Grid  (real, tested)
│   │   ├── fetch.py         #    download USGS 3DEP tiles (real)
│   │   ├── lidar.py         #    LiDAR .las/.laz    (Phase 5)
│   │   └── photogrammetry.py #    drone SfM          (Phase 5)
│   ├── physics/              # 2. shallow-water solver
│   │   ├── swe.py            #    local-inertial SWE solver (real, tested)
│   │   ├── stability.py      #    CFL timestep       (real, tested)
│   │   ├── friction.py       #    Manning roughness  (real, tested)
│   │   ├── boundary.py       #    boundary conditions (open/closed/inflow)
│   │   └── infiltration.py   #    losses             (Phase 6)
│   ├── risk/                 # 3. hazard, sink nodes, alerts
│   │   ├── hazard.py         #    depth×velocity classes (real, tested)
│   │   ├── sink_nodes.py     #    orifice inflow         (real, tested)
│   │   └── alerts.py         #    time-stamped risk log  (real, tested)
│   └── export/frames.py      # 4. run export (manifest + frames)  (real, tested)
├── viz/                      # 4. Three.js browser viewer (Phase 4)
├── tests/                    # pytest suite
└── pyproject.toml
```

Dependencies are introduced **phase by phase** — the Phase 0 core is pure Python,
so the repo is always installable and testable at head.

## Remote workflow

```bash
git pull
git checkout -b feature/x
# ... work ...
git add -A && git commit -m "..."
git push -u origin feature/x
```

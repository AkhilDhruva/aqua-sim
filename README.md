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
  shallow-water physics engine, and data flow.
- **[docs/DATA_INGESTION.md](docs/DATA_INGESTION.md)** — DEM / LiDAR /
  drone-photogrammetry input formats and how they converge to one terrain grid.

## Status: Phase 0 (foundations)

The module skeleton and a **dependency-free, tested seed** are in place. The
shallow-water solver (Phase 2) and real data ingestion (Phase 1/5) are stubbed
with documented contracts. See the roadmap in `docs/PLANNING.md`.

What runs today (`python -m aqua_sim`): synthetic terrain generation, a
CFL-limited timestep, depth×velocity hazard classification, and orifice-based
sink-node inflow — the real, unit-tested building blocks the solver will use.

## Quick start (Python 3.10+)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m aqua_sim                 # Phase 0 demo
pytest                             # test suite
```

## Project layout

```
aqua-sim/
├── docs/                     # PLANNING, ARCHITECTURE, DATA_INGESTION
├── src/aqua_sim/
│   ├── config.py             # run configuration (storm, solver) + units
│   ├── grid.py               # the core metric terrain Grid (every layer's input)
│   ├── geofence.py           # area-of-interest masking
│   ├── ingestion/            # 1. terrain sources → Grid
│   │   ├── base.py           #    TerrainSource contract
│   │   ├── synthetic.py      #    real, dependency-free test terrain (Phase 0)
│   │   ├── dem.py            #    GeoTIFF DEM        (Phase 1)
│   │   ├── lidar.py         #    LiDAR .las/.laz    (Phase 5)
│   │   └── photogrammetry.py #    drone SfM          (Phase 5)
│   ├── physics/              # 2. shallow-water solver
│   │   ├── stability.py      #    CFL timestep       (real, tested)
│   │   ├── friction.py       #    Manning roughness  (real, tested)
│   │   ├── boundary.py       #    boundary conditions
│   │   ├── infiltration.py   #    losses             (Phase 6)
│   │   └── swe.py            #    solver             (Phase 2)
│   ├── risk/                 # 3. hazard, sink nodes, alerts
│   │   ├── hazard.py         #    depth×velocity classes (real, tested)
│   │   ├── sink_nodes.py     #    orifice inflow         (real, tested)
│   │   └── alerts.py         #    time-stamped risk log  (real, tested)
│   └── export/frames.py      # 4. run export for the viewer (Phase 4)
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

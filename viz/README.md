# aqua-sim viewer — Flood Telemetry Dashboard (Phase 4)

The browser-based **Three.js** front-end. It is a *telemetry dashboard*, not a
simulator: all physics runs offline in the Python engine, which exports a run
folder (`manifest.json`, `terrain.json`, `frame_NNN.json`, `alerts.json`). The
viewer loads the static terrain once, then cycles the frames.

## Run it

```bash
# 1. Generate a run (from the repo root):
python -m aqua_sim run viz/sample_run          # synthetic Manhattan demo
#    …or on a real DEM:
python -m aqua_sim run viz/my_run --dem manhattan_3dep.tif

# 2. Serve the repo over HTTP (ES modules require it):
python -m http.server 8000

# 3. Open the dashboard:
#    http://localhost:8000/viz/            (loads ./sample_run by default)
#    http://localhost:8000/viz/?run=my_run (or type a folder in the header)
```

## What's in it

- **God-mode camera** — orbit / pan / zoom (OrbitControls).
- **Kinetic water shader** (`app.js`) — a custom GLSL fragment shader that colors
  each water cell by *danger*, computed from **both depth and velocity**:
  `danger = max(depth/D_crit, hazard/HR_crit)` with `hazard = d·(v+0.5)`,
  `D_crit = 0.5 m`, `HR_crit = 1.25`. Shallow/slow water renders translucent
  cyan; deep or fast water escalates to opaque crimson. Opacity itself rises
  with depth.
- **Palette switcher** (`palettes.js`) — four ramps (Crimson Surge *default*,
  Ice-to-Blood, Inferno, Storm Teal→Red) selectable live; all share the same
  physical thresholds, only the hue mapping changes. Add a ramp by appending to
  `PALETTES`.
- **Alert matrix** — the run's `alerts.json` listed chronologically; entries
  light up as the timeline passes them; click one to jump to its frame.
- **Breach banners** — when the scrubber hits a frame whose embedded
  `breaches` list is non-empty, a flashing CRITICAL overlay reports the node,
  inundation rate (m³/s), head, and %-full — values computed by the solver,
  not the viewer.
- **Provenance strip** — `run_id`, terrain source, grid, CRS, storm and scheme
  from the manifest, always visible in the header.

## Buildings layer

Runs that ship a `buildings.json` (see
`ingestion/buildings.export_buildings_json`) get a **3D buildings layer**:
official-dataset footprints extruded to true height, spatially tiled, one
batched draw per tile with **two LOD levels** (true polygons near, box prisms
far) and per-tile frustum culling. Coordinates arrive **scene-local** — the
grid's UTM origin is subtracted server-side, so float32 never sees full
eastings. **Click a building** for height, ground elevation, peak adjacent
flood depth, first critical-depth crossing time, and max hazard class
(computed client-side from the loaded frames + the manifest's hazard block).
Layer checkboxes toggle Terrain / Buildings / Water / Sensors.

Geometry policy: buildings come only from official public datasets (NYC Open
Data Building Footprints; provenance + SHA-256 in `buildings.json`). The
"Photorealistic basemap" toggle is a disabled placeholder: streaming Google
3D Tiles would require an API key and live attribution and may never feed the
solver or exported assets — no Google-derived geometry exists in this repo.

## Vendored dependencies

`vendor/` contains Three.js (`three.module.min.js`, MIT — see `THREE_LICENSE`)
and `OrbitControls.js`, so the dashboard has **zero runtime network
dependencies** — it works fully offline once the repo is cloned.

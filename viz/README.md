# aqua-sim viewer (Phase 4)

The browser-based **Three.js** visualization front-end. Placeholder — the viewer
is built in Phase 4 (see [`../docs/PLANNING.md`](../docs/PLANNING.md)).

It is deliberately a **separate deployable** from the Python engine. The seam
between them is a file format, not a function call: the engine exports a run
(`manifest.json`, per-timestep depth/velocity frames, `alerts.json`) and the
viewer reads it. See [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) §5–§6.

Planned features:
- God-mode orbit/pan/zoom camera over the 3D terrain.
- Terrain mesh from the grid heightmap; buildings extruded from the obstacle layer.
- Dynamic water surface updated per frame.
- Depth/velocity hazard shader (transparent shallow-blue → opaque deep-fast red),
  matching `risk.hazard` classes.
- Storm dashboard: rainfall intensity, duration, drainage capacity.

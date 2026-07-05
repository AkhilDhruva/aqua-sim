"""Frame export — the contract between the Python engine and the Three.js viewer.

A completed run is written as a self-describing folder:

    run_dir/
      manifest.json     grid geometry, CRS, time axis, units, PROVENANCE, node list
      terrain.json      static bed elevation + obstacle heights (loaded once)
      frame_001.json    per-timestep water depth (+ peak stats)  ...
      frame_100.json
      alerts.json       time-stamped, severity-ranked risk log

The manifest's ``provenance`` block is what gives the model its authority: it
records the data source, resolution, CRS, solver scheme, and configuration so any
reviewer can trace a result back to its inputs. See docs/ARCHITECTURE.md §6.

JSON is used for the thin slice (human-readable, zero-dependency). The format is
versioned (``format_version``) so a later phase can swap frame bodies for compact
binary / typed-array buffers without breaking the viewer.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable

from aqua_sim.config import SimConfig
from aqua_sim.grid import Grid
from aqua_sim.physics.swe import FlowState

FORMAT_VERSION = "1.1"


def _round_grid(matrix, ndigits: int = 4):
    return [[round(v, ndigits) for v in row] for row in matrix]


def _representative_manning(grid: Grid) -> float:
    """The most common Manning's n on the grid — the headline roughness value."""
    counts: dict[float, int] = {}
    for row in grid.manning:
        for n in row:
            counts[round(n, 4)] = counts.get(round(n, 4), 0) + 1
    return max(counts, key=counts.get) if counts else 0.0


def _run_id(provenance: dict) -> str:
    """Deterministic content hash of the generation parameters.

    Same inputs -> same id. This is the honest form of a "provenance header": it
    proves a frame set was deterministically derived from a specific configuration
    (anyone can recompute it and get the same id), without a misleading wall-clock
    or fake cryptographic signature.
    """
    canonical = json.dumps(provenance, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def write_run(
    run_dir: str,
    grid: Grid,
    frames: Iterable[FlowState],
    config: SimConfig,
    alerts: list[dict] | None = None,
    frame_breaches: list[list[dict]] | None = None,
    scheme: str = "local_inertial",
) -> dict:
    """Serialize a completed run to ``run_dir``. Returns the manifest dict.

    ``frames`` may be a generator; it is consumed once and written incrementally
    as ``frame_001.json`` .. ``frame_NNN.json``. ``frame_breaches[i]`` (if given)
    is the list of active-breach objects embedded into frame ``i+1``.
    """
    os.makedirs(run_dir, exist_ok=True)

    # Static terrain (loaded once by the viewer).
    with open(os.path.join(run_dir, "terrain.json"), "w") as f:
        json.dump(
            {"nx": grid.nx, "ny": grid.ny, "dx": grid.dx,
             "z": _round_grid(grid.z, 3), "obstacle": _round_grid(grid.obstacle, 2)},
            f,
        )

    # Provenance is computed up front so every frame can carry the run id.
    provenance = {
        "terrain_source": grid.meta.get("source"),
        "resolution_m": grid.meta.get("resolution_m", grid.dx),
        "crs": grid.crs,
        "solver_scheme": scheme,
        "manning_representative": _representative_manning(grid),
        "storm": {
            "rainfall_mm_per_hr": config.storm.rainfall_mm_per_hr,
            "duration_hours": config.storm.duration_hours,
            "drainage_capacity_mm_per_hr": config.storm.drainage_capacity_mm_per_hr,
            "drainage_blockage": config.storm.drainage_blockage,
        },
        "solver": {
            "cfl": config.solver.cfl,
            "total_time_s": config.solver.total_time_s,
            "output_interval_s": config.solver.output_interval_s,
        },
        "terrain_meta": grid.meta,
    }
    run_id = _run_id(provenance)
    # Compact per-frame provenance header — a frame is never separable from how it
    # was made. Full detail lives in manifest.provenance under the same run_id.
    frame_header = {
        "run_id": run_id,
        "aoi": config.aoi_name,
        "grid_resolution_m": grid.dx,
        "rainfall_mm_per_hr": config.storm.rainfall_mm_per_hr,
        "manning_representative": provenance["manning_representative"],
        "solver_scheme": scheme,
    }

    frame_records = []
    times = []
    peak_depth = 0.0
    breach_list = frame_breaches or []
    for i, state in enumerate(frames, start=1):
        name = f"frame_{i:03d}.json"
        breaches = breach_list[i - 1] if i - 1 < len(breach_list) else []
        with open(os.path.join(run_dir, name), "w") as f:
            json.dump(
                {"index": i, "time_s": round(state.time_s, 3),
                 "provenance": frame_header,
                 "depth": _round_grid(state.depth, 4),
                 "speed": _round_grid(state.speed, 3) if state.speed else [],
                 "max_depth": round(state.max_depth, 4),
                 "max_speed": round(state.max_speed, 4),
                 "total_volume_m3": round(state.total_volume_m3, 3),
                 "breaches": breaches},
                f,
            )
        frame_records.append({"index": i, "file": name, "time_s": round(state.time_s, 3),
                              "max_depth": round(state.max_depth, 4),
                              "breach_count": len(breaches)})
        times.append(round(state.time_s, 3))
        peak_depth = max(peak_depth, state.max_depth)

    manifest = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "aoi": config.aoi_name,
        "grid": {"nx": grid.nx, "ny": grid.ny, "dx_m": grid.dx, "crs": grid.crs},
        "units": {"elevation": "m", "depth": "m", "velocity": "m/s", "time": "s"},
        "time_axis_s": times,
        "frame_count": len(frame_records),
        "peak_depth_m": round(peak_depth, 4),
        "provenance": provenance,
        "frames": frame_records,
        "alerts_file": "alerts.json",
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(run_dir, "alerts.json"), "w") as f:
        json.dump(alerts or [], f, indent=2)

    return manifest

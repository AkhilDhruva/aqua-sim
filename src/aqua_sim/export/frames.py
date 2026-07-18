"""Frame export — the contract between the Python engine and the Three.js viewer.

A completed run is written as a self-describing folder:

    run_dir/
      manifest.json     grid geometry, CRS, time axis, units, PROVENANCE,
                        hazard thresholds, node list
      terrain.json      static bed elevation + obstacle heights + AOI mask
      frame_001.json    per-timestep water depth & speed (+ peak stats)  ...
      frame_100.json
      alerts.json       time-stamped, severity-ranked risk log

The manifest's ``provenance`` block is what gives the model its authority: it
records the data source, a content digest of the terrain, resolution, CRS,
solver scheme, and configuration so any reviewer can trace a result back to its
inputs. The ``hazard`` block exports the engine's hazard constants
(risk/hazard.py) so the viewer colors water with the same thresholds the alerts
were computed from. See docs/ARCHITECTURE.md §6.

JSON is used for the thin slice (human-readable, zero-dependency). The format is
versioned (``format_version``) so a later phase can swap frame bodies for compact
binary / typed-array buffers without breaking the viewer.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Callable, Iterable, Union

from aqua_sim.config import SimConfig
from aqua_sim.grid import Grid
from aqua_sim.physics.swe import FlowState
from aqua_sim.risk.hazard import DEBRIS_FACTOR, DEPTH_CRITICAL_M, HR_BANDS

# 2.0: frames carry a per-cell "speed" grid (required); terrain.json carries the
# AOI "mask"; manifest carries "hazard" thresholds and a terrain content digest.
FORMAT_VERSION = "2.0"


def _round_grid(matrix, ndigits: int = 4):
    return [[round(v, ndigits) for v in row] for row in matrix]


def _representative_manning(grid: Grid) -> float:
    """The most common Manning's n on the grid — the headline roughness value."""
    counts: dict[float, int] = {}
    for row in grid.manning:
        for n in row:
            counts[round(n, 4)] = counts.get(round(n, 4), 0) + 1
    return max(counts, key=counts.get) if counts else 0.0


def _terrain_digest(grid: Grid) -> str:
    """SHA-256 over the elevation and obstacle content (mm precision).

    Two different terrains at the same file path get different digests — so the
    run id genuinely covers *what* was simulated, not just where it came from.
    """
    hasher = hashlib.sha256()
    for matrix in (grid.z, grid.obstacle):
        for row in matrix:
            hasher.update(",".join(f"{v:.3f}" for v in row).encode())
            hasher.update(b";")
    return hasher.hexdigest()[:16]


def _run_id(provenance: dict) -> str:
    """Deterministic content hash of the generation parameters.

    Same inputs -> same id. Because the provenance block includes the terrain
    content digest, identical configuration over *different* terrain yields a
    different id. This proves a frame set was deterministically derived from a
    specific configuration and terrain (anyone can recompute it), without a
    misleading wall-clock or decorative signature.
    """
    canonical = json.dumps(provenance, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def write_run(
    run_dir: str,
    grid: Grid,
    frames: Iterable[FlowState],
    config: SimConfig,
    alerts: Union[list[dict], Callable[[], list[dict]], None] = None,
    frame_breaches: list[list[dict]] | None = None,
    nodes: list[dict] | None = None,
) -> dict:
    """Serialize a completed run to ``run_dir``. Returns the manifest dict.

    ``frames`` may be a generator; it is consumed once and written incrementally
    as ``frame_001.json`` .. ``frame_NNN.json``, so a streaming pipeline holds
    only one frame in memory. ``frame_breaches[i]`` is the breach list embedded
    into frame ``i+1``; it may be a list that a streaming producer appends to as
    each frame is pulled (it is read only after the frame is consumed).
    ``alerts`` may be a zero-arg callable, evaluated after all frames are
    consumed — for streaming producers whose alert log completes with the last
    frame. The solver scheme is read from ``config.solver.scheme`` (validated at
    solver construction), never passed separately.
    """
    os.makedirs(run_dir, exist_ok=True)

    # Static terrain (loaded once by the viewer). The mask matters: DEM ingestion
    # void-fills nodata cells, and only the mask tells the viewer they are not
    # real terrain.
    with open(os.path.join(run_dir, "terrain.json"), "w") as f:
        json.dump(
            {"nx": grid.nx, "ny": grid.ny, "dx": grid.dx,
             "z": _round_grid(grid.z, 3), "obstacle": _round_grid(grid.obstacle, 2),
             "mask": [[bool(v) for v in row] for row in grid.mask]},
            f,
        )

    # Provenance is computed up front so every frame can carry the run id.
    provenance = {
        "terrain_source": grid.meta.get("source"),
        "terrain_digest": _terrain_digest(grid),
        "resolution_m": grid.meta.get("resolution_m", grid.dx),
        "crs": grid.crs,
        "solver_scheme": config.solver.scheme,
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
        "solver_scheme": config.solver.scheme,
    }

    frame_records = []
    times = []
    peak_depth = 0.0
    breach_list = frame_breaches if frame_breaches is not None else []
    for i, state in enumerate(frames, start=1):
        name = f"frame_{i:03d}.json"
        # Read AFTER the state is consumed: a streaming producer appends this
        # frame's breaches as a side effect of yielding the state.
        breaches = breach_list[i - 1] if i - 1 < len(breach_list) else []
        with open(os.path.join(run_dir, name), "w") as f:
            json.dump(
                {"index": i, "time_s": round(state.time_s, 3),
                 "provenance": frame_header,
                 "depth": _round_grid(state.depth, 4),
                 "speed": _round_grid(state.speed, 3),
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

    if callable(alerts):
        alerts = alerts()  # streaming producers finish their log with the frames

    manifest = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "aoi": config.aoi_name,
        "grid": {"nx": grid.nx, "ny": grid.ny, "dx_m": grid.dx, "crs": grid.crs},
        "units": {"elevation": "m", "depth": "m", "velocity": "m/s", "time": "s"},
        "time_axis_s": times,
        "frame_count": len(frame_records),
        "peak_depth_m": round(peak_depth, 4),
        # The engine's hazard constants — the viewer reads these so shader
        # coloring and alert classification can never drift apart.
        "hazard": {
            "debris_factor": DEBRIS_FACTOR,
            "hr_bands": HR_BANDS,
            "depth_critical_m": DEPTH_CRITICAL_M,
        },
        "provenance": provenance,
        "nodes": nodes or [],  # sink-node positions so the viewer can mark them
        "frames": frame_records,
        "alerts_file": "alerts.json",
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(run_dir, "alerts.json"), "w") as f:
        json.dump(alerts or [], f, indent=2)

    return manifest

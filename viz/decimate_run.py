#!/usr/bin/env python3
"""Write a decimated copy of a run folder that a browser can load.

    python3 viz/decimate_run.py <run_dir> <out_dir> --cell-stride 2 [--frame-stride 1]

The dashboard loads every frame at once. A 1.4M-cell street run is ~14 MB of
JSON per frame, too heavy for a browser tab, so this writes the same
run-folder contract at reduced resolution, using the holo table's decimation
(``viz/holo/build_artifact.decimate``): every NxN block keeps its MAXIMUM
depth and speed (a one-cell-wide flooded street is not averaged away), mean
elevation, max obstacle height and any-mask. ``buildings.json`` keeps its
exact 1 m geometry; only its grid block and per-building cell boxes are
remapped. The manifest is stamped ``derived`` and its area name says
DECIMATED, so the copy can never pass for the engine's output.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "holo"))

from build_artifact import decimate  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--cell-stride", type=int, default=2)
    ap.add_argument("--frame-stride", type=int, default=1)
    a = ap.parse_args()

    src = a.run_dir
    manifest = json.load(open(os.path.join(src, "manifest.json")))
    terrain = json.load(open(os.path.join(src, "terrain.json")))
    frames = [json.load(open(os.path.join(src, f["file"]))) for f in manifest["frames"]]
    manifest, terrain, frames = decimate(manifest, terrain, frames, a.cell_stride, a.frame_stride)

    os.makedirs(a.out_dir, exist_ok=True)
    files = []
    for i, fr in enumerate(frames, 1):
        name = f"frame_{i:03d}.json"
        files.append(name)
        with open(os.path.join(a.out_dir, name), "w") as f:
            json.dump(fr, f)
    manifest["frames"] = [dict(m, file=n) for m, n in zip(manifest["frames"], files)]
    with open(os.path.join(a.out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    with open(os.path.join(a.out_dir, "terrain.json"), "w") as f:
        json.dump(terrain, f)
    for extra in ("alerts.json", "run_notes.json"):
        p = os.path.join(src, extra)
        if os.path.exists(p):
            shutil.copy(p, os.path.join(a.out_dir, extra))
    bp = os.path.join(src, "buildings.json")
    if os.path.exists(bp):
        s = a.cell_stride
        doc = json.load(open(bp))
        doc["grid"] = {"nx": terrain["nx"], "ny": terrain["ny"], "dx": terrain["dx"]}
        for t in doc["tiles"]:
            for b in t["buildings"]:
                i0, j0, i1, j1 = b["cells"]
                b["cells"] = [i0 // s, j0 // s, min(i1 // s, terrain["nx"] - 1), min(j1 // s, terrain["ny"] - 1)]
        doc["derived"] = {"cell_stride": s, "note": "cell boxes remapped to the decimated grid; geometry unchanged"}
        with open(os.path.join(a.out_dir, "buildings.json"), "w") as f:
            json.dump(doc, f)
    print(f"[decimate] {terrain['nx']}x{terrain['ny']} @ {terrain['dx']} m, {len(frames)} frames -> {a.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

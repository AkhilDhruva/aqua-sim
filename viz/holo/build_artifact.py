#!/usr/bin/env python3
"""Bundle the holo table and one run into a single self-contained HTML fragment.

Usage: python3 viz/holo/build_artifact.py <run_dir> <out.html> [--three-url URL]
                                          [--cell-stride N] [--frame-stride M]

The served page (index.html + holo.js) fetches a run folder and imports Three.js
from ../vendor. A hosted artifact can do neither, so this script:

  * embeds the run (manifest, terrain, alerts, every frame) as window.__HOLO_RUN,
    which holo.js checks before fetching anything;
  * inlines the vendored OrbitControls into the page's module script, rewriting
    its bare `from 'three'` import into a destructure of the THREE namespace;
  * imports Three.js itself from one pinned CDN URL (r160, the vendored version);
  * emits only <title>, <link>, <style> and body content — no doctype, html,
    head or body tags — which is what the artifact publisher expects.

The output is deterministic for a given run folder and source files, so its
SHA-256 identifies exactly which run and which viewer revision were published.

Decimation (--cell-stride N, --frame-stride M): a street-resolution run is far
larger than a hosted page may be (a 0.5M-cell, 25-frame run is ~120 MB of
JSON; the publisher's limit is 16 MB). With N > 1 every NxN block of cells
becomes one cell carrying the block's MAXIMUM depth and speed (so a flooded
street one cell wide is not averaged away), the MEAN elevation, the MAX
obstacle height and ANY-mask; with M > 1 only every M-th frame is kept, plus
the last. The manifest is stamped with a ``derived`` block naming the source
run_id and the strides, and the area name gains a DECIMATED suffix, so the
hosted copy can never pass for the engine's output. Values are rounded to mm.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "..", "vendor")
# Tried in order; both hosts are on the artifact publisher's script allowlist.
# The study host's egress proxy forbids them, so reachability cannot be checked
# from here — hence the runtime fallback rather than a build-time choice.
THREE_URLS = [
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.module.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js",
]


def _blocks(a, s, how, mask=None):
    """Aggregate a 2-D array into s x s blocks (edge blocks may be partial)."""
    import numpy as np
    a = np.asarray(a)
    ny, nx = a.shape
    py, px = (-ny) % s, (-nx) % s
    shp = (ny // s + (py > 0), s, nx // s + (px > 0), s)
    if how == "max":
        pad = np.pad(a, ((0, py), (0, px)), constant_values=-np.inf if a.dtype.kind == "f" else 0)
        return pad.reshape(shp).max(axis=(1, 3))
    if how == "any":
        pad = np.pad(a.astype(bool), ((0, py), (0, px)), constant_values=False)
        return pad.reshape(shp).any(axis=(1, 3))
    # masked mean: ignore cells outside the mask when averaging elevation
    vals = a.astype(float); msk = np.asarray(mask, dtype=bool)
    num = np.pad(np.where(msk, vals, 0.0), ((0, py), (0, px))).reshape(shp).sum(axis=(1, 3))
    den = np.pad(msk.astype(float), ((0, py), (0, px))).reshape(shp).sum(axis=(1, 3))
    fill = float(vals[msk].min()) if msk.any() else 0.0
    return np.where(den > 0, num / np.maximum(den, 1), fill)


def _mm(a):
    """Round to millimetres and emit exact zeros as ints (most cells are dry)."""
    out = []
    for row in a.tolist():
        out.append([0 if v == 0 else round(v, 3) for v in row])
    return out


def decimate(manifest, terrain, frames, cell_stride, frame_stride):
    import numpy as np
    s = int(cell_stride)
    if s > 1:
        mask = np.asarray(terrain["mask"], dtype=bool)
        z = _blocks(terrain["z"], s, "mean", mask=mask)
        obst = _blocks(np.asarray(terrain["obstacle"], dtype=float), s, "max")
        m2 = _blocks(mask, s, "any")
        ny2, nx2 = m2.shape
        terrain = {"nx": nx2, "ny": ny2, "dx": terrain["dx"] * s, "z": _mm(z), "obstacle": _mm(obst),
                   "mask": m2.tolist()}
        manifest = dict(manifest)
        manifest["grid"] = dict(manifest.get("grid", {}), nx=nx2, ny=ny2, dx_m=terrain["dx"])
        manifest["nodes"] = [dict(n, x=int(n["x"]) // s, y=int(n["y"]) // s) for n in manifest.get("nodes", [])]
        for fr in frames:
            fr["depth"] = _mm(_blocks(np.asarray(fr["depth"], dtype=float), s, "max"))
            fr["speed"] = _mm(_blocks(np.asarray(fr["speed"], dtype=float), s, "max"))
    m = int(frame_stride)
    if m > 1:
        keep = sorted(set(range(0, len(frames), m)) | {len(frames) - 1})
        frames = [frames[i] for i in keep]
        manifest = dict(manifest)
        manifest["frames"] = [manifest["frames"][i] for i in keep]
        manifest["time_axis_s"] = [manifest["time_axis_s"][i] for i in keep]
        manifest["frame_count"] = len(frames)
    if s > 1 or m > 1:
        manifest["derived"] = {"from_run_id": manifest["run_id"], "cell_stride": s, "frame_stride": m,
                               "method": "block max depth/speed, block mean elevation, block max obstacle, any-mask; mm rounding",
                               "note": "decimated copy built for the hosted holo table — not the engine's output"}
        manifest["aoi"] = f"{manifest.get('aoi', '')} — DECIMATED ×{s} cells / ×{m} frames for hosting"
    return manifest, terrain, frames


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    run_dir, out = sys.argv[1], sys.argv[2]
    three_urls = list(THREE_URLS)
    if "--three-url" in sys.argv:
        three_urls = [sys.argv[sys.argv.index("--three-url") + 1]] + three_urls
    cell_stride = int(sys.argv[sys.argv.index("--cell-stride") + 1]) if "--cell-stride" in sys.argv else 1
    frame_stride = int(sys.argv[sys.argv.index("--frame-stride") + 1]) if "--frame-stride" in sys.argv else 1

    manifest = json.load(open(os.path.join(run_dir, "manifest.json")))
    terrain = json.load(open(os.path.join(run_dir, "terrain.json")))
    alerts = []
    ap = os.path.join(run_dir, manifest.get("alerts_file") or "alerts.json")
    if os.path.exists(ap):
        alerts = json.load(open(ap))
    frames = [json.load(open(os.path.join(run_dir, f["file"]))) for f in manifest["frames"]]
    manifest, terrain, frames = decimate(manifest, terrain, frames, cell_stride, frame_stride)
    payload = json.dumps({"manifest": manifest, "terrain": terrain, "alerts": alerts, "frames": frames},
                         separators=(",", ":"))
    payload = payload.replace("</", "<\\/")          # never terminate the script element early

    html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "holo.js"), encoding="utf-8").read()
    orbit = open(os.path.join(VENDOR, "OrbitControls.js"), encoding="utf-8").read()

    # OrbitControls: `import { A, B, ... } from 'three';` -> `const { A, B, ... } = THREE;`
    orbit = re.sub(r"import\s*\{([^}]*)\}\s*from\s*['\"]three['\"];", r"const {\1} = THREE;", orbit, count=1)
    orbit = orbit.replace("export class ", "class ").replace("export {", "// export {")

    loader = ("let THREE = null;\n"
              f"for (const u of {json.dumps(three_urls)}) {{\n"
              "  try { THREE = await import(u); break; } catch (e) { console.warn('three.js import failed:', u, e); }\n"
              "}\n"
              "if (!THREE) { document.getElementById('load-msg').textContent = 'could not load Three.js from any CDN'; throw new Error('three.js unavailable'); }\n")
    js = js.replace("import * as THREE from 'three';", loader)
    js = js.replace("import { OrbitControls } from '../vendor/OrbitControls.js';", "/* OrbitControls inlined below */\n" + orbit + "\n")
    js = js.replace("</script", "<\\/script")

    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    link = re.search(r"<link rel=\"stylesheet\"[^>]*>", html).group(0)
    style = re.search(r"<style>.*?</style>", html, re.S).group(0)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    body = re.sub(r"<script type=\"importmap\">.*?</script>", "", body, flags=re.S)
    body = body.replace('<script type="module" src="./holo.js"></script>', "")

    doc = (f"<title>{title}</title>\n{link}\n{style}\n{body}\n"
           f"<script>window.__HOLO_RUN = {payload};</script>\n"
           f"<script type=\"module\">\n{js}\n</script>\n")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write(doc)
    import hashlib
    print(f"-> {out}  {len(doc):,} bytes  frames={len(frames)}  cells={terrain['nx'] * terrain['ny']}  "
          f"sha256={hashlib.sha256(doc.encode()).hexdigest()[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())

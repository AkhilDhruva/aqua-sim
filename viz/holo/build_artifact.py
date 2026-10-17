#!/usr/bin/env python3
"""Bundle the holo table and one run into a single self-contained HTML fragment.

Usage: python3 viz/holo/build_artifact.py <run_dir> <out.html> [--three-url URL]

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


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    run_dir, out = sys.argv[1], sys.argv[2]
    three_urls = list(THREE_URLS)
    if "--three-url" in sys.argv:
        three_urls = [sys.argv[sys.argv.index("--three-url") + 1]] + three_urls

    manifest = json.load(open(os.path.join(run_dir, "manifest.json")))
    terrain = json.load(open(os.path.join(run_dir, "terrain.json")))
    alerts = []
    ap = os.path.join(run_dir, manifest.get("alerts_file") or "alerts.json")
    if os.path.exists(ap):
        alerts = json.load(open(ap))
    frames = [json.load(open(os.path.join(run_dir, f["file"]))) for f in manifest["frames"]]
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

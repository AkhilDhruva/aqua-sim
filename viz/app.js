// aqua-sim telemetry dashboard.
//
// The "brain" (Python solver) writes a run folder; this viewer is the thin
// telemetry layer. It loads the static terrain ONCE, then cycles the exported
// depth/speed frames, driving a custom kinetic shader and an alert matrix keyed
// to the timeline scrubber. No physics runs here.

import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { PALETTES, DEFAULT_PALETTE, THRESHOLDS, paletteUniforms } from './palettes.js';
import { BuildingsLayer, buildingFloodStats } from './buildings-layer.js';

const state = {
  manifest: null,
  terrain: null,
  frames: [],       // loaded frame objects
  alerts: [],
  nx: 0, ny: 0, dx: 1,
  cur: 0,
  playing: false,
  fps: 8,
  lastStep: 0,
  palette: DEFAULT_PALETTE,
  waterMat: null,
  waterGeom: null,
  scene: null, camera: null, renderer: null, controls: null,
  vertExag: 4.0,        // derived per run from terrain relief (see buildScene)
  hazard: null,         // engine hazard thresholds from manifest.json
  nodes: [],            // sink-node positions from manifest.json
  nodeMarkers: [],      // {node, mesh} pairs in the scene
  buildings: null,      // BuildingsLayer instance (created with the scene)
  buildingsDoc: null,   // parsed buildings.json, when the run provides one
  basemapPlane: null,   // street-basemap ground plane (presentation only)
};

// Engine hazard thresholds for the shader/legend: prefer the values the run's
// manifest exports (the same constants the alerts were computed from); fall
// back to the palettes.js defaults only for pre-2.0 runs.
function hazardParams() {
  const h = state.hazard || {};
  return {
    dCrit: h.depth_critical_m ?? THRESHOLDS.D_CRIT,
    hrCrit: (h.hr_bands && h.hr_bands.moderate) ?? THRESHOLDS.HR_CRIT,
    debris: h.debris_factor ?? 0.5,
  };
}

// ---------- data loading ----------

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function runBase() {
  // The ?run= query seeds the input once (see initUI); after that the input is
  // the single source of truth, so "Load run" always honors what the user typed.
  let base = document.getElementById('runPath').value || 'sample_run';
  if (!base.endsWith('/')) base += '/';
  return base;
}

async function loadRun() {
  const base = runBase();
  setStatus(`Loading ${base} …`);
  try {
    const manifest = await fetchJSON(base + 'manifest.json');
    const terrain = await fetchJSON(base + 'terrain.json');
    let alerts = [];
    try { alerts = await fetchJSON(base + (manifest.alerts_file || 'alerts.json')); } catch (e) {}
    let buildingsDoc = null;   // optional layer — separate contract from terrain.json
    try { buildingsDoc = await fetchJSON(base + 'buildings.json'); } catch (e) {}

    setStatus(`Loading ${manifest.frame_count} frames …`);
    const frames = await Promise.all(
      manifest.frames.map((f) => fetchJSON(base + f.file))
    );

    state.manifest = manifest;
    state.terrain = terrain;
    state.frames = frames;
    state.alerts = alerts;
    state.nodes = manifest.nodes || [];
    state.hazard = manifest.hazard || null;
    state.buildingsDoc = buildingsDoc;
    state.nx = terrain.nx; state.ny = terrain.ny; state.dx = terrain.dx;
    state.cur = 0;

    buildScene();
    buildBuildings();
    buildBasemap();
    applyLayerState();   // reflect checkbox state onto freshly-created objects
    buildPresets();
    buildAlertMatrix();
    showProvenance();
    showSources();
    renderLegend();
    setFrame(0);
    setStatus('');
    document.getElementById('scrubber').max = String(frames.length - 1);
  } catch (err) {
    setStatus(`Load failed: ${err.message}. Serve the folder over HTTP ` +
              `(e.g. "python -m http.server" in the repo, then open viz/).`);
    console.error(err);
  }
}

// ---------- scene ----------

function buildScene() {
  const canvas = document.getElementById('gl');
  const { nx, ny, dx, terrain } = { ...state, terrain: state.terrain };

  if (!state.renderer) {
    state.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    state.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    state.scene = new THREE.Scene();
    state.scene.background = new THREE.Color(0x0a0e14);
    state.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1e6);
    state.controls = new OrbitControls(state.camera, canvas);
    state.controls.enableDamping = true;
    // Intensities calibrated for three.js r155+ physical lighting (the legacy
    // 0.6/0.9 pair renders up-facing surfaces near-ambient => dark roofs).
    state.scene.add(new THREE.AmbientLight(0xffffff, 1.7));
    const sun = new THREE.DirectionalLight(0xffffff, 2.8);
    sun.position.set(1, 2, 1);
    state.scene.add(sun);
    window.addEventListener('resize', onResize);
  }
  // Clear any previous terrain/water on reload — dispose GPU resources
  // (geometry AND material/textures) so repeated "Load run" doesn't leak.
  for (const name of ['terrainMesh', 'waterMesh']) {
    const old = state.scene.getObjectByName(name);
    if (old) {
      state.scene.remove(old);
      old.geometry.dispose();
      if (old.material) old.material.dispose();
    }
  }
  for (const m of state.nodeMarkers) {
    state.scene.remove(m.group);
    m.group.traverse((o) => {
      o.geometry?.dispose();
      if (o.material) { o.material.map?.dispose(); o.material.dispose(); }
    });
  }
  state.nodeMarkers = [];

  const W = (nx - 1) * dx, H = (ny - 1) * dx;

  // ----- terrain mesh (elevation + buildings, colored by height) -----
  const tGeom = gridGeometry(nx, ny, dx);
  const tPos = tGeom.attributes.position;
  const tCol = [];
  // When the run ships a buildings layer, the terrain stays BARE EARTH —
  // extruding obstacle cells here would double-draw dark, cell-quantized
  // building blocks on top of the real extruded footprints (the "dark noisy
  // roofs" artifact). Without a buildings layer, obstacle extrusion remains
  // the visual for walls.
  const extrudeObstacles = !state.buildingsDoc;
  let zmin = Infinity, zmax = -Infinity;
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    if (terrain.mask && terrain.mask[j] && terrain.mask[j][i] === false) continue;
    const e = terrain.z[j][i] + (extrudeObstacles ? (terrain.obstacle?.[j]?.[i] || 0) : 0);
    zmin = Math.min(zmin, e); zmax = Math.max(zmax, e);
  }
  // Vertical exaggeration derived from this run's relief: exaggerate flat
  // terrain so ponding reads, leave mountainous terrain near true scale.
  // Building-aware runs render at TRUE scale (1x) — buildings at true height
  // demand an unexaggerated ground, or proportions lie.
  const relief = Math.max(zmax - zmin, 1e-6);
  state.vertExag = state.buildingsDoc
    ? 1.0
    : Math.min(12, Math.max(1, (0.025 * Math.max(W, H)) / relief));
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i;
    const masked = terrain.mask && terrain.mask[j] && terrain.mask[j][i] === false;
    const base = terrain.z[j][i];
    const obs = extrudeObstacles ? (terrain.obstacle?.[j]?.[i] || 0) : 0;
    const e = base + obs;
    tPos.setY(k, e * state.vertExag);
    let col;
    if (masked) col = new THREE.Color(0x0a0d13);          // nodata / outside AOI: void
    else if (obs > 0) col = new THREE.Color(0x2b3550);    // walls (no bldg layer): slate
    else {
      const t = (e - zmin) / Math.max(zmax - zmin, 1e-6);
      col = new THREE.Color().setHSL(0.30 - 0.12 * t, 0.35, 0.18 + 0.30 * t);
    }
    tCol.push(col.r, col.g, col.b);
  }
  tGeom.setAttribute('color', new THREE.Float32BufferAttribute(tCol, 3));
  tGeom.computeVertexNormals();
  const tMesh = new THREE.Mesh(
    tGeom, new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: false, roughness: 0.95 }));
  tMesh.name = 'terrainMesh';
  state.scene.add(tMesh);

  // ----- water mesh (kinetic shader) -----
  const wGeom = gridGeometry(nx, ny, dx);
  const aBase = new Float32Array(nx * ny);
  const aDepth = new Float32Array(nx * ny);
  const aSpeed = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) aBase[j * nx + i] = terrain.z[j][i];
  wGeom.setAttribute('aBase', new THREE.BufferAttribute(aBase, 1));
  wGeom.setAttribute('aDepth', new THREE.BufferAttribute(aDepth, 1));
  wGeom.setAttribute('aSpeed', new THREE.BufferAttribute(aSpeed, 1));
  state.waterGeom = wGeom;
  state.waterMat = makeWaterMaterial();
  const wMesh = new THREE.Mesh(wGeom, state.waterMat);
  wMesh.name = 'waterMesh';
  state.scene.add(wMesh);

  // ----- sink-node markers (subway stations etc.) -----
  const cxg = (nx - 1) / 2, cyg = (ny - 1) / 2;
  const pinH = Math.max(W, H) * 0.03;
  const pinR = Math.max(W, H) * 0.004;
  for (const node of state.nodes) {
    if (node.x == null || node.y == null) continue;
    const gx = (node.x - cxg) * dx, gz = (node.y - cyg) * dx;
    const gy = (terrain.z[node.y]?.[node.x] || 0) * state.vertExag;
    const group = new THREE.Group();
    const pin = new THREE.Mesh(
      new THREE.ConeGeometry(pinR, pinH, 8),
      new THREE.MeshBasicMaterial({ color: 0xffffff }));
    pin.position.set(gx, gy + pinH / 2, gz);
    pin.rotation.x = Math.PI;  // point down at the station
    group.add(pin);
    const label = makeTextSprite(node.name);
    const lh = pinH * 0.7;  // label height in world units
    label.scale.set(lh * label.userData.aspect, lh, 1);
    label.position.set(gx, gy + pinH * 1.5, gz);
    group.add(label);
    state.scene.add(group);
    state.nodeMarkers.push({ node, pin, group, baseColor: 0xffffff });
  }

  // camera framing
  const span = Math.max(W, H);
  state.camera.position.set(0, span * 0.7, span * 0.9);
  state.camera.lookAt(0, 0, 0);
  state.controls.target.set(0, 0, 0);
  onResize();
}

// A small canvas-textured label that always faces the camera.
function makeTextSprite(text) {
  const pad = 8, fs = 42;
  const cvs = document.createElement('canvas');
  const ctx = cvs.getContext('2d');
  ctx.font = `${fs}px ui-monospace, monospace`;
  const w = Math.ceil(ctx.measureText(text).width) + pad * 2;
  cvs.width = w; cvs.height = fs + pad * 2;
  ctx.font = `${fs}px ui-monospace, monospace`;
  ctx.fillStyle = 'rgba(10,14,20,0.82)';
  ctx.fillRect(0, 0, cvs.width, cvs.height);
  ctx.fillStyle = '#dfe7f2';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, pad, cvs.height / 2);
  const tex = new THREE.CanvasTexture(cvs);
  tex.minFilter = THREE.LinearFilter;
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false }));
  const scale = 0.0;  // set by caller relative to domain, below
  spr.userData.aspect = cvs.width / cvs.height;
  spr.scale.set(1, 1, 1);
  return spr;
}

// A flat XZ grid; Y is filled in later (terrain elevation / water surface).
function gridGeometry(nx, ny, dx) {
  const geom = new THREE.BufferGeometry();
  const pos = new Float32Array(nx * ny * 3);
  const cx = (nx - 1) / 2, cy = (ny - 1) / 2;
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = (j * nx + i) * 3;
    pos[k] = (i - cx) * dx;
    pos[k + 1] = 0;
    pos[k + 2] = (j - cy) * dx;
  }
  geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const idx = [];
  for (let j = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++) {
    const a = j * nx + i, b = a + 1, c = a + nx, d = c + 1;
    idx.push(a, c, b, b, c, d);
  }
  geom.setIndex(idx);
  return geom;
}

// Pack a palette's stops into THREE.Vector3 uniforms — the single helper used
// by both the initial material build and live palette switches.
function stopVectors(key) {
  const pu = paletteUniforms(key);
  const vecs = [];
  for (let t = 0; t < pu.count; t++) {
    vecs.push(new THREE.Vector3(pu.colors[t * 3], pu.colors[t * 3 + 1], pu.colors[t * 3 + 2]));
  }
  return { vecs, positions: pu.positions, count: pu.count };
}

function makeWaterMaterial() {
  const sv = stopVectors(state.palette);
  const hp = hazardParams();
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    uniforms: {
      uExag: { value: state.vertExag },
      uDcrit: { value: hp.dCrit },
      uHRcrit: { value: hp.hrCrit },
      uDebris: { value: hp.debris },
      uKineticFloor: { value: THRESHOLDS.KINETIC_FLOOR },
      uMinDepth: { value: THRESHOLDS.MIN_DEPTH },
      uAlphaFull: { value: THRESHOLDS.ALPHA_FULL },
      uAlphaMin: { value: THRESHOLDS.ALPHA_MIN },
      uAlphaMax: { value: THRESHOLDS.ALPHA_MAX },
      uStopColor: { value: sv.vecs },
      uStopPos: { value: sv.positions },
      uNumStops: { value: sv.count },
    },
    vertexShader: `
      attribute float aBase; attribute float aDepth; attribute float aSpeed;
      uniform float uExag;
      varying float vDepth; varying float vSpeed;
      void main() {
        vDepth = aDepth; vSpeed = aSpeed;
        vec3 p = position;
        p.y = (aBase + aDepth) * uExag;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }`,
    fragmentShader: `
      precision highp float;
      varying float vDepth; varying float vSpeed;
      uniform float uDcrit, uHRcrit, uDebris, uKineticFloor, uMinDepth, uAlphaFull, uAlphaMin, uAlphaMax;
      uniform vec3 uStopColor[5]; uniform float uStopPos[5]; uniform int uNumStops;
      vec3 gradient(float t) {
        if (t <= uStopPos[0]) return uStopColor[0];
        for (int i = 1; i < 5; i++) {
          if (i >= uNumStops) break;
          if (t <= uStopPos[i]) {
            float f = (t - uStopPos[i-1]) / max(uStopPos[i] - uStopPos[i-1], 1e-5);
            return mix(uStopColor[i-1], uStopColor[i], f);
          }
        }
        return uStopColor[uNumStops - 1];
      }
      void main() {
        if (vDepth < uMinDepth) discard;
        float depthN = vDepth / uDcrit;
        // Kinetic (velocity) danger only counts once water is deep enough to be
        // a real surge — below the floor, a fast thin film over a steep slope is
        // hillside runoff, not a critical hazard, so it must not read crimson.
        float kinetic = step(uKineticFloor, vDepth);
        float hazard = vDepth * (vSpeed + uDebris) * kinetic;   // engine's HR = d*(v+DF)
        float hazardN = hazard / uHRcrit;
        float danger = clamp(max(depthN, hazardN), 0.0, 1.0);
        vec3 col = gradient(danger);
        float alpha = mix(uAlphaMin, uAlphaMax, clamp(vDepth / uAlphaFull, 0.0, 1.0));
        gl_FragColor = vec4(col, alpha);
      }`,
  });
}

function applyPalette(key) {
  state.palette = key;
  if (!state.waterMat) return;
  const sv = stopVectors(key);
  state.waterMat.uniforms.uStopColor.value = sv.vecs;
  state.waterMat.uniforms.uStopPos.value = sv.positions;
  state.waterMat.uniforms.uNumStops.value = sv.count;
  state.waterMat.uniformsNeedUpdate = true;
  renderLegend();
}

// ---------- layers ----------

// Layer visibility setters. Basemap is a disabled placeholder: streaming
// Photorealistic 3D Tiles needs an API key and live attribution and may never
// feed the solver or exports — see viz/README.md.
const LAYER_FNS = {
  lyrTerrain: (v) => { const m = state.scene?.getObjectByName('terrainMesh'); if (m) m.visible = v; },
  lyrBuildings: (v) => state.buildings?.setVisible(v),
  lyrWater: (v) => { const m = state.scene?.getObjectByName('waterMesh'); if (m) m.visible = v; },
  lyrSensors: (v) => { for (const mk of state.nodeMarkers) mk.group.visible = v; },
  lyrBasemap: (v) => setBasemapVisible(v),
};

function applyLayerState() {
  for (const id of Object.keys(LAYER_FNS)) {
    const box = document.getElementById(id);
    if (box && !box.disabled) LAYER_FNS[id](box.checked);
  }
}

// ---------- buildings layer, presets, picking ----------

function buildBuildings() {
  if (!state.buildings) state.buildings = new BuildingsLayer(state.scene);
  state.buildings.clear();
  document.getElementById('bInfo').style.display = 'none';
  const chk = document.getElementById('lyrBuildings');
  if (!state.buildingsDoc) {
    chk.disabled = true;
    document.getElementById('bCount').textContent = 'none in this run';
    return;
  }
  chk.disabled = false;
  const stats = state.buildings.build(state.buildingsDoc, state.vertExag);
  state.buildings.setVisible(chk.checked);
  document.getElementById('bCount').textContent =
    `${stats.buildings} bldgs / ${stats.tiles} tiles`;
}

// Street basemap: an attributed ground plane under the terrain. Official NYC
// planimetric map tiles (or OSM) stream onto it at runtime when network is
// available; otherwise it stays a neutral attributed plane. It NEVER feeds the
// solver or exports — presentation only.
const BASEMAPS = {
  nyc: { label: 'NYC tiles',
         attr: 'Basemap: NYC OTI Map Tiles © City of New York (gis.nyc.gov/tiles)',
         template: 'https://gis.nyc.gov/tiles/{z}/{x}/{y}.png' },
  osm: { label: 'OSM',
         attr: 'Basemap © OpenStreetMap contributors',
         template: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png' },
};

function buildBasemap() {
  const scene = state.scene;
  const old = scene.getObjectByName('basemapPlane');
  if (old) { scene.remove(old); old.geometry.dispose(); old.material.dispose(); }
  const { nx, ny, dx } = { nx: state.nx, ny: state.ny, dx: state.dx };
  const [zmin] = state.terrain ? terrainMinMax() : [0, 0];
  const geo = new THREE.PlaneGeometry(nx * dx, ny * dx);
  geo.rotateX(-Math.PI / 2);
  const mat = new THREE.MeshBasicMaterial({ color: 0x141a24 });
  const plane = new THREE.Mesh(geo, mat);
  plane.name = 'basemapPlane';
  plane.position.y = (zmin - 1) * state.vertExag;   // just below terrain
  plane.visible = document.getElementById('lyrBasemap').checked;
  scene.add(plane);
  state.basemapPlane = plane;
  // Try to drape tiles when a network is available; silently keep the neutral
  // plane if not (egress-restricted environments simply see the plane).
  tryLoadBasemapTiles(plane);
}

function terrainMinMax() {
  let a = Infinity, b = -Infinity;
  const t = state.terrain;
  for (let j = 0; j < t.ny; j++) for (let i = 0; i < t.nx; i++) {
    if (t.mask && t.mask[j] && t.mask[j][i] === false) continue;
    const e = t.z[j][i]; if (e < a) a = e; if (e > b) b = e;
  }
  return [a, b];
}

function tryLoadBasemapTiles(plane) {
  const key = document.getElementById('bmSrc').dataset.key || 'nyc';
  const bm = BASEMAPS[key];
  // A single center tile as a lightweight drape (full slippy-tiling is a
  // later refinement); load errors are swallowed so offline stays graceful.
  if (!state.manifest?.provenance?.crs || !state.manifest.provenance) return;
  const loader = new THREE.TextureLoader();
  // Without a geo→tile transform we can't place real tiles precisely; this
  // hook is where a tiled basemap plugs in when network + a tile transform
  // are configured. Left as the neutral plane by default.
  void loader; void bm;
}

function setBasemapVisible(v) {
  if (state.basemapPlane) state.basemapPlane.visible = v;
  const attr = document.getElementById('basemapAttr');
  const key = document.getElementById('bmSrc').dataset.key || 'nyc';
  attr.style.display = v ? '' : 'none';
  attr.textContent = v ? BASEMAPS[key].attr : '';
}

function showSources() {
  const el = document.getElementById('sources');
  const p = state.manifest?.provenance || {};
  const rows = [];
  const add = (label, name, url) => rows.push(
    `<div class="src"><b>${label}:</b> <span>${name || '—'}` +
    (url ? ` · <a href="${url}" target="_blank" rel="noopener">source</a>` : '') +
    `</span></div>`);
  add('Terrain', p.terrain_source, p.terrain_meta?.source_path?.[0] || null);
  const bmeta = p.terrain_meta?.buildings;
  if (bmeta) add('Buildings', bmeta.name || 'NYC Building Footprints', bmeta.official_url);
  const cond = p.terrain_meta?.conditioning;
  if (cond) {
    for (const grp of ['surfaces', 'barriers']) {
      for (const k of Object.keys(cond[grp] || {})) {
        const c = cond[grp][k];
        add(k, c.name, c.official_url);
      }
    }
    if (cond.drainage) add('drainage', cond.drainage.name, cond.drainage.official_url);
  }
  add('Storm', `${p.storm?.rainfall_mm_per_hr ?? '?'} mm/hr · ${p.storm?.duration_hours ?? '?'} h`, null);
  add('Solver', `${p.solver_scheme} · ${p.solver?.backend || 'auto'}`, null);
  rows.push('<div class="src" style="margin-top:4px">No Google-derived geometry is used in the model or exports.</div>');
  el.innerHTML = rows.join('');
}

function buildPresets() {
  const holder = document.getElementById('presets');
  holder.innerHTML = '';
  const presets = state.buildingsDoc?.presets || [];
  for (const p of presets) {
    const btn = document.createElement('button');
    btn.textContent = p.name;
    btn.onclick = () => {
      state.controls.target.set(p.x, 0, p.z);
      state.camera.position.set(p.x + p.dist * 0.35, p.dist * 0.55, p.z + p.dist * 0.8);
    };
    holder.appendChild(btn);
  }
  holder.parentElement.style.display = presets.length ? '' : 'none';
}

function showBuildingInfo(b) {
  const panel = document.getElementById('bInfo');
  if (!b) { panel.style.display = 'none'; return; }
  const gridDims = { nx: state.nx, ny: state.ny };
  const s = buildingFloodStats(b, state.frames, gridDims, state.hazard);
  const cross = s.firstCrossing_s !== null
    ? `${(s.firstCrossing_s / 60).toFixed(0)} min` : 'never';
  // Ground elevation is the official dataset value (NAVD88) when present; the
  // DEM base (what the prism sits on) is shown separately when it differs.
  const groundStr = b.ground != null
    ? `${b.ground.toFixed(1)} m <span class="lyr-note">NAVD88</span>`
    : `${b.base.toFixed(1)} m <span class="lyr-note">DEM</span>`;
  panel.style.display = '';
  panel.innerHTML =
    `<b>Building ${b.bin || b.id}</b>${b.year ? ` · built ${b.year}` : ''}<br>` +
    `height <b>${b.h.toFixed(1)} m</b> · ground elev ${groundStr}<br>` +
    `peak adjacent depth <b>${s.peakAdjacentDepth.toFixed(2)} m</b><br>` +
    `first ≥critical crossing: <b>${cross}</b><br>` +
    `max hazard: <b class="hz-${s.maxHazardClass.toLowerCase()}">${s.maxHazardClass}</b>`;
}

const _ray = new THREE.Raycaster();
const _ptr = new THREE.Vector2();
let _downAt = null;

function initPicking(canvas) {
  canvas.addEventListener('pointerdown', (ev) => { _downAt = [ev.clientX, ev.clientY]; });
  canvas.addEventListener('pointerup', (ev) => {
    if (!_downAt) return;
    const moved = Math.hypot(ev.clientX - _downAt[0], ev.clientY - _downAt[1]);
    _downAt = null;
    if (moved > 5 || !state.buildings) return;   // it was a drag, not a click
    const r = canvas.getBoundingClientRect();
    _ptr.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    _ptr.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    _ray.setFromCamera(_ptr, state.camera);
    showBuildingInfo(state.buildings.pick(_ray));
  });
}

// ---------- frame stepping ----------

function setFrame(i) {
  const frames = state.frames;
  if (!frames.length) return;
  state.cur = Math.max(0, Math.min(i, frames.length - 1));
  const fr = frames[state.cur];
  const { nx, ny } = state;
  const aDepth = state.waterGeom.attributes.aDepth.array;
  const aSpeed = state.waterGeom.attributes.aSpeed.array;
  const speed = fr.speed && fr.speed.length ? fr.speed : null;
  for (let j = 0; j < ny; j++) for (let i2 = 0; i2 < nx; i2++) {
    const k = j * nx + i2;
    aDepth[k] = fr.depth[j][i2];
    aSpeed[k] = speed ? speed[j][i2] : 0;
  }
  state.waterGeom.attributes.aDepth.needsUpdate = true;
  state.waterGeom.attributes.aSpeed.needsUpdate = true;

  document.getElementById('scrubber').value = String(state.cur);
  updateHUD(fr);
  updateAlerts(fr);
}

function updateHUD(fr) {
  const m = state.manifest;
  const mins = (fr.time_s / 60).toFixed(1);
  document.getElementById('hud').innerHTML =
    `<b>${m.aoi}</b> &nbsp; frame ${state.cur + 1}/${state.frames.length} ` +
    `&nbsp; t = ${mins} min<br>` +
    `max depth ${fr.max_depth.toFixed(2)} m &nbsp; max speed ${fr.max_speed.toFixed(2)} m/s ` +
    `&nbsp; volume ${(fr.total_volume_m3 / 1000).toFixed(1)} ×10³ m³`;
}

// ---------- alert matrix ----------

function buildAlertMatrix() {
  const el = document.getElementById('alertList');
  if (!state.alerts.length) { el.innerHTML = '<div class="a-none">No alerts in this run.</div>'; return; }
  el.innerHTML = '';
  state.alerts.forEach((a, i) => {
    const d = document.createElement('div');
    d.className = 'a-row a-' + a.severity.toLowerCase();
    d.dataset.idx = String(i);
    d.innerHTML = `<span class="a-sev">${a.severity}</span>` +
      `<span class="a-t">${(a.time_s / 60).toFixed(0)}m</span>` +
      `<span class="a-msg">${a.message}</span>`;
    d.onclick = () => { seekToTime(a.time_s); };
    el.appendChild(d);
  });
}

function updateAlerts(fr) {
  // Highlight past/active alerts in the matrix.
  const rows = document.querySelectorAll('#alertList .a-row');
  rows.forEach((r) => {
    const a = state.alerts[Number(r.dataset.idx)];
    r.classList.toggle('a-fired', a.time_s <= fr.time_s + 1e-6);
  });

  // Light up station markers whose node is breaching on this frame.
  const activeBreaches = new Set((fr.breaches || []).map((b) => b.node_id));
  for (const m of state.nodeMarkers) {
    const hot = activeBreaches.has(m.node.name);
    m.pin.material.color.setHex(hot ? 0xff2b2b : m.baseColor);
    m.pin.scale.setScalar(hot ? 1.6 : 1.0);
  }

  // Overlay banners for breaches active on THIS frame.
  const overlay = document.getElementById('overlay');
  overlay.innerHTML = '';
  (fr.breaches || []).forEach((b) => {
    const div = document.createElement('div');
    div.className = 'breach-banner';
    div.innerHTML =
      `<span class="bb-tag">CRITICAL</span> Inundation threshold exceeded at ` +
      `<b>${b.node_id}</b> &nbsp;|&nbsp; Rate: <b>${b.inundation_rate_m3_s} m³/s</b> ` +
      `&nbsp;|&nbsp; head ${b.head_m} m &nbsp; (${Math.round(b.fraction_full * 100)}% full)`;
    overlay.appendChild(div);
  });
}

function seekToTime(t) {
  let best = 0, bd = Infinity;
  state.frames.forEach((f, i) => { const d = Math.abs(f.time_s - t); if (d < bd) { bd = d; best = i; } });
  setFrame(best);
}

// ---------- provenance + legend ----------

function showProvenance() {
  const m = state.manifest, p = m.provenance || {};
  document.getElementById('prov').innerHTML =
    `run_id <code>${m.run_id || '?'}</code> · ${p.terrain_source || '?'} · ` +
    `${m.grid.nx}×${m.grid.ny} @ ${m.grid.dx_m} m · ${m.grid.crs || 'CRS?'}<br>` +
    `rain ${p.storm?.rainfall_mm_per_hr ?? '?'} mm/hr for ${p.storm?.duration_hours ?? '?'} h · ` +
    `scheme ${p.solver_scheme || '?'} · Manning n ${p.manning_representative ?? '?'}`;
}

function renderLegend() {
  // Place each color at its actual shader stop position so the legend bar
  // matches where the danger bands really begin.
  const stops = (PALETTES[state.palette] || PALETTES[DEFAULT_PALETTE]).stops;
  const cssStops = stops.map((s) => `${s.hex} ${Math.round(s.pos * 100)}%`);
  document.getElementById('legendBar').style.background =
    `linear-gradient(90deg, ${cssStops.join(', ')})`;
  const hp = hazardParams();
  document.getElementById('legendCrit').textContent =
    `critical ≈ depth ≥ ${hp.dCrit} m or hazard ≥ ${hp.hrCrit}` +
    (state.hazard ? ' (from run manifest)' : ' (defaults)');
}

// ---------- loop + resize ----------

function onResize() {
  const wrap = document.getElementById('canvasWrap');
  const w = wrap.clientWidth, h = wrap.clientHeight;
  state.renderer.setSize(w, h, false);
  state.camera.aspect = w / Math.max(h, 1);
  state.camera.updateProjectionMatrix();
}

function tick(ts) {
  requestAnimationFrame(tick);
  if (state.playing && state.frames.length) {
    if (ts - state.lastStep > 1000 / state.fps) {
      state.lastStep = ts;
      let next = state.cur + 1;
      if (next >= state.frames.length) next = 0;
      setFrame(next);
    }
  }
  if (state.controls) state.controls.update();
  if (state.buildings && state.camera) state.buildings.update(state.camera);
  if (state.renderer) state.renderer.render(state.scene, state.camera);
}

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
  document.getElementById('status').style.display = msg ? 'block' : 'none';
}

// ---------- wiring ----------

function initUI() {
  // ?run= seeds the input once; from then on the input is authoritative.
  const q = new URLSearchParams(location.search).get('run');
  if (q) document.getElementById('runPath').value = q;

  // palette selector
  const sel = document.getElementById('paletteSel');
  Object.entries(PALETTES).forEach(([k, p]) => {
    const o = document.createElement('option');
    o.value = k; o.textContent = p.label;
    sel.appendChild(o);
  });
  sel.value = DEFAULT_PALETTE;
  sel.onchange = () => applyPalette(sel.value);

  document.getElementById('playBtn').onclick = () => {
    state.playing = !state.playing;
    document.getElementById('playBtn').textContent = state.playing ? '❚❚ Pause' : '▶ Play';
  };
  document.getElementById('scrubber').oninput = (e) => {
    state.playing = false;
    document.getElementById('playBtn').textContent = '▶ Play';
    setFrame(Number(e.target.value));
  };
  document.getElementById('speedSel').onchange = (e) => { state.fps = Number(e.target.value); };
  document.getElementById('loadBtn').onclick = loadRun;

  document.getElementById('bmSrc').dataset.key = 'nyc';   // default basemap source
  for (const id of Object.keys(LAYER_FNS)) {
    document.getElementById(id).onchange = (e) => LAYER_FNS[id](e.target.checked);
  }
  initPicking(document.getElementById('gl'));

  renderLegend();
  requestAnimationFrame(tick);
  loadRun();
}

initUI();

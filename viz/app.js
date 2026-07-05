// aqua-sim telemetry dashboard.
//
// The "brain" (Python solver) writes a run folder; this viewer is the thin
// telemetry layer. It loads the static terrain ONCE, then cycles the exported
// depth/speed frames, driving a custom kinetic shader and an alert matrix keyed
// to the timeline scrubber. No physics runs here.

import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { PALETTES, DEFAULT_PALETTE, THRESHOLDS, paletteUniforms } from './palettes.js';

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

    setStatus(`Loading ${manifest.frame_count} frames …`);
    const frames = await Promise.all(
      manifest.frames.map((f) => fetchJSON(base + f.file))
    );

    state.manifest = manifest;
    state.terrain = terrain;
    state.frames = frames;
    state.alerts = alerts;
    state.hazard = manifest.hazard || null;
    state.nx = terrain.nx; state.ny = terrain.ny; state.dx = terrain.dx;
    state.cur = 0;

    buildScene();
    buildAlertMatrix();
    showProvenance();
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
    state.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9);
    sun.position.set(1, 2, 1);
    state.scene.add(sun);
    window.addEventListener('resize', onResize);
  }
  // Clear any previous terrain/water on reload.
  for (const name of ['terrainMesh', 'waterMesh']) {
    const old = state.scene.getObjectByName(name);
    if (old) { state.scene.remove(old); old.geometry.dispose(); }
  }

  const W = (nx - 1) * dx, H = (ny - 1) * dx;

  // ----- terrain mesh (elevation + buildings, colored by height) -----
  const tGeom = gridGeometry(nx, ny, dx);
  const tPos = tGeom.attributes.position;
  const tCol = [];
  let zmin = Infinity, zmax = -Infinity;
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    if (terrain.mask && terrain.mask[j] && terrain.mask[j][i] === false) continue;
    const e = terrain.z[j][i] + (terrain.obstacle?.[j]?.[i] || 0);
    zmin = Math.min(zmin, e); zmax = Math.max(zmax, e);
  }
  // Vertical exaggeration derived from this run's relief: exaggerate flat
  // terrain so ponding reads, leave mountainous terrain near true scale.
  const relief = Math.max(zmax - zmin, 1e-6);
  state.vertExag = Math.min(12, Math.max(1, (0.025 * Math.max(W, H)) / relief));
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i;
    const masked = terrain.mask && terrain.mask[j] && terrain.mask[j][i] === false;
    const base = terrain.z[j][i];
    const obs = terrain.obstacle?.[j]?.[i] || 0;
    const e = base + obs;
    tPos.setY(k, e * state.vertExag);
    let col;
    if (masked) col = new THREE.Color(0x0a0d13);          // nodata / outside AOI: void
    else if (obs > 0) col = new THREE.Color(0x2b3550);    // buildings: slate
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

  // camera framing
  const span = Math.max(W, H);
  state.camera.position.set(0, span * 0.7, span * 0.9);
  state.camera.lookAt(0, 0, 0);
  state.controls.target.set(0, 0, 0);
  onResize();
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
      uniform float uDcrit, uHRcrit, uDebris, uMinDepth, uAlphaFull, uAlphaMin, uAlphaMax;
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
        float hazard = vDepth * (vSpeed + uDebris);   // engine's HR = d*(v+DF)
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

  renderLegend();
  requestAnimationFrame(tick);
  loadRun();
}

initUI();

// aqua-sim · HOLO TABLE
//
// A tactical-table rendering of a completed run. Reads the same run folder the
// telemetry dashboard reads (manifest.json, terrain.json, frame_NNN.json,
// alerts.json) and draws it as a volumetric display:
//
//   terrain   a dark holographic surface with a scan grid, plus elevation
//             contours extracted by marching squares and drawn as additive,
//             glowing lines that brighten toward the ridges
//   water     one GPU surface shared with the terrain geometry. Depth and speed
//             live in float textures; two consecutive frames are uploaded and
//             blended in the vertex shader, so playback is smooth between the
//             solver's output intervals. Colour is the engine's danger rating
//             (depth and depth·velocity against the thresholds the manifest
//             exports). Streaks advect down the water-surface gradient, which is
//             the direction the local-inertial momentum equation pushes water,
//             and brighten with speed. The critical-depth outline is a second
//             marching-squares contour, re-extracted whenever the frame changes.
//   sensors   each manifest node gets a vertical beam, pulsing ground rings and
//             a projected HTML card with live depth, hazard class and breach
//             status drawn from the alert log
//   bloom     a small custom post pass (bright-pass, separable blur at half
//             resolution, additive composite with a vignette). No external
//             post-processing modules are needed.
//
// Nothing here computes physics. Every number shown comes from the run folder.
//
// Query parameters:  ?run=../sample_run   folder, relative to this page
//                    &frame=22            start at frame index (1-based)
//                    &cam=1|2|3           camera preset
//                    &orbit=0|1           auto-orbit on/off
// Automation hook:   window.__holo = { setFrame, setCamera, setAutoRotate, play, pause, ready }

import * as THREE from 'three';
import { OrbitControls } from '../vendor/OrbitControls.js';

// ---------------------------------------------------------------- constants
const L = 100;                  // world units across the table's longer side
const MIN_DEPTH = 0.01;         // m — below this a cell is dry (matches dashboard)
const RELIEF_TARGET = 0.12;     // terrain relief as a fraction of L (vertical exaggeration target)
const EXAG_MAX = 120;
const CONTOUR_LEVELS = 18;
const MAX_CONTOUR_CELLS = 1_400_000;   // skip per-frame depth outline above this
const BASE_FPS = 1.5;           // frames per second at 1×
const COLORS = {
  cy: new THREE.Color('#3fe9ff'), cy2: new THREE.Color('#19a7b8'), am: new THREE.Color('#ffb347'),
  or: new THREE.Color('#ff7a2a'), hot: new THREE.Color('#ff3b3b'), dim: new THREE.Color('#0b3a46'),
};

const qs = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- data
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function flatten(rows, nx, ny) {
  const a = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    const row = rows[j];
    const o = j * nx;
    for (let i = 0; i < nx; i++) a[o + i] = row[i];
  }
  return a;
}

async function loadRun(base, onProgress) {
  let raw;
  if (window.__HOLO_RUN) {
    raw = window.__HOLO_RUN;                       // embedded by the artifact build
  } else {
    onProgress('reading manifest', 0.02);
    const manifest = await fetchJSON(base + 'manifest.json');
    onProgress('reading terrain', 0.06);
    const terrain = await fetchJSON(base + 'terrain.json');
    let alerts = [];
    try { alerts = await fetchJSON(base + (manifest.alerts_file || 'alerts.json')); } catch (e) { /* optional */ }
    const frames = [];
    const n = manifest.frames.length;
    for (let k = 0; k < n; k++) {
      onProgress(`reading frame ${k + 1} / ${n}`, 0.08 + 0.9 * (k / n));
      frames.push(await fetchJSON(base + manifest.frames[k].file));
    }
    raw = { manifest, terrain, alerts, frames };
  }
  const { manifest, terrain, alerts } = raw;
  const nx = terrain.nx, ny = terrain.ny, dx = terrain.dx;
  const z = flatten(terrain.z, nx, ny);
  const mask = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) mask[j * nx + i] = terrain.mask[j][i] ? 1 : 0;
  const obstacle = terrain.obstacle ? flatten(terrain.obstacle, nx, ny) : null;
  // typed frames; the nested JSON lists are dropped so memory scales with cells × frames × 8 bytes
  const frames = raw.frames.map((f) => {
    const depth = flatten(f.depth, nx, ny);
    const speed = f.speed ? flatten(f.speed, nx, ny) : new Float32Array(nx * ny);
    let wet = 0;
    for (let k = 0; k < depth.length; k++) if (depth[k] >= MIN_DEPTH && mask[k] > 0.5) wet++;
    return { index: f.index, t: f.time_s, depth, speed, maxDepth: f.max_depth, maxSpeed: f.max_speed,
             volume: f.total_volume_m3, breaches: f.breaches || [], wetCells: wet };
  });
  onProgress('building scene', 0.99);
  return { manifest, nx, ny, dx, z, mask, obstacle, frames, alerts };
}

// ---------------------------------------------------------------- marching squares
// Emits line segments of field == level. yOf(i, j) returns the height to draw at
// cell (i, j); it is interpolated along each crossing edge together with the
// field, so a depth contour rides the water surface and a terrain contour sits
// exactly on its level. Masked cells are skipped. Output is a Float32Array of
// [x,y,z, x,y,z, ...] in world units.
function marchingSquares(field, mask, nx, ny, level, yOf, toX, toZ) {
  const out = [];
  const edgeTable = [[], [[3, 0]], [[0, 1]], [[3, 1]], [[1, 2]], [[3, 0], [1, 2]], [[0, 2]], [[3, 2]],
                     [[2, 3]], [[2, 0]], [[0, 1], [2, 3]], [[2, 1]], [[1, 3]], [[1, 0]], [[0, 3]], []];
  // edge k: 0 = (i,j)-(i+1,j)  1 = (i+1,j)-(i+1,j+1)  2 = (i+1,j+1)-(i,j+1)  3 = (i,j+1)-(i,j)
  const ci = [0, 1, 1, 0], cj = [0, 0, 1, 1];
  for (let j = 0; j < ny - 1; j++) {
    for (let i = 0; i < nx - 1; i++) {
      const k00 = j * nx + i, k10 = k00 + 1, k11 = k00 + nx + 1, k01 = k00 + nx;
      if (mask[k00] < 0.5 || mask[k10] < 0.5 || mask[k11] < 0.5 || mask[k01] < 0.5) continue;
      const v = [field[k00], field[k10], field[k11], field[k01]];
      let idx = 0;
      if (v[0] >= level) idx |= 1; if (v[1] >= level) idx |= 2; if (v[2] >= level) idx |= 4; if (v[3] >= level) idx |= 8;
      if (idx === 0 || idx === 15) continue;
      let segs = edgeTable[idx];
      if (idx === 5 || idx === 10) {       // saddle: disambiguate by centre value
        const c = (v[0] + v[1] + v[2] + v[3]) * 0.25;
        if ((c >= level) !== (idx === 5)) segs = idx === 5 ? [[0, 1], [2, 3]] : [[3, 0], [1, 2]];
      }
      for (const [ea, eb] of segs) {
        for (const e of [ea, eb]) {
          const a = e, b = (e + 1) & 3;
          const va = v[a], vb = v[b];
          const t = Math.abs(vb - va) < 1e-9 ? 0.5 : (level - va) / (vb - va);
          const ia = i + ci[a], ja = j + cj[a], ib = i + ci[b], jb = j + cj[b];
          const x = toX(ia + (ib - ia) * t), zz = toZ(ja + (jb - ja) * t);
          const y = yOf(ia, ja) + (yOf(ib, jb) - yOf(ia, ja)) * t;
          out.push(x, y, zz);
        }
      }
    }
  }
  return new Float32Array(out);
}

// ---------------------------------------------------------------- shaders
const VERT = /* glsl */`
uniform sampler2D tTerrain;   // R: z - zRef (m), G: mask
uniform sampler2D tFrameA;    // R: depth (m), G: speed (m/s)
uniform sampler2D tFrameB;
uniform float uMix, uYScale, uMinDepth;
uniform ivec2 uDims;
out vec2 vUv; out float vMask; out float vZ; out float vDepth; out float vSpeed; out float vWet; out vec3 vWorld;
void main() {
  ivec2 ij = ivec2(int(uv.x * float(uDims.x)), int(uv.y * float(uDims.y)));
  vec2 tr = texelFetch(tTerrain, ij, 0).rg;
  vMask = tr.g; vZ = tr.r; vDepth = 0.0; vSpeed = 0.0; vWet = 0.0;
  float y = tr.r * uYScale;
  #ifdef WATER
    vec2 f = mix(texelFetch(tFrameA, ij, 0).rg, texelFetch(tFrameB, ij, 0).rg, uMix);
    vDepth = f.r; vSpeed = f.g;
    vWet = smoothstep(uMinDepth, uMinDepth * 4.0, f.r);
    y = (tr.r + max(f.r, 0.0)) * uYScale + 0.02;
  #endif
  vec4 wp = modelMatrix * vec4(position.x, y, position.z, 1.0);
  vWorld = wp.xyz; vUv = uv;
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;

const TERRAIN_FRAG = /* glsl */`
out vec4 fragColor;
uniform float uTime, uRelief, uGrid, uSweepX, uSweepW;
in vec2 vUv; in float vMask; in float vZ; in vec3 vWorld;
void main() {
  if (vMask < 0.5) discard;
  float zn = clamp(vZ / max(uRelief, 1e-3), 0.0, 1.0);
  vec3 lo = vec3(0.010, 0.040, 0.062), mid = vec3(0.018, 0.21, 0.29), hi = vec3(0.22, 0.92, 1.0);
  vec3 col = mix(lo, mid, smoothstep(0.0, 0.55, zn));
  col = mix(col, hi, smoothstep(0.45, 1.0, zn) * 0.9);
  vec3 n = normalize(cross(dFdx(vWorld), dFdy(vWorld)));
  float lit = clamp(dot(n, normalize(vec3(0.35, 1.0, 0.25))), 0.0, 1.0);
  col *= 0.5 + 0.75 * lit;
  vec2 gq = vWorld.xz / uGrid;
  vec2 g = abs(fract(gq - 0.5) - 0.5) / max(fwidth(gq), vec2(1e-4));
  float line = 1.0 - min(min(g.x, g.y), 1.0);
  col += vec3(0.10, 0.50, 0.60) * line * 0.38;
  float sweep = exp(-pow((vWorld.x - uSweepX) / uSweepW, 2.0));
  col += vec3(0.20, 0.90, 1.0) * sweep * 0.22;
  col *= 0.95 + 0.05 * sin(vWorld.z * 55.0 + uTime * 3.0);
  fragColor = vec4(col, 0.97);
}`;

const WATER_FRAG = /* glsl */`
out vec4 fragColor;
uniform sampler2D tTerrain, tFrameA, tFrameB;
uniform float uMix, uTime, uDCrit, uHRCrit, uDebris, uStreakFreq, uSweepX, uSweepW;
uniform ivec2 uDims;
in vec2 vUv; in float vMask; in float vZ; in float vDepth; in float vSpeed; in float vWet; in vec3 vWorld;
float eta(ivec2 ij) {
  ij = clamp(ij, ivec2(0), uDims - 1);
  float d = mix(texelFetch(tFrameA, ij, 0).r, texelFetch(tFrameB, ij, 0).r, uMix);
  return texelFetch(tTerrain, ij, 0).r + max(d, 0.0);
}
void main() {
  if (vMask < 0.5 || vWet <= 0.002) discard;
  ivec2 ij = ivec2(int(vUv.x * float(uDims.x)), int(vUv.y * float(uDims.y)));
  vec2 grad = vec2(eta(ij + ivec2(1, 0)) - eta(ij - ivec2(1, 0)), eta(ij + ivec2(0, 1)) - eta(ij - ivec2(0, 1)));
  vec2 dir = -normalize(grad + vec2(1e-6, 0.0));
  float depthN = clamp(vDepth / uDCrit, 0.0, 1.0);
  float danger = clamp(max(depthN, vDepth * (vSpeed + uDebris) / uHRCrit), 0.0, 1.0);
  vec3 c0 = vec3(0.05, 0.75, 0.95), c1 = vec3(0.55, 1.0, 1.0), c2 = vec3(1.0, 0.72, 0.30), c3 = vec3(1.0, 0.45, 0.15), c4 = vec3(1.0, 0.20, 0.22);
  vec3 col = danger < 0.30 ? mix(c0, c1, danger / 0.30)
           : danger < 0.60 ? mix(c1, c2, (danger - 0.30) / 0.30)
           : danger < 0.85 ? mix(c2, c3, (danger - 0.60) / 0.25)
           :                 mix(c3, c4, (danger - 0.85) / 0.15);
  float speedN = clamp(vSpeed / 1.5, 0.0, 1.0);
  float phase = dot(vWorld.xz, dir) * uStreakFreq - uTime * (0.7 + 2.2 * speedN);
  float streak = pow(fract(phase), 9.0);
  col += vec3(0.75, 1.0, 1.0) * streak * (0.12 + 0.88 * speedN) * 0.95;
  col += 0.05 * sin(vWorld.x * 2.3 + uTime * 1.7) * sin(vWorld.z * 2.1 - uTime * 1.3);
  vec3 V = normalize(cameraPosition - vWorld);
  float fres = pow(1.0 - clamp(V.y, 0.0, 1.0), 2.0);
  col += vec3(0.20, 0.60, 0.80) * fres * 0.30;
  float sweep = exp(-pow((vWorld.x - uSweepX) / uSweepW, 2.0));
  col += vec3(0.4, 1.0, 1.0) * sweep * 0.25;
  // thin rain sheets stay faint; only real depth glows, so terrain reads through
  float film = smoothstep(0.0, 0.35, depthN);
  col *= 0.28 + 0.72 * film;
  float alpha = mix(0.08, 0.90, pow(depthN, 0.75)) * vWet;
  fragColor = vec4(col, alpha);
}`;

const SLAB_FRAG = /* glsl */`
out vec4 fragColor;
uniform float uGrid, uTime; uniform vec2 uHalf;
in vec3 vWorld;
void main() {
  vec2 q = abs(vWorld.xz) / uHalf;
  float inside = 1.0 - smoothstep(0.98, 1.06, max(q.x, q.y));
  vec2 gq = vWorld.xz / uGrid;
  vec2 g = abs(fract(gq - 0.5) - 0.5) / max(fwidth(gq), vec2(1e-4));
  float line = 1.0 - min(min(g.x, g.y), 1.0);
  vec3 col = vec3(0.006, 0.020, 0.030) + vec3(0.05, 0.28, 0.36) * line * 0.5 * inside;
  float border = 1.0 - smoothstep(0.0, 0.012, abs(max(q.x, q.y) - 1.0));
  col += vec3(0.25, 0.9, 1.0) * border * 0.9;
  fragColor = vec4(col, 0.92);
}`;
const SLAB_VERT = /* glsl */`
out vec3 vWorld;
void main() { vec4 wp = modelMatrix * vec4(position, 1.0); vWorld = wp.xyz; gl_Position = projectionMatrix * viewMatrix * wp; }`;

// ---------------------------------------------------------------- bloom (custom, no addons)
class Bloom {
  constructor(renderer, w, h) {
    this.renderer = renderer;
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2));
    this.scene = new THREE.Scene(); this.scene.add(this.quad);
    this.cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const type = THREE.HalfFloatType;
    this.rtScene = new THREE.WebGLRenderTarget(w, h, { type, depthBuffer: true });
    this.rtA = new THREE.WebGLRenderTarget(w >> 1, h >> 1, { type });
    this.rtB = new THREE.WebGLRenderTarget(w >> 1, h >> 1, { type });
    const fsVert = `out vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }`;
    const mk = (frag, uniforms) => new THREE.ShaderMaterial({ glslVersion: THREE.GLSL3, vertexShader: fsVert, fragmentShader: frag, uniforms, depthTest: false, depthWrite: false });
    this.bright = mk(`out vec4 fragColor; uniform sampler2D tex; uniform float uThr; in vec2 vUv;
      void main(){ vec3 c = texture(tex, vUv).rgb; float l = max(max(c.r,c.g),c.b); float k = smoothstep(uThr, uThr + 0.35, l); fragColor = vec4(c * k, 1.0); }`,
      { tex: { value: null }, uThr: { value: 0.62 } });
    this.blur = mk(`out vec4 fragColor; uniform sampler2D tex; uniform vec2 uDir; in vec2 vUv;
      void main(){ vec2 px = uDir / vec2(textureSize(tex, 0));
        float w[5] = float[5](0.227, 0.194, 0.121, 0.054, 0.016);
        vec3 c = texture(tex, vUv).rgb * w[0];
        for (int i = 1; i < 5; i++) { c += texture(tex, vUv + px * float(i) * 1.5).rgb * w[i]; c += texture(tex, vUv - px * float(i) * 1.5).rgb * w[i]; }
        fragColor = vec4(c, 1.0); }`,
      { tex: { value: null }, uDir: { value: new THREE.Vector2(1, 0) } });
    this.comp = mk(`out vec4 fragColor; uniform sampler2D tScene, tBloom; uniform float uStrength; in vec2 vUv;
      void main(){ vec3 c = texture(tScene, vUv).rgb + texture(tBloom, vUv).rgb * uStrength;
        float v = 1.0 - 0.42 * smoothstep(0.45, 1.15, length(vUv - 0.5) * 1.45);
        c = c * v; c = c / (1.0 + c * 0.12);
        fragColor = vec4(c, 1.0); }`,
      { tScene: { value: null }, tBloom: { value: null }, uStrength: { value: 1.35 } });
  }
  setSize(w, h) { this.rtScene.setSize(w, h); this.rtA.setSize(Math.max(1, w >> 1), Math.max(1, h >> 1)); this.rtB.setSize(Math.max(1, w >> 1), Math.max(1, h >> 1)); }
  render(scene, camera) {
    const r = this.renderer;
    r.setRenderTarget(this.rtScene); r.clear(); r.render(scene, camera);
    this.quad.material = this.bright; this.bright.uniforms.tex.value = this.rtScene.texture;
    r.setRenderTarget(this.rtA); r.render(this.scene, this.cam);
    for (let i = 0; i < 2; i++) {
      this.quad.material = this.blur; this.blur.uniforms.tex.value = this.rtA.texture; this.blur.uniforms.uDir.value.set(1, 0);
      r.setRenderTarget(this.rtB); r.render(this.scene, this.cam);
      this.blur.uniforms.tex.value = this.rtB.texture; this.blur.uniforms.uDir.value.set(0, 1);
      r.setRenderTarget(this.rtA); r.render(this.scene, this.cam);
    }
    this.quad.material = this.comp; this.comp.uniforms.tScene.value = this.rtScene.texture; this.comp.uniforms.tBloom.value = this.rtA.texture;
    r.setRenderTarget(null); r.render(this.scene, this.cam);
  }
}

// ---------------------------------------------------------------- app
const S = {
  run: null, renderer: null, scene: null, camera: null, controls: null, bloom: null,
  geo: null, terrainMat: null, waterMat: null, slabMat: null, contourLines: null, outline: null,
  frameTex: new Map(), texOrder: [],
  pos: 0, playing: false, rate: 1, autoRotate: true, lastInteract: 0, lastIntFrame: -1,
  nodes: [], cards: [], alertEls: [], nodeEls: [],
  world: { scale: 1, yScale: 1, zRef: 0, relief: 1, halfX: 1, halfZ: 1 },
  clock: new THREE.Clock(), ready: false,
};

function setStatus(text, err = false) { $('status-text').textContent = text; $('status').classList.toggle('err', err); }
function fmtTime(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), q = s % 60; return `T+${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(q).padStart(2, '0')}`; }
function fmtNum(v, d = 2) { return Number.isFinite(v) ? v.toFixed(d) : '—'; }
function fmtBig(v) { return v >= 1e9 ? (v / 1e9).toFixed(2) + 'G' : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v.toFixed(0); }

function buildScene(run) {
  const { nx, ny, dx, z, mask } = run;
  const cells = Math.max(nx, ny);
  const scale = L / cells;                          // world units per cell
  const wpm = scale / dx;                           // world units per metre (horizontal)
  let zmin = Infinity, zmax = -Infinity;
  for (let k = 0; k < z.length; k++) if (mask[k] > 0.5) { if (z[k] < zmin) zmin = z[k]; if (z[k] > zmax) zmax = z[k]; }
  if (!Number.isFinite(zmin)) { zmin = 0; zmax = 1; }
  const relief = Math.max(zmax - zmin, 0.5);
  const exag = Math.min(EXAG_MAX, Math.max(1, RELIEF_TARGET * L / (relief * wpm)));
  const yScale = wpm * exag;
  Object.assign(S.world, { scale, yScale, zRef: zmin, relief, halfX: nx * scale / 2, halfZ: ny * scale / 2, exag, wpm });
  const toX = (i) => (i + 0.5 - nx / 2) * scale;
  const toZ = (j) => (j + 0.5 - ny / 2) * scale;
  S.toX = toX; S.toZ = toZ;
  S.yOfElev = (e) => (e - zmin) * yScale;

  const renderer = new THREE.WebGLRenderer({ canvas: $('stage'), antialias: true, alpha: false, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);
  renderer.setClearColor(0x05080c, 1);
  renderer.autoClear = true;
  S.renderer = renderer;
  const scene = new THREE.Scene(); S.scene = scene;
  const camera = new THREE.PerspectiveCamera(42, innerWidth / innerHeight, 0.1, L * 12); S.camera = camera;
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.06; controls.minDistance = L * 0.12; controls.maxDistance = L * 3.2;
  controls.maxPolarAngle = Math.PI * 0.49; controls.autoRotateSpeed = 0.35;
  controls.addEventListener('start', () => { S.lastInteract = performance.now(); });
  S.controls = controls;

  // --- textures
  const terr = new Float32Array(nx * ny * 2);
  for (let k = 0; k < nx * ny; k++) { terr[2 * k] = z[k] - zmin; terr[2 * k + 1] = mask[k]; }
  const tTerrain = new THREE.DataTexture(terr, nx, ny, THREE.RGFormat, THREE.FloatType);
  tTerrain.magFilter = tTerrain.minFilter = THREE.NearestFilter; tTerrain.generateMipmaps = false; tTerrain.needsUpdate = true;
  S.tTerrain = tTerrain;

  // --- shared geometry: one vertex per cell, heights come from the textures
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(nx * ny * 3), uv = new Float32Array(nx * ny * 2);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i;
    pos[3 * k] = toX(i); pos[3 * k + 1] = 0; pos[3 * k + 2] = toZ(j);
    uv[2 * k] = (i + 0.5) / nx; uv[2 * k + 1] = (j + 0.5) / ny;
  }
  const idx = new Uint32Array((nx - 1) * (ny - 1) * 6); let q = 0;
  for (let j = 0; j < ny - 1; j++) for (let i = 0; i < nx - 1; i++) {
    const a = j * nx + i, b = a + 1, c = a + nx, d = c + 1;
    idx[q++] = a; idx[q++] = c; idx[q++] = b; idx[q++] = b; idx[q++] = c; idx[q++] = d;
  }
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, relief * yScale / 2, 0), L);
  S.geo = geo;

  const hz = run.manifest.hazard || {};
  const common = {
    tTerrain: { value: tTerrain }, tFrameA: { value: null }, tFrameB: { value: null },
    uMix: { value: 0 }, uYScale: { value: yScale }, uMinDepth: { value: MIN_DEPTH }, uDims: { value: new THREE.Vector2(nx, ny) },
    uTime: { value: 0 }, uSweepX: { value: -L }, uSweepW: { value: L * 0.03 },
  };
  // uDims must be an ivec2: pass an object Three uploads with uniform2iv
  common.uDims = { value: [nx, ny], type: 'iv2' };

  S.terrainMat = new THREE.ShaderMaterial({
    glslVersion: THREE.GLSL3, vertexShader: VERT, fragmentShader: TERRAIN_FRAG,
    uniforms: { ...common, uRelief: { value: relief }, uGrid: { value: niceGrid(scale, dx) } },
    transparent: true, side: THREE.DoubleSide,
  });
  S.waterMat = new THREE.ShaderMaterial({
    glslVersion: THREE.GLSL3, vertexShader: VERT, fragmentShader: WATER_FRAG, defines: { WATER: 1 },
    uniforms: { ...common, uDCrit: { value: hz.depth_critical_m ?? 0.5 }, uHRCrit: { value: hz.hr_bands?.moderate ?? 1.25 },
                uDebris: { value: hz.debris_factor ?? 0.5 }, uStreakFreq: { value: 0.9 } },
    transparent: true, depthWrite: false, side: THREE.DoubleSide,
  });
  const terrainMesh = new THREE.Mesh(geo, S.terrainMat); terrainMesh.renderOrder = 0; terrainMesh.frustumCulled = false; scene.add(terrainMesh);
  const waterMesh = new THREE.Mesh(geo, S.waterMat); waterMesh.renderOrder = 3; waterMesh.frustumCulled = false; scene.add(waterMesh);

  // --- table slab under the terrain
  const slab = new THREE.Mesh(new THREE.PlaneGeometry(nx * scale * 1.12, ny * scale * 1.12),
    (S.slabMat = new THREE.ShaderMaterial({ glslVersion: THREE.GLSL3, vertexShader: SLAB_VERT, fragmentShader: SLAB_FRAG,
      uniforms: { uGrid: { value: niceGrid(scale, dx) }, uTime: { value: 0 }, uHalf: { value: new THREE.Vector2(nx * scale / 2, ny * scale / 2) } },
      transparent: true })));
  slab.rotation.x = -Math.PI / 2; slab.position.y = -0.9; slab.renderOrder = -1; scene.add(slab);
  // front-edge distance ticks
  const ticks = []; const step = niceGrid(scale, dx);
  for (let x = -S.world.halfX; x <= S.world.halfX + 1e-6; x += step) ticks.push(x, -0.85, S.world.halfZ + 0.6, x, -0.85, S.world.halfZ + 1.8);
  scene.add(new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(ticks, 3)),
    new THREE.LineBasicMaterial({ color: COLORS.cy2, transparent: true, opacity: 0.8 })));

  // --- elevation contours
  setStatus('extracting contours');
  const cpos = [], ccol = [];
  const levels = CONTOUR_LEVELS;
  for (let n = 1; n < levels; n++) {
    const lv = zmin + relief * n / levels;
    const seg = marchingSquares(z, mask, nx, ny, lv, () => (lv - zmin) * yScale + 0.03, toX, toZ);
    const tn = n / levels, major = n % 5 === 0;
    const c = new THREE.Color().copy(COLORS.dim).lerp(COLORS.cy, Math.pow(tn, 1.3)).multiplyScalar(major ? 1.6 : 1.0);
    for (let k = 0; k < seg.length; k += 3) { cpos.push(seg[k], seg[k + 1], seg[k + 2]); ccol.push(c.r, c.g, c.b); }
  }
  const cg = new THREE.BufferGeometry();
  cg.setAttribute('position', new THREE.Float32BufferAttribute(cpos, 3));
  cg.setAttribute('color', new THREE.Float32BufferAttribute(ccol, 3));
  S.contourLines = new THREE.LineSegments(cg, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending, depthWrite: false }));
  S.contourLines.renderOrder = 1; scene.add(S.contourLines);

  // --- critical-depth outline (rebuilt per frame)
  S.outline = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: COLORS.or, transparent: true, opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false }));
  S.outline.renderOrder = 4; S.outline.frustumCulled = false; scene.add(S.outline);

  // --- obstacles (buildings) as glowing prisms, if any
  if (run.obstacle) {
    const cellsWith = []; for (let k = 0; k < run.obstacle.length; k++) if (run.obstacle[k] > 0 && mask[k] > 0.5) cellsWith.push(k);
    if (cellsWith.length && cellsWith.length <= 80000) {
      const box = new THREE.BoxGeometry(scale * 0.9, 1, scale * 0.9);
      const inst = new THREE.InstancedMesh(box, new THREE.MeshBasicMaterial({ color: COLORS.cy, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false }), cellsWith.length);
      const m = new THREE.Matrix4();
      cellsWith.forEach((k, n) => { const i = k % nx, j = (k - i) / nx; const h = Math.max(run.obstacle[k] * yScale, 0.05); m.makeScale(1, h, 1); m.setPosition(toX(i), (z[k] - zmin) * yScale + h / 2, toZ(j)); inst.setMatrixAt(n, m); });
      inst.renderOrder = 2; scene.add(inst);
    }
  }

  // --- sensors
  buildNodes(run);

  // --- bloom + camera
  S.bloom = new Bloom(renderer, Math.floor(innerWidth * renderer.getPixelRatio()), Math.floor(innerHeight * renderer.getPixelRatio()));
  setCamera(Number(qs.get('cam')) || 1);
  window.addEventListener('resize', onResize);
}

function niceGrid(scale, dx) {
  // grid spacing in world units chosen so lines sit every ~1, 2, 5 × 10^n metres and ~10–16 lines span the table
  const metresAcross = L / scale * dx;
  const target = metresAcross / 12;
  const p = Math.pow(10, Math.floor(Math.log10(target)));
  const m = [1, 2, 5, 10].find((f) => f * p >= target) || 10;
  return m * p * scale / dx;
}

function buildNodes(run) {
  const { nx, ny, z, mask } = run; const { yScale, zRef } = S.world;
  const breachTime = new Map();
  for (const a of run.alerts || []) if (a.severity === 'CRITICAL' && a.location) { const t = breachTime.get(a.location); if (t === undefined || a.time_s < t) breachTime.set(a.location, a.time_s); }
  for (const f of run.frames) for (const b of f.breaches || []) { const nm = b.node || b.name || b.location; if (nm && !breachTime.has(nm)) breachTime.set(nm, f.t); }
  const cardsRoot = $('cards'), list = $('nd-list');
  const beamH = L * 0.075;
  for (const nd of run.manifest.nodes || []) {
    const i = Math.min(nx - 1, Math.max(0, nd.x | 0)), j = Math.min(ny - 1, Math.max(0, nd.y | 0)), k = j * nx + i;
    const gy = (z[k] - zRef) * yScale;
    const x = S.toX(i), zz = S.toZ(j);
    const group = new THREE.Group(); group.position.set(x, gy, zz);
    const beam = new THREE.Line(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0, 0, beamH, 0], 3)),
      new THREE.LineBasicMaterial({ color: COLORS.cy, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false }));
    group.add(beam);
    const rings = [];
    for (let r = 0; r < 3; r++) {
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.0, 48), new THREE.MeshBasicMaterial({ color: COLORS.cy, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.06; group.add(ring); rings.push(ring);
    }
    group.renderOrder = 5; S.scene.add(group);
    const card = document.createElement('div'); card.className = 'card';
    card.innerHTML = `<div class="nm"><span></span><i>DRY</i></div><div class="row"><span>depth</span><b class="d">0.00 m</b></div><div class="row"><span>hazard</span><b class="h">—</b></div><div class="row"><span>breach</span><b class="b">—</b></div>`;
    card.querySelector('.nm span').textContent = nd.name;
    card.addEventListener('click', () => focusNode(group.position));
    cardsRoot.appendChild(card);
    const li = document.createElement('li'); li.innerHTML = `<span class="nm"></span><span class="st">dry</span><span class="dp">—</span>`;
    li.querySelector('.nm').textContent = nd.name; li.addEventListener('click', () => focusNode(group.position)); list.appendChild(li);
    S.nodes.push({ nd, i, j, k, group, beam, rings, card, li, breachAt: breachTime.get(nd.name), worldY: gy, beamH });
  }
  $('nd-count').textContent = String(S.nodes.length);
}

function focusNode(p) {
  const c = S.controls; c.target.copy(p);
  const d = L * 0.35; S.camera.position.set(p.x + d * 0.45, p.y + d * 0.55, p.z + d * 0.75); c.update();
  S.lastInteract = performance.now();
}

function setCamera(n) {
  const c = S.controls, h = S.world.relief * S.world.yScale;
  c.target.set(0, h * 0.25, 0);
  if (n === 2) S.camera.position.set(L * 0.35, L * 0.22, L * 0.62);
  else if (n === 3) S.camera.position.set(0, L * 1.3, 0.02);   // straight down, north at the top
  else S.camera.position.set(L * 0.55, L * 0.5, L * 0.78);
  c.update(); $('cam-mode').textContent = ['', 'overview', 'oblique', 'plan'][n] || 'orbit';
}

function onResize() {
  S.camera.aspect = innerWidth / innerHeight; S.camera.updateProjectionMatrix();
  S.renderer.setSize(innerWidth, innerHeight);
  const pr = S.renderer.getPixelRatio(); S.bloom.setSize(Math.floor(innerWidth * pr), Math.floor(innerHeight * pr));
}

// ---------------------------------------------------------------- frames
function frameTexture(k) {
  if (S.frameTex.has(k)) return S.frameTex.get(k);
  const f = S.run.frames[k], n = f.depth.length;
  const buf = new Float32Array(n * 2);
  for (let i = 0; i < n; i++) { buf[2 * i] = f.depth[i]; buf[2 * i + 1] = f.speed[i]; }
  const tex = new THREE.DataTexture(buf, S.run.nx, S.run.ny, THREE.RGFormat, THREE.FloatType);
  tex.magFilter = tex.minFilter = THREE.NearestFilter; tex.generateMipmaps = false; tex.needsUpdate = true;
  S.frameTex.set(k, tex); S.texOrder.push(k);
  while (S.texOrder.length > 10) { const old = S.texOrder.shift(); if (old !== k) { S.frameTex.get(old)?.dispose(); S.frameTex.delete(old); } }
  return tex;
}

function applyPos() {
  const n = S.run.frames.length;
  S.pos = Math.max(0, Math.min(n - 1, S.pos));
  const a = Math.floor(S.pos), b = Math.min(n - 1, a + 1), mix = S.pos - a;
  for (const m of [S.terrainMat, S.waterMat]) { m.uniforms.tFrameA.value = frameTexture(a); m.uniforms.tFrameB.value = frameTexture(b); m.uniforms.uMix.value = mix; }
  const fa = S.run.frames[a], fb = S.run.frames[b];
  const lerp = (x, y) => x + (y - x) * mix;
  const t = lerp(fa.t, fb.t);
  $('tb-time').textContent = fmtTime(t);
  $('tb-frame').textContent = `frame ${a + 1} / ${n}`;
  $('tele-frame').textContent = `f ${String(a + 1).padStart(3, '0')}`;
  $('v-depth').textContent = fmtNum(lerp(fa.maxDepth, fb.maxDepth));
  $('v-speed').textContent = fmtNum(lerp(fa.maxSpeed, fb.maxSpeed));
  $('v-vol').textContent = fmtBig(lerp(fa.volume, fb.volume));
  $('v-wet').textContent = fmtNum(lerp(fa.wetCells, fb.wetCells) * S.run.dx * S.run.dx / 1e6, 2);
  $('scrub').value = String(S.pos); $('scrub').style.setProperty('--p', `${(S.pos / Math.max(1, n - 1)) * 100}%`);
  drawSparks();
  updateAlerts(t);
  updateNodes(a, b, mix, t);
  if (a !== S.lastIntFrame) { S.lastIntFrame = a; rebuildOutline(a); drawMinimap(a); }
}

function rebuildOutline(k) {
  const run = S.run; const f = run.frames[k];
  if (run.nx * run.ny > MAX_CONTOUR_CELLS) return;
  const dcrit = S.waterMat.uniforms.uDCrit.value;
  const { yScale, zRef } = S.world;
  const seg = marchingSquares(f.depth, run.mask, run.nx, run.ny, dcrit,
    (i, j) => (run.z[j * run.nx + i] - zRef + Math.max(f.depth[j * run.nx + i], 0)) * yScale + 0.06, S.toX, S.toZ);
  S.outline.geometry.dispose();
  S.outline.geometry = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(seg, 3));
}

// ---------------------------------------------------------------- HUD
function drawSparks() {
  const fr = S.run.frames, n = fr.length, p = S.pos / Math.max(1, n - 1);
  const series = [['s-depth', fr.map((f) => f.maxDepth), '#3fe9ff'], ['s-speed', fr.map((f) => f.maxSpeed), '#3fe9ff'],
                  ['s-vol', fr.map((f) => f.volume), '#3fe9ff'], ['s-wet', fr.map((f) => f.wetCells), '#ffb347']];
  for (const [id, ys, col] of series) {
    const cv = $(id), ctx = cv.getContext('2d'), W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    const mx = Math.max(1e-9, ...ys);
    ctx.beginPath();
    ys.forEach((y, i) => { const x = (i / Math.max(1, n - 1)) * (W - 2) + 1, yy = H - 2 - (y / mx) * (H - 6); i ? ctx.lineTo(x, yy) : ctx.moveTo(x, yy); });
    ctx.strokeStyle = col; ctx.lineWidth = 1.2; ctx.globalAlpha = 0.9; ctx.stroke();
    ctx.lineTo((W - 2) + 1, H); ctx.lineTo(1, H); ctx.closePath(); ctx.fillStyle = col; ctx.globalAlpha = 0.10; ctx.fill();
    ctx.globalAlpha = 1; ctx.fillStyle = '#ffffff'; ctx.fillRect(p * (W - 2) + 1, 0, 1, H);
  }
}

function updateAlerts(t) {
  let live = 0;
  for (const { el, a } of S.alertEls) { const on = a.time_s <= t; el.classList.toggle('live', on); if (on) live++; }
  $('al-count').textContent = `${live} / ${S.alertEls.length}`;
}

const _v = new THREE.Vector3();
function updateNodes(a, b, mix, t) {
  const fa = S.run.frames[a], fb = S.run.frames[b]; const hz = S.run.manifest.hazard || {};
  const bands = hz.hr_bands || { low: 0.75, moderate: 1.25, significant: 2.0 }, DF = hz.debris_factor ?? 0.5, dcrit = hz.depth_critical_m ?? 0.5;
  for (const nd of S.nodes) {
    const d = fa.depth[nd.k] + (fb.depth[nd.k] - fa.depth[nd.k]) * mix;
    const v = fa.speed[nd.k] + (fb.speed[nd.k] - fa.speed[nd.k]) * mix;
    const hr = d * (v + DF);
    const cls = d < MIN_DEPTH ? '—' : hr < bands.low ? 'LOW' : hr < bands.moderate ? 'MODERATE' : hr < bands.significant ? 'SIGNIFICANT' : 'EXTREME';
    const breached = nd.breachAt !== undefined && t >= nd.breachAt;
    const state = breached ? 'breached' : d >= dcrit * 0.5 ? 'watch' : 'dry';
    nd.card.className = `card ${state}`;
    nd.card.querySelector('.nm i').textContent = state === 'breached' ? 'BREACH' : state === 'watch' ? 'WATCH' : d >= MIN_DEPTH ? 'WET' : 'DRY';
    nd.card.querySelector('.d').textContent = `${fmtNum(d)} m`;
    nd.card.querySelector('.h').textContent = cls;
    nd.card.querySelector('.b').textContent = nd.breachAt === undefined ? 'none' : breached ? `at ${fmtTime(nd.breachAt)}` : `in ${fmtTime(nd.breachAt - t).slice(2)}`;
    nd.li.className = state; nd.li.querySelector('.st').textContent = state; nd.li.querySelector('.dp').textContent = `${fmtNum(d)} m · HR ${fmtNum(hr)} · ${cls}`;
    const col = state === 'breached' ? COLORS.or : state === 'watch' ? COLORS.am : COLORS.cy;
    nd.beam.material.color.copy(col); nd.rings.forEach((r) => r.material.color.copy(col));
    // project the beam tip; cards are placed afterwards so overlapping ones can be lifted
    _v.set(nd.group.position.x, nd.group.position.y + nd.beamH, nd.group.position.z).project(S.camera);
    nd.behind = _v.z > 1 || _v.z < -1;
    nd.sx = (_v.x * 0.5 + 0.5) * innerWidth; nd.sy = (-_v.y * 0.5 + 0.5) * innerHeight;
    nd.card.classList.toggle('hidden', nd.behind);
  }
  placeCards();
}

// Cards anchor to their beam tip with a leader line. When two would overlap, the
// later one (left to right) is lifted to sit just above the one it hit and its
// leader lengthens to match, so every sensor stays labelled and readable.
function placeCards() {
  const placed = [];
  const order = S.nodes.filter((n) => !n.behind).sort((a, b) => a.sx - b.sx);
  for (const nd of order) {
    const w = nd.card.offsetWidth || 150, h = nd.card.offsetHeight || 70;
    let lift = 22, guard = 0, rect;
    while (guard++ < 40) {
      rect = { l: nd.sx - w / 2, r: nd.sx + w / 2, t: nd.sy - lift - h, b: nd.sy - lift };
      const hit = placed.find((p) => !(rect.r < p.l - 6 || rect.l > p.r + 6 || rect.b < p.t - 6 || rect.t > p.b + 6));
      if (!hit) break;
      lift = nd.sy - hit.t + 10;
    }
    placed.push(rect);
    nd.card.style.left = `${nd.sx}px`; nd.card.style.top = `${nd.sy}px`;
    nd.card.style.setProperty('--lift', `${Math.round(lift)}px`);
  }
}

function drawMinimap(k) {
  const run = S.run, f = run.frames[k], cv = $('mm');
  const W = Math.min(220, run.nx), H = Math.max(1, Math.round(W * run.ny / run.nx));
  cv.width = W; cv.height = H;
  const img = cv.getContext('2d').createImageData(W, H), px = img.data;
  const dcrit = S.waterMat.uniforms.uDCrit.value, DF = S.waterMat.uniforms.uDebris.value, hrc = S.waterMat.uniforms.uHRCrit.value;
  const { zRef, relief } = S.world;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = Math.floor((x + 0.5) / W * run.nx), j = Math.floor((y + 0.5) / H * run.ny), kk = j * run.nx + i, o = 4 * (y * W + x);
    if (run.mask[kk] < 0.5) { px[o + 3] = 0; continue; }
    const zn = (run.z[kk] - zRef) / relief, d = f.depth[kk];
    let r = 8 + 30 * zn, g = 30 + 120 * zn, b = 40 + 140 * zn;
    if (d >= MIN_DEPTH) { const dn = Math.min(1, Math.max(d / dcrit, d * (f.speed[kk] + DF) / hrc)); r = 40 + 215 * dn; g = 220 - 120 * dn; b = 255 - 200 * dn; }
    px[o] = r; px[o + 1] = g; px[o + 2] = b; px[o + 3] = 235;
  }
  cv.getContext('2d').putImageData(img, 0, 0);
  const ctx = cv.getContext('2d'); ctx.fillStyle = '#ffb347';
  for (const nd of S.nodes) ctx.fillRect(Math.round(nd.i / run.nx * W) - 1, Math.round(nd.j / run.ny * H) - 1, 3, 3);
  $('mm-lab').textContent = `f ${String(k + 1).padStart(3, '0')}`;
}

function fillRunInfo(run) {
  const m = run.manifest, p = m.provenance || {};
  $('ri-run').textContent = m.run_id || '—'; $('ri-aoi').textContent = m.aoi || '—';
  $('ri-grid').textContent = `${run.nx} × ${run.ny} @ ${run.dx} m`; $('ri-crs').textContent = m.grid?.crs || p.crs || '—';
  $('ri-scheme').textContent = p.solver_scheme || '—'; $('ri-terrain').textContent = (p.terrain_source || '—') + (p.terrain_digest ? ` · ${p.terrain_digest}` : '');
  const st = p.storm || {}; $('ri-storm').textContent = st.rainfall_mm_per_hr !== undefined ? `${st.rainfall_mm_per_hr} mm/h × ${st.duration_hours} h` : '—';
  $('ri-frames').textContent = `${m.frame_count} · Δt ${(m.time_axis_s?.[1] ?? 0) - (m.time_axis_s?.[0] ?? 0)} s`;
  const hz = m.hazard || {}; $('lg-crit').textContent = `${hz.depth_critical_m ?? 0.5} crit`; $('lg-caution').textContent = `${((hz.depth_critical_m ?? 0.5) * 0.3).toFixed(2)}`;
  const bar = Math.round(niceGrid(S.world.scale, run.dx) / S.world.scale * run.dx);
  $('scale-lab').textContent = bar >= 1000 ? `${+(bar / 1000).toFixed(2)} km` : `${bar} m`;
  const list = $('al-list'); const alerts = [...(run.alerts || [])].sort((x, y) => x.time_s - y.time_s);
  for (const a of alerts) {
    const el = document.createElement('li'); el.className = a.severity || 'INFO';
    el.innerHTML = `<span class="t"></span><span class="sev"></span><span class="m"></span>`;
    el.querySelector('.t').textContent = fmtTime(a.time_s); el.querySelector('.sev').textContent = a.severity || ''; el.querySelector('.m').textContent = a.message || a.location || '';
    el.addEventListener('click', () => { const i = run.frames.findIndex((f) => f.t >= a.time_s); S.pos = Math.max(0, i); applyPos(); });
    list.appendChild(el); S.alertEls.push({ el, a });
  }
  $('al-count').textContent = `0 / ${alerts.length}`;
}

// ---------------------------------------------------------------- loop
function tick() {
  requestAnimationFrame(tick);
  const dt = Math.min(0.1, S.clock.getDelta());
  const now = performance.now();
  const time = S.clock.elapsedTime;
  if (S.playing) { S.pos += dt * BASE_FPS * S.rate; if (S.pos >= S.run.frames.length - 1) S.pos = 0; applyPos(); }
  const idle = now - S.lastInteract > 8000;
  S.controls.autoRotate = S.autoRotate && idle;
  S.controls.update();
  const sweepX = -L * 0.7 + ((time * 0.11) % 1) * L * 1.4;
  for (const m of [S.terrainMat, S.waterMat]) { m.uniforms.uTime.value = time; m.uniforms.uSweepX.value = sweepX; }
  S.slabMat.uniforms.uTime.value = time;
  for (const nd of S.nodes) nd.rings.forEach((r, i) => { const ph = (time * 0.6 + i / 3) % 1; const s = 0.6 + ph * (L * 0.03); r.scale.set(s, s, 1); r.material.opacity = 0.75 * (1 - ph); });
  if (!S.playing) updateNodes(Math.floor(S.pos), Math.min(S.run.frames.length - 1, Math.floor(S.pos) + 1), S.pos - Math.floor(S.pos), S.run.frames[Math.floor(S.pos)].t);
  S.bloom.render(S.scene, S.camera);
  if (!S.ready) { S.ready = true; window.__holo.ready = true; }
}

function wireUI() {
  const play = (on) => { S.playing = on; $('btn-play').textContent = on ? '❚❚ pause' : '▶ play'; $('btn-play').classList.toggle('on', on); };
  $('btn-play').addEventListener('click', () => play(!S.playing));
  $('speed').addEventListener('change', (e) => { S.rate = Number(e.target.value); });
  $('scrub').max = String(S.run.frames.length - 1);
  $('scrub').addEventListener('input', (e) => { play(false); S.pos = Number(e.target.value); applyPos(); });
  document.querySelectorAll('[data-cam]').forEach((b) => b.addEventListener('click', () => { setCamera(Number(b.dataset.cam)); S.lastInteract = performance.now(); }));
  const orbitBtn = $('btn-orbit'); const setOrbit = (on) => { S.autoRotate = on; orbitBtn.classList.toggle('on', on); };
  orbitBtn.addEventListener('click', () => setOrbit(!S.autoRotate)); setOrbit(qs.get('orbit') !== '0');
  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.code === 'Space') { e.preventDefault(); play(!S.playing); }
    else if (e.key === 'ArrowRight') { play(false); S.pos = Math.min(S.run.frames.length - 1, Math.floor(S.pos) + 1); applyPos(); }
    else if (e.key === 'ArrowLeft') { play(false); S.pos = Math.max(0, Math.ceil(S.pos) - 1); applyPos(); }
    else if (e.key === '1' || e.key === '2' || e.key === '3') setCamera(Number(e.key));
    else if (e.key.toLowerCase() === 'o') setOrbit(!S.autoRotate);
    else if (e.key.toLowerCase() === 'r') setCamera(1);
  });
  window.__holo = {
    ready: false,
    setFrame: (f) => { play(false); S.pos = Math.max(0, Math.min(S.run.frames.length - 1, f - 1)); applyPos(); },
    setCamera, setAutoRotate: setOrbit, play: () => play(true), pause: () => play(false), state: S,
  };
}

async function main() {
  const base = (qs.get('run') || '../sample_run').replace(/\/?$/, '/');
  const msg = $('load-msg'), bar = $('load-bar');
  try {
    const run = await loadRun(base, (m, p) => { msg.textContent = m; bar.style.width = `${Math.round(p * 100)}%`; });
    S.run = run;
    buildScene(run);
    fillRunInfo(run);
    wireUI();
    const f0 = Number(qs.get('frame'));
    S.pos = Number.isFinite(f0) && f0 > 0 ? Math.min(run.frames.length - 1, f0 - 1) : run.frames.reduce((bi, f, i, arr) => (f.maxDepth > arr[bi].maxDepth ? i : bi), 0);
    applyPos();
    setStatus(`${run.frames.length} frames · ${run.nx * run.ny} cells · exaggeration ×${S.world.exag.toFixed(0)}`);
    $('load').classList.add('done');
    S.clock.start(); tick();
  } catch (e) {
    console.error(e); msg.textContent = `failed: ${e.message}`; setStatus('load failed', true);
  }
}
main();

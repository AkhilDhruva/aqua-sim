// Buildings layer: tiled, batched, LOD'd extruded footprints.
//
// Consumes buildings.json (see ingestion/buildings.export_buildings_json):
// scene-local coordinates (the grid's UTM origin already subtracted
// server-side, so no float32 precision loss), tiled spatially. Each tile
// becomes a THREE.LOD with two levels:
//   LOD0 — true extruded footprint polygons (merged, one draw call per tile)
//   LOD1 — bounding-box prisms (far view, much cheaper triangulation)
// Frustum culling is per-tile (three.js culls by bounding sphere).
//
// Visualization only — the physics obstacle grid is produced independently by
// the ingestion module; nothing here feeds the solver.

import * as THREE from 'three';

const LOD_SWITCH_M = 1800;         // camera distance where LOD0 -> LOD1

function mergeNonIndexed(geoms) {
  let vtx = 0;
  for (const g of geoms) vtx += g.attributes.position.count;
  const pos = new Float32Array(vtx * 3);
  const nor = new Float32Array(vtx * 3);
  let o = 0;
  const ranges = [];
  for (const g of geoms) {
    const p = g.attributes.position.array;
    const n = g.attributes.normal.array;
    pos.set(p, o * 3);
    nor.set(n, o * 3);
    ranges.push([o, o + g.attributes.position.count]);
    o += g.attributes.position.count;
    g.dispose();
  }
  const merged = new THREE.BufferGeometry();
  merged.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  merged.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  merged.computeBoundingSphere();
  return { merged, ranges };
}

function extrudeBuilding(b, W, H, exag) {
  // Shape in (x, -z) so rotateX(-90deg) yields y-up, z-south.
  const outer = b.rings[0];
  const shape = new THREE.Shape();
  outer.forEach(([lx, ly], i) => {
    const sx = lx - W / 2, sy = -(ly - H / 2);
    if (i === 0) shape.moveTo(sx, sy); else shape.lineTo(sx, sy);
  });
  for (let r = 1; r < b.rings.length; r++) {
    const hole = new THREE.Path();
    b.rings[r].forEach(([lx, ly], i) => {
      const sx = lx - W / 2, sy = -(ly - H / 2);
      if (i === 0) hole.moveTo(sx, sy); else hole.lineTo(sx, sy);
    });
    shape.holes.push(hole);
  }
  const g = new THREE.ExtrudeGeometry(shape, { depth: b.h * exag, bevelEnabled: false });
  g.rotateX(-Math.PI / 2);
  g.translate(0, b.base * exag, 0);
  const ng = g.index ? g.toNonIndexed() : g;
  if (ng !== g) g.dispose();
  return ng;
}

function boxPrism(b, W, H, exag) {
  const xs = b.rings[0].map((p) => p[0]), ys = b.rings[0].map((p) => p[1]);
  const x0 = Math.min(...xs) - W / 2, x1 = Math.max(...xs) - W / 2;
  const z0 = Math.min(...ys) - H / 2, z1 = Math.max(...ys) - H / 2;
  const g = new THREE.BoxGeometry(Math.max(x1 - x0, 1), b.h * exag, Math.max(z1 - z0, 1));
  g.translate((x0 + x1) / 2, b.base * exag + (b.h * exag) / 2, (z0 + z1) / 2);
  const ng = g.index ? g.toNonIndexed() : g;
  if (ng !== g) g.dispose();
  return ng;
}

export class BuildingsLayer {
  constructor(scene) {
    this.scene = scene;
    this.group = new THREE.Group();
    this.group.name = 'buildingsLayer';
    scene.add(this.group);
    this.pickMeshes = [];   // LOD0 meshes with .userData.pick = {ranges, buildings}
    this.doc = null;
    this.matNear = new THREE.MeshStandardMaterial({
      color: 0x8b93a7, roughness: 0.85, metalness: 0.05, flatShading: true });
    this.matFar = new THREE.MeshStandardMaterial({
      color: 0x767e91, roughness: 0.95, flatShading: true });
  }

  clear() {
    for (const child of [...this.group.children]) {
      this.group.remove(child);
      child.traverse?.((o) => o.geometry?.dispose());
    }
    this.pickMeshes = [];
    this.doc = null;
  }

  build(doc, vertExag) {
    this.clear();
    this.doc = doc;
    const { nx, ny, dx } = doc.grid;
    const W = nx * dx, H = ny * dx;
    for (const tile of doc.tiles) {
      const near = [], far = [];
      for (const b of tile.buildings) {
        near.push(extrudeBuilding(b, W, H, vertExag));
        far.push(boxPrism(b, W, H, vertExag));
      }
      if (!near.length) continue;
      const { merged: gNear, ranges } = mergeNonIndexed(near);
      const { merged: gFar } = mergeNonIndexed(far);
      const mNear = new THREE.Mesh(gNear, this.matNear);
      mNear.userData.pick = { ranges, buildings: tile.buildings };
      const mFar = new THREE.Mesh(gFar, this.matFar);
      const lod = new THREE.LOD();
      lod.addLevel(mNear, 0);
      lod.addLevel(mFar, LOD_SWITCH_M);
      this.group.add(lod);
      this.pickMeshes.push(mNear);
    }
    return { tiles: doc.tiles.length, buildings: doc.building_count };
  }

  setVisible(v) { this.group.visible = v; }

  update(camera) {
    for (const child of this.group.children) {
      if (child.isLOD) child.update(camera);
    }
  }

  // Raycast pick: returns the building record under the pointer, or null.
  pick(raycaster) {
    if (!this.group.visible) return null;
    const hits = raycaster.intersectObjects(this.pickMeshes, false);
    if (!hits.length) return null;
    const hit = hits[0];
    const vtx = hit.faceIndex * 3;
    const { ranges, buildings } = hit.object.userData.pick;
    for (let i = 0; i < ranges.length; i++) {
      if (vtx >= ranges[i][0] && vtx < ranges[i][1]) return buildings[i];
    }
    return null;
  }
}

// Flood statistics for one building, computed from the loaded frames.
// Uses the cells of the building's footprint bbox expanded by one ring
// (obstacle cells themselves are dry by construction at fine resolution).
export function buildingFloodStats(b, frames, grid, hazard) {
  const [bi0, bj0, bi1, bj1] = b.cells;
  const i0 = Math.max(bi0 - 1, 0), j0 = Math.max(bj0 - 1, 0);
  const i1 = Math.min(bi1 + 1, grid.nx - 1), j1 = Math.min(bj1 + 1, grid.ny - 1);
  const dCrit = hazard?.depth_critical_m ?? 0.5;
  const df = hazard?.debris_factor ?? 0.5;
  const bands = hazard?.hr_bands ?? { low: 0.75, moderate: 1.25, significant: 2.0 };
  let peak = 0, firstCross = null, maxHR = 0;
  for (const fr of frames) {
    let frameMax = 0;
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) {
        const d = fr.depth[j][i];
        if (d > frameMax) frameMax = d;
        if (d > 0) {
          const s = fr.speed?.[j]?.[i] ?? 0;
          const hr = d * (Math.max(s, 0) + df);
          if (hr > maxHR) maxHR = hr;
        }
      }
    }
    if (frameMax > peak) peak = frameMax;
    if (firstCross === null && frameMax >= dCrit) firstCross = fr.time_s;
  }
  let hClass = 'NONE';
  if (maxHR > 0) {
    hClass = maxHR < bands.low ? 'LOW' : maxHR < bands.moderate ? 'MODERATE'
      : maxHR < bands.significant ? 'SIGNIFICANT' : 'EXTREME';
  }
  return { peakAdjacentDepth: peak, firstCrossing_s: firstCross,
           maxHazardClass: hClass, maxHR };
}

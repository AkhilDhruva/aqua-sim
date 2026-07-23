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
  // Merge with REAL face normals. (A position-only merge relying on
  // flatShading's derivative normals renders roofs near-unlit on this
  // pipeline — computed per-face normals through the standard shader path are
  // reliable, and non-indexed geometry keeps the faceted look.)
  let vtx = 0;
  for (const g of geoms) vtx += g.attributes.position.count;
  const pos = new Float32Array(vtx * 3);
  let o = 0;
  const ranges = [];
  for (const g of geoms) {
    pos.set(g.attributes.position.array, o * 3);
    ranges.push([o, o + g.attributes.position.count]);
    o += g.attributes.position.count;
    g.dispose();
  }
  const merged = new THREE.BufferGeometry();
  merged.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  merged.computeVertexNormals();   // non-indexed => true per-face normals
  merged.computeBoundingSphere();
  return { merged, ranges };
}

// Clean a ring for triangulation: scene-plane points, consecutive duplicates
// removed, closing point dropped. Returns null for degenerate (<3 pts) rings.
function cleanRing(ring, W, H) {
  const pts = [];
  for (const [lx, ly] of ring) {
    const x = lx - W / 2, y = -(ly - H / 2);
    const prev = pts[pts.length - 1];
    if (!prev || Math.abs(prev.x - x) > 1e-6 || Math.abs(prev.y - y) > 1e-6) {
      pts.push(new THREE.Vector2(x, y));
    }
  }
  if (pts.length > 1 && pts[0].distanceToSquared(pts[pts.length - 1]) < 1e-12) pts.pop();
  return pts.length >= 3 ? pts : null;
}

function shapeFromPolygon(poly, W, H) {
  // ring[0] outer, rest holes; (x, -z) so rotateX(-90deg) yields y-up, z-south.
  //
  // Winding is NORMALIZED here — the source data mixes ring orientations
  // (ESRI CW vs GeoJSON CCW), and with flat shading (derivative normals) the
  // wrong orientation renders as dark patches on roofs. The shape plane maps
  // sy = -sceneZ (y mirrored), so a ring that must read CCW *from above in the
  // scene* must be CW in shape space: outer -> CW (negative area), holes -> CCW.
  const outer = cleanRing(poly[0], W, H);
  if (!outer) return null;
  if (THREE.ShapeUtils.area(outer) > 0) outer.reverse();       // outer -> CW (up-facing cap)
  const shape = new THREE.Shape(outer);
  for (let r = 1; r < poly.length; r++) {
    const hole = cleanRing(poly[r], W, H);
    if (!hole) continue;
    if (THREE.ShapeUtils.area(hole) < 0) hole.reverse();       // holes -> CCW
    shape.holes.push(new THREE.Path(hole));
  }
  return shape;
}

function extrudeBuilding(b, W, H, exag) {
  // One extruded geometry per polygon part, merged into one building geometry.
  const parts = [];
  for (const poly of b.polys) {
    const g = new THREE.ExtrudeGeometry(shapeFromPolygon(poly, W, H),
      { depth: b.h * exag, bevelEnabled: false });
    g.rotateX(-Math.PI / 2);
    g.translate(0, b.base * exag, 0);
    const ng = g.index ? g.toNonIndexed() : g;
    if (ng !== g) g.dispose();
    parts.push(ng);
  }
  if (parts.length === 1) return parts[0];
  return mergeNonIndexed(parts).merged;
}

function boxPrism(b, W, H, exag) {
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  for (const poly of b.polys) for (const [x, y] of poly[0]) {
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < z0) z0 = y; if (y > z1) z1 = y;
  }
  x0 -= W / 2; x1 -= W / 2; z0 -= H / 2; z1 -= H / 2;
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
    // No flatShading: merged geometry carries true per-face normals (see
    // mergeNonIndexed), which gives the same faceted look via the reliable
    // normal-attribute shader path.
    this.matNear = new THREE.MeshStandardMaterial({
      color: 0x8b93a7, roughness: 0.85, metalness: 0.05 });
    this.matFar = new THREE.MeshStandardMaterial({
      color: 0x767e91, roughness: 0.95 });
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
const KINETIC_FLOOR_M = 0.08;   // must match the shader's uKineticFloor

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
          // Velocity term gated below the kinetic floor, exactly as the water
          // shader does, so the panel's hazard can't exceed what's on screen.
          const s = d >= KINETIC_FLOOR_M ? (fr.speed?.[j]?.[i] ?? 0) : 0;
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

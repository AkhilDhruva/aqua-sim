// Flood color ramps for the kinetic water shader.
//
// Each palette maps a normalized "danger" value in [0,1] to a color. Danger is
// computed from BOTH depth and velocity (see app.js / the fragment shader):
//
//     depthN  = depth / D_CRIT                     (D_CRIT = 0.5 m)
//     hazard  = depth * (speed + 0.5)              (depth-velocity hazard rating)
//     hazardN = hazard / HR_CRIT                   (HR_CRIT = 1.25)
//     danger  = clamp(max(depthN, hazardN), 0, 1)
//
// So water reads as critical when it is EITHER deep (>=0.5 m, floods entrances /
// stalls vehicles) OR kinetically dangerous (fast + non-trivial depth). Crimson
// therefore marks true infrastructure-threatening water, not mere wet pavement.
//
// Physical anchors shared by every palette (only the hue mapping differs):
//   danger 0.30  -> ~0.15 m  : nuisance / safe accumulation      (translucent, cool)
//   danger 0.55  -> ~0.30 m  : hazardous to pedestrians          (mid)
//   danger 0.80  -> ~0.50 m  : CRITICAL — subway/basement ingress (hot)
//   danger 1.00  -> >=1.0 m  : lethal / total inundation          (opaque, hottest)

export const THRESHOLDS = {
  D_CRIT: 0.5,      // m — depth at which water is treated as critical
  HR_CRIT: 1.25,    // depth-velocity hazard rating for "significant" band
  MIN_DEPTH: 0.01,  // m — below this a cell is dry (not drawn)
  ALPHA_FULL: 0.6,  // m — depth at which the water is fully opaque
  ALPHA_MIN: 0.18,  // opacity of the thinnest drawn water
  ALPHA_MAX: 0.94,  // opacity of deep water
};

// hex helper -> [r,g,b] in 0..1
const c = (hex) => [
  parseInt(hex.slice(1, 3), 16) / 255,
  parseInt(hex.slice(3, 5), 16) / 255,
  parseInt(hex.slice(5, 7), 16) / 255,
];

export const PALETTES = {
  crimson_surge: {
    label: 'Crimson Surge',
    note: 'Your cyan→crimson brief: intuitive, high-urgency.',
    stops: [
      { pos: 0.00, hex: '#22E0E0' }, // cyan — safe
      { pos: 0.30, hex: '#1E78FF' }, // azure
      { pos: 0.55, hex: '#FFB020' }, // amber
      { pos: 0.80, hex: '#DC143C' }, // crimson — CRITICAL
      { pos: 1.00, hex: '#8B0000' }, // blood — lethal
    ],
  },
  ice_to_blood: {
    label: 'Ice-to-Blood',
    note: 'Stays in the water-hue family, escalating to blood red.',
    stops: [
      { pos: 0.00, hex: '#A8F0FF' },
      { pos: 0.30, hex: '#2E6BFF' },
      { pos: 0.55, hex: '#7A3CFF' },
      { pos: 0.80, hex: '#E01050' },
      { pos: 1.00, hex: '#6E0010' },
    ],
  },
  inferno: {
    label: 'Inferno (scientific)',
    note: 'Perceptually uniform, colorblind-safe — the peer-review look.',
    stops: [
      { pos: 0.00, hex: '#1B0C41' },
      { pos: 0.30, hex: '#721F81' },
      { pos: 0.55, hex: '#F1605D' },
      { pos: 0.80, hex: '#FEAF77' },
      { pos: 1.00, hex: '#FCFDBF' },
    ],
  },
  storm_teal_red: {
    label: 'Storm Teal→Red',
    note: 'High-contrast command-center palette.',
    stops: [
      { pos: 0.00, hex: '#0E7C7B' },
      { pos: 0.30, hex: '#3FBF6F' },
      { pos: 0.55, hex: '#F5A623' },
      { pos: 0.80, hex: '#E8331C' },
      { pos: 1.00, hex: '#7A0A00' },
    ],
  },
};

export const DEFAULT_PALETTE = 'crimson_surge';

// Expand a palette into flat uniform arrays for the shader (5 stops).
export function paletteUniforms(key) {
  const p = PALETTES[key] || PALETTES[DEFAULT_PALETTE];
  const colors = [];
  const positions = [];
  for (const s of p.stops) {
    const [r, g, b] = c(s.hex);
    colors.push(r, g, b);
    positions.push(s.pos);
  }
  return { colors, positions, count: p.stops.length };
}

export function stopHexes(key) {
  const p = PALETTES[key] || PALETTES[DEFAULT_PALETTE];
  return p.stops.map((s) => s.hex);
}

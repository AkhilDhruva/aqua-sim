#!/usr/bin/env python3
"""Summarize a Lower Manhattan run folder: street-level peak depths, hotspots,
pits, station results. Usage: python3 analyze.py <run_dir> <out.json>"""
import json
import os
import sys

import numpy as np
from scipy import ndimage as ndi
from pyproj import Transformer

run, out = sys.argv[1], sys.argv[2]
m = json.load(open(os.path.join(run, "manifest.json")))
t = json.load(open(os.path.join(run, "terrain.json")))
notes = json.load(open(os.path.join(run, "run_notes.json")))
z = np.array(t["z"]); ob = np.array(t["obstacle"]) > 0
dx = t["dx"]; left, top = notes["transform"][2], notes["transform"][5]
land = (z >= -1.0) & ~ob                    # open ground (rivers are the sink)
peak = np.zeros_like(z); peak_t = np.zeros_like(z); vmax = np.zeros_like(z)
for f in m["frames"]:
    fr = json.load(open(os.path.join(run, f["file"])))
    d = np.array(fr["depth"]); s = np.array(fr["speed"])
    upd = d > peak; peak[upd] = d[upd]; peak_t[upd] = fr["time_s"]
    vmax = np.maximum(vmax, s)
# Pits: closed depressions >= 1.5 m deep below their 15 m surroundings
# (2013 excavations, sunken lots, the WTC site): water collects but they are
# not streets, so street statistics exclude them.
ring = ndi.grey_dilation(np.where(land, z, -99), size=9)
pit = land & (ring - z > 1.5) & (peak > 1.0)
pit = ndi.binary_dilation(pit, iterations=2) & land
street = land & ~pit
tr = Transformer.from_crs(notes["crs"], "EPSG:4326", always_xy=True)

def lonlat(r, c):
    lon, lat = tr.transform(left + (c + 0.5) * dx, top - (r + 0.5) * dx)
    return [round(lat, 5), round(lon, 5)]

lab, n = ndi.label(street & (peak >= 0.5))
spots = []
for k in range(1, n + 1):
    cells = lab == k
    area = float(cells.sum() * dx * dx)
    if area < 200:
        continue
    r, c = np.unravel_index(np.argmax(np.where(cells, peak, 0)), peak.shape)
    spots.append({"latlon": lonlat(r, c), "area_m2": round(area), "max_depth_m": round(float(peak[r, c]), 2),
                  "time_of_peak_min": round(float(peak_t[r, c]) / 60), "ground_z_m": round(float(z[r, c]), 2)})
spots.sort(key=lambda s: -s["area_m2"])
pl, pn = ndi.label(pit)
pits = []
for k in range(1, pn + 1):
    cells = pl == k
    r, c = np.unravel_index(np.argmax(np.where(cells, peak, 0)), peak.shape)
    pits.append({"latlon": lonlat(r, c), "area_m2": round(float(cells.sum() * dx * dx)),
                 "max_depth_m": round(float(peak[r, c]), 2)})
pits.sort(key=lambda p: -p["max_depth_m"])
stations = []
alerts = json.load(open(os.path.join(run, "alerts.json")))
for nd in m["nodes"]:
    x, y = nd["x"], nd["y"]
    first = [a["time_s"] for a in alerts if a["location"] == nd["name"] and a["severity"] == "CRITICAL"]
    # The probe is the lowest open cell within 100 m, which in 2013 terrain can
    # be an excavation or sunken lot. Street depths within 50 m of it are the
    # more robust measure of what the entrance sees.
    rr = int(round(50 / dx))
    yy, xx = np.ogrid[:z.shape[0], :z.shape[1]]
    near = street & ((yy - y) ** 2 + (xx - x) ** 2 <= rr * rr)
    vals = peak[near]
    stations.append({"name": nd["name"], "probe_latlon": lonlat(y, x), "ground_z_m": round(float(z[y, x]), 2),
                     "probe_in_pit": bool(pit[y, x]),
                     "probe_peak_depth_m": round(float(peak[y, x]), 3),
                     "street_within_50m_median_peak_m": round(float(np.median(vals)), 3) if vals.size else None,
                     "street_within_50m_p90_peak_m": round(float(np.percentile(vals, 90)), 3) if vals.size else None,
                     "breach_min": round(first[0] / 60) if first else None})
area_cell = dx * dx
res = {
    "run_id": m["run_id"], "aoi": m["aoi"], "grid": m["grid"],
    "street_cells_m2": round(float(street.sum() * area_cell)),
    "street_peak_depth_m": round(float(peak[street].max()), 3),
    "street_area_wet_over_m2": {f"{th}m": round(float((street & (peak > th)).sum() * area_cell))
                                for th in (0.05, 0.1, 0.3, 0.5, 1.0)},
    "street_peak_speed_m_s": round(float(vmax[street].max()), 2),
    "hotspots_over_0p5m_largest": spots[:12], "hotspot_count_over_200m2": len(spots),
    "pits_excluded": {"count": len(pits), "deepest": pits[:5],
                      "rule": "closed depressions >= 1.5 m below 15 m surroundings that held > 1 m"},
    "stations": stations,
}
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: res[k] for k in ("street_peak_depth_m", "street_area_wet_over_m2", "street_peak_speed_m_s",
                                      "hotspot_count_over_200m2")}))
for s in res["hotspots_over_0p5m_largest"]: print("hotspot", s)
for p in res["pits_excluded"]["deepest"]: print("pit", p)
for s in stations: print("station", s)

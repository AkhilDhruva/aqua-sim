"""Historical validation: Hurricane Ida remnants over Manhattan, Sep 1 2021.

The event: the remnants of Hurricane Ida dropped a record 3.15 inches
(80.0 mm) of rain on Central Park in the single hour ending 9:51 PM EDT —
the most intense hour ever recorded there, breaking the record set by
Tropical Storm Henri eleven days earlier. Event totals in the city were
~6–8 inches. Runoff overwhelmed the sewer system (designed for ~1.75 in/hr
≈ 44 mm/hr) and poured into the subway; dozens of stations flooded and
subway service was suspended citywide. Sources: NWS New York observations,
NYC & MTA post-event reports (encoded below from those documented figures).

The validation question: **driven by the documented Ida hyetograph over the
real USGS 3DEP terrain, does the model reproduce flooding at the subway
stations that actually flooded?** The primary metric is the probability of
detection (POD) over documented-flooded stations. A false-alarm rate needs an
authoritative list of stations that did NOT flood, which we do not have — so
FAR is explicitly out of scope and stated as such in the report.

Honest limitations (also embedded in the report):
  * Station coordinates are approximate (general knowledge, ±~100 m); refine
    from MTA GTFS when network egress allows.
  * Building footprints are not burned in unless a GeoJSON is supplied
    (sources blocked in this sandbox); at 20–40 m cells the terrain signal
    dominates, but street-canyon channeling is not resolved without them.
  * The USGS 1/3 arc-second tile n41w074 ends at longitude −74.000°, so the
    Battery Park City strip west of it is outside the domain. All validation
    stations lie east of −74.0°.
  * Uniform drainage approximates a surcharged sewer network.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from aqua_sim.config import SimConfig, SolverConfig, StormConfig
from aqua_sim.risk.sink_nodes import SinkNode
from aqua_sim.scenario import Scenario, run_scenario

# ---------------------------------------------------------------------------
# Documented storm forcing.
#
# Approximate staircase reconstruction of the NWS Central Park gauge: the peak
# hour is exact (80.0 mm, the record hour ending 21:51 EDT); the shoulder hours
# are approximate. Total 160 mm ≈ the ~6.3 in observed in the core hours.
IDA_HYETOGRAPH = (
    (0.0, 20.0),   # hour 1 — leading rain bands
    (1.0, 80.0),   # hour 2 — THE RECORD HOUR (3.15 in / 80 mm, exact)
    (2.0, 40.0),   # hour 3 — trailing heavy rain
    (3.0, 20.0),   # hour 4 — tapering
)
IDA_DURATION_HOURS = 4.0
#: NYC sewers are designed for roughly 1.75 in/hr (~44 mm/hr) — NYC DEP figure.
NYC_DRAINAGE_CAPACITY_MM_HR = 44.0

# ---------------------------------------------------------------------------
# Documented flooded subway stations (Manhattan) during Ida, with approximate
# WGS84 coordinates. Each of these appears in MTA/news documentation of the
# event (e.g. the 157 St and 28 St platform-flooding footage).
IDA_FLOODED_STATIONS = (
    ("157 St (1)",        40.8340, -73.9412),
    ("Dyckman St (1)",    40.8607, -73.9255),
    ("168 St (1/A/C)",    40.8404, -73.9401),
    ("28 St (1)",         40.7470, -73.9910),
    ("96 St (1/2/3)",     40.7938, -73.9722),
    ("Times Sq-42 St",    40.7553, -73.9870),
)

#: AOI covering all validation stations (east of the tile edge at -74.0°).
IDA_AOI = (-73.999, 40.735, -73.918, 40.875)


@dataclass
class StationResult:
    name: str
    breached: bool
    peak_depth_m: float
    first_breach_min: float | None


def _lonlat_to_index(grid, lon: float, lat: float) -> tuple[int, int]:
    """Map WGS84 lon/lat to (x, y) grid indices via the grid's affine transform."""
    from rasterio.warp import transform as warp_transform

    xs, ys = warp_transform("EPSG:4326", grid.crs, [lon], [lat])
    a, _, c, _, e, f = grid.transform
    x = int((xs[0] - c) / a)
    y = int((ys[0] - f) / e)
    if not (0 <= x < grid.nx and 0 <= y < grid.ny):
        raise ValueError(f"({lon}, {lat}) falls outside the grid ({x}, {y}).")
    return x, y


def _local_low(grid, x: int, y: int, radius_cells: int) -> tuple[int, int]:
    """The lowest in-AOI cell within ``radius_cells`` of (x, y).

    Station coordinates carry ~±100 m uncertainty, and entrances flood via the
    low corner of their surroundings — runoff converges there. Sampling the
    exact cell of an approximate coordinate lands on an arbitrary slope; the
    local minimum is the physically meaningful probe point.
    """
    best = (x, y)
    best_z = grid.z[y][x]
    for j in range(max(0, y - radius_cells), min(grid.ny, y + radius_cells + 1)):
        for i in range(max(0, x - radius_cells), min(grid.nx, x + radius_cells + 1)):
            if grid.mask[j][i] and grid.z[j][i] < best_z:
                best, best_z = (i, j), grid.z[j][i]
    return best


#: Height of a sidewalk vent/grate lip above grade (m). Ida's documented
#: primary ingress path was street-level vents — ingress begins when standing
#: water covers them, not at a raised entrance step.
VENT_LIP_M = 0.05
#: Station-coordinate uncertainty expressed in cells at the run resolution.
COORD_UNCERTAINTY_M = 100.0


def build_ida_scenario(
    dem_path: str,
    target_dx_m: float = 30.0,
    buildings_geojson: str | None = None,
    sim_hours: float = 6.0,
    drainage_blockage: float = 0.0,
) -> Scenario:
    """Assemble the Ida validation scenario on real terrain.

    Each station's sink node is placed at the **lowest ground cell within the
    coordinate-uncertainty radius** (~100 m) of its approximate location —
    runoff converges to the local low, and that is where entrances flood. The
    node threshold sits ``VENT_LIP_M`` above that ground (street-level vent
    ingress, the documented Ida pathway).

    ``drainage_blockage`` expresses the sewer state: 0.0 = full design
    capacity all storm; 0.5 = surcharged to half; 1.0 = failed. The
    validation runs a sensitivity matrix over this rather than picking one.
    """
    from aqua_sim.ingestion.dem import DEMSource

    grid = DEMSource(dem_path, target_dx_m=target_dx_m, aoi_bounds=IDA_AOI).load()
    if buildings_geojson:
        from aqua_sim.ingestion.buildings import burn_buildings
        burn_buildings(grid, buildings_geojson)

    radius_cells = max(1, int(round(COORD_UNCERTAINTY_M / target_dx_m)))
    nodes = []
    for name, lat, lon in IDA_FLOODED_STATIONS:
        x0, y0 = _lonlat_to_index(grid, lon, lat)
        x, y = _local_low(grid, x0, y0, radius_cells)
        nodes.append(SinkNode(
            name=name, x=x, y=y,
            threshold_elevation=grid.z[y][x] + VENT_LIP_M,
            opening_area_m2=3.0,       # a stairwell-scale opening
            capacity_m3=2000.0,        # mezzanine/platform-scale storage
        ))

    storm = StormConfig(
        rainfall_mm_per_hr=80.0,       # headline figure: the record hour
        duration_hours=IDA_DURATION_HOURS,
        drainage_capacity_mm_per_hr=NYC_DRAINAGE_CAPACITY_MM_HR,
        drainage_blockage=drainage_blockage,
        hyetograph=IDA_HYETOGRAPH,
    )
    total_s = sim_hours * 3600.0
    config = SimConfig(
        storm=storm,
        solver=SolverConfig(cfl=0.7, total_time_s=total_s,
                            output_interval_s=total_s / 100.0),
        aoi_name="Manhattan — Hurricane Ida remnants, 2021-09-01 (validation)",
    )
    return Scenario(grid=grid, config=config, nodes=nodes)


def run_ida_validation(
    dem_path: str,
    out_dir: str,
    target_dx_m: float = 30.0,
    buildings_geojson: str | None = None,
    drainage_blockage: float = 0.0,
) -> dict:
    """Run one Ida scenario and score it against the documented outcome.

    Writes the standard run folder plus ``validation.json``; returns the
    validation report dict.
    """
    scenario = build_ida_scenario(dem_path, target_dx_m, buildings_geojson,
                                  drainage_blockage=drainage_blockage)
    manifest = run_scenario(scenario, out_dir)

    with open(os.path.join(out_dir, "alerts.json")) as f:
        alerts = json.load(f)
    breach_times: dict[str, float] = {}
    for a in alerts:
        if a["severity"] == "CRITICAL" and a["location"] in {n.name for n in scenario.nodes}:
            breach_times.setdefault(a["location"], a["time_s"])

    # Peak depth per station cell across all frames.
    peaks = {n.name: 0.0 for n in scenario.nodes}
    for rec in manifest["frames"]:
        with open(os.path.join(out_dir, rec["file"])) as f:
            frame = json.load(f)
        for n in scenario.nodes:
            d = frame["depth"][n.y][n.x]
            if d > peaks[n.name]:
                peaks[n.name] = d

    stations = [
        StationResult(
            name=n.name,
            breached=n.name in breach_times,
            peak_depth_m=round(peaks[n.name], 3),
            first_breach_min=(round(breach_times[n.name] / 60.0, 1)
                              if n.name in breach_times else None),
        )
        for n in scenario.nodes
    ]
    detected = sum(1 for s in stations if s.breached)
    report = {
        "event": "Hurricane Ida remnants, Manhattan, 2021-09-01",
        "run_id": manifest["run_id"],
        "terrain": manifest["provenance"]["terrain_source"],
        "terrain_digest": manifest["provenance"]["terrain_digest"],
        "grid": manifest["grid"],
        "storm": {
            "hyetograph_mm_per_hr": [list(step) for step in IDA_HYETOGRAPH],
            "total_rainfall_mm": scenario.config.storm.total_rainfall_mm(),
            "peak_hour_mm": 80.0,
            "drainage_capacity_mm_per_hr": NYC_DRAINAGE_CAPACITY_MM_HR,
        },
        "drainage_blockage": drainage_blockage,
        "stations": [s.__dict__ for s in stations],
        "probability_of_detection": round(detected / len(stations), 3),
        "detected": detected,
        "documented_flooded": len(stations),
        "false_alarm_rate": None,
        "notes": [
            "POD over documented-flooded stations; FAR requires an authoritative "
            "list of non-flooded stations and is out of scope.",
            "Station coordinates approximate (±~100 m).",
            "Hyetograph: peak hour exact (80 mm NWS Central Park record); "
            "shoulder hours approximate staircase.",
            "Buildings " + ("burned in." if buildings_geojson else
                            "NOT burned in (footprint sources unreachable); "
                            "street-canyon channeling unresolved."),
            "Tile n41w074 ends at lon -74.0; Battery Park City strip excluded.",
        ],
    }
    with open(os.path.join(out_dir, "validation.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


def run_ida_sensitivity(
    dem_path: str,
    out_root: str,
    target_dx_m: float = 30.0,
    buildings_geojson: str | None = None,
    blockages: tuple[float, ...] = (0.0, 0.5, 1.0),
) -> dict:
    """The full validation: a drainage-state sensitivity matrix.

    The sewer state during Ida is the one forcing we cannot know precisely
    (the system is documented as overwhelmed, but not by how much, where, or
    when). Rather than pick a flattering value, run the storm under three
    assumptions — design capacity (blockage 0.0), surcharged-to-half (0.5),
    failed (1.0) — and report POD for each. Each case writes a full run folder
    under ``out_root``; the combined report is ``out_root/validation.json``.
    """
    cases = {}
    for b in blockages:
        sub = os.path.join(out_root, f"blockage_{int(round(b * 100)):03d}")
        cases[f"{b:.2f}"] = run_ida_validation(
            dem_path, sub, target_dx_m, buildings_geojson, drainage_blockage=b)
    combined = {
        "event": "Hurricane Ida remnants, Manhattan, 2021-09-01",
        "method": "drainage-state sensitivity matrix (blockage 0 / 0.5 / 1.0)",
        "pod_by_drainage_blockage": {
            k: {"probability_of_detection": v["probability_of_detection"],
                "detected": v["detected"],
                "documented_flooded": v["documented_flooded"],
                "run_id": v["run_id"], "stations": v["stations"]}
            for k, v in cases.items()
        },
        "shared_notes": next(iter(cases.values()))["notes"],
    }
    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, "validation.json"), "w") as f:
        json.dump(combined, f, indent=2)
    return combined


if __name__ == "__main__":  # pragma: no cover - manual utility
    import sys

    if len(sys.argv) < 3:
        print("usage: python -m aqua_sim.validation.ida2021 DEM.tif OUTDIR "
              "[dx_m] [buildings.geojson]")
        raise SystemExit(2)
    dem, out = sys.argv[1], sys.argv[2]
    dx = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    bld = sys.argv[4] if len(sys.argv) > 4 else None
    rep = run_ida_sensitivity(dem, out, target_dx_m=dx, buildings_geojson=bld)
    for blk, case in rep["pod_by_drainage_blockage"].items():
        print(f"\ndrainage blockage {blk}: POD = {case['probability_of_detection']} "
              f"({case['detected']}/{case['documented_flooded']})  run {case['run_id']}")
        for s in case["stations"]:
            mark = "BREACHED" if s["breached"] else "dry"
            eta = f" at t={s['first_breach_min']} min" if s["first_breach_min"] else ""
            print(f"  {s['name']:<18} {mark:<9} peak depth {s['peak_depth_m']} m{eta}")
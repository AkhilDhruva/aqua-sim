"""Validation utilities for conditioned hydraulic surfaces (Phase 6A).

Two things the plan asks for beyond the physics-proof unit tests:

  * ``cross_section`` — sample bed elevation, barrier crests, and water depth
    along a straight line of cells, for inspecting a street cross-section.
  * ``compare_resolutions`` — run the same scenario at two cell sizes and report
    mass-balance and flood arrival-time differences, so a coarse screening run
    can be checked against a fine one.

These operate on scenarios/grids the caller already conditioned; they add no new
physics, only measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class CrossSection:
    """Bed, barrier crest and water depth sampled along a cell path."""
    cells: list[tuple[int, int]]
    distance_m: list[float]
    bed_m: list[float]
    crest_m: list[Optional[float]]
    depth_m: list[float]
    surface_m: list[float]


def cross_section(grid, state, x0, y0, x1, y1) -> CrossSection:
    """Sample a straight cell line (x0,y0)->(x1,y1) through a FlowState.

    ``crest_m`` reports the higher of the two subgrid face crests bounding each
    cell in the dominant direction (None where there is no barrier).
    """
    n = max(abs(x1 - x0), abs(y1 - y0), 1)
    cells, dist, bed, crest, depth, surf = [], [], [], [], [], []
    horizontal = abs(x1 - x0) >= abs(y1 - y0)
    for k in range(n + 1):
        t = k / n
        i = int(round(x0 + (x1 - x0) * t))
        j = int(round(y0 + (y1 - y0) * t))
        i = min(max(i, 0), grid.nx - 1)
        j = min(max(j, 0), grid.ny - 1)
        cells.append((i, j))
        dist.append(k * grid.dx)
        b = grid.z[j][i]
        d = state.depth[j][i]
        bed.append(b)
        depth.append(d)
        surf.append(b + d)
        cr = None
        if horizontal and grid.crest_x is not None:
            vals = [grid.crest_x[j][i], grid.crest_x[j][i + 1]]
            vals = [v for v in vals if v > -1e30]
            cr = max(vals) if vals else None
        elif not horizontal and grid.crest_y is not None:
            vals = [grid.crest_y[j][i], grid.crest_y[j + 1][i]]
            vals = [v for v in vals if v > -1e30]
            cr = max(vals) if vals else None
        crest.append(cr)
    return CrossSection(cells, dist, bed, crest, depth, surf)


@dataclass
class ResolutionComparison:
    coarse_dx: float
    fine_dx: float
    coarse_peak_volume_m3: float
    fine_peak_volume_m3: float
    volume_rel_diff: float
    coarse_arrival_min: Optional[float]
    fine_arrival_min: Optional[float]
    arrival_diff_min: Optional[float]
    coarse_mass_error: float
    fine_mass_error: float
    notes: list[str] = field(default_factory=list)


def _run_metrics(scenario_factory: Callable, dx: float, probe_lonlat, arrival_depth_m):
    """Run one scenario at cell size ``dx``; return (peak_volume, arrival_min,
    mass_error). ``scenario_factory(dx)`` must return a Scenario."""
    from aqua_sim.physics.swe_numpy import make_solver

    sc = scenario_factory(dx)
    solver = make_solver(sc.grid, sc.config, boundary=sc.boundary,
                         backend=sc.config.solver.backend)
    grid = sc.grid
    # Probe cell for arrival time.
    pi = pj = None
    if probe_lonlat is not None and grid.transform is not None:
        from rasterio.warp import transform as warp_transform
        a, _, left, _, e, top = grid.transform
        xs, ys = warp_transform("EPSG:4326", grid.crs, [probe_lonlat[0]], [probe_lonlat[1]])
        pi = int((xs[0] - left) / a); pj = int((top - ys[0]) / (-e))
        if not (0 <= pi < grid.nx and 0 <= pj < grid.ny):
            pi = pj = None

    # Rain actually delivered over the SIM window (not the whole storm), by
    # integrating the (possibly hyetograph) rainfall rate to run end.
    storm = sc.config.storm
    t_end = sc.config.solver.total_time_s
    steps = 2000
    rain_m = sum(storm.rainfall_at(t_end * k / steps) for k in range(steps)) \
        * (t_end / steps)
    dry_area = grid.cell_area_m2() * sum(1 for row in grid.mask for m in row if m)
    peak_vol = 0.0
    arrival = None
    for st in solver.run():
        peak_vol = max(peak_vol, st.total_volume_m3)
        if arrival is None and pi is not None and st.depth[pj][pi] >= arrival_depth_m:
            arrival = st.time_s / 60.0
    # Mass-balance error: peak stored volume vs rain volume in (a loose closed-
    # system check; open boundaries/drainage make this an upper bound, so we
    # report the relative gap rather than assert on it).
    expected = rain_m * dry_area
    mass_err = abs(peak_vol - expected) / expected if expected > 0 else 0.0
    return peak_vol, arrival, mass_err


def compare_resolutions(scenario_factory: Callable, coarse_dx: float,
                        fine_dx: float, probe_lonlat=None,
                        arrival_depth_m: float = 0.1) -> ResolutionComparison:
    """Run ``scenario_factory`` at two cell sizes and diff mass balance +
    arrival time. ``scenario_factory(dx)`` returns a Scenario at that resolution
    (typically a closure over a DEM path + conditioning).

    Returns a :class:`ResolutionComparison`; the caller decides tolerances
    (finer grids resolve more channelization, so some divergence is expected and
    physical, not error).
    """
    cv, ca, cerr = _run_metrics(scenario_factory, coarse_dx, probe_lonlat, arrival_depth_m)
    fv, fa, ferr = _run_metrics(scenario_factory, fine_dx, probe_lonlat, arrival_depth_m)
    vol_rel = abs(cv - fv) / fv if fv > 0 else 0.0
    adiff = (abs(ca - fa) if (ca is not None and fa is not None) else None)
    notes = [
        f"Peak stored volume: coarse {cv:.0f} m³ vs fine {fv:.0f} m³ "
        f"({vol_rel*100:.1f}% diff).",
        "Finer grids resolve street channelization and micro-relief, so some "
        "volume/arrival divergence is expected and physical, not numerical error.",
    ]
    if adiff is not None:
        notes.append(f"Arrival at probe: coarse {ca:.1f} min vs fine {fa:.1f} min "
                     f"(Δ {adiff:.1f} min).")
    return ResolutionComparison(
        coarse_dx=coarse_dx, fine_dx=fine_dx,
        coarse_peak_volume_m3=cv, fine_peak_volume_m3=fv, volume_rel_diff=vol_rel,
        coarse_arrival_min=ca, fine_arrival_min=fa, arrival_diff_min=adiff,
        coarse_mass_error=cerr, fine_mass_error=ferr, notes=notes)

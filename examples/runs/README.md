# Street-scale Manhattan runs — record

Run records (manifest, alerts, notes, derived analysis) for runs made with
`examples/nyc_street_run.py`. Frames and terrain are not committed (≈250 MB
per run); the `run_id` is the SHA-256 of the canonical provenance and the
notes carry the DEM digests, so a re-run from the same inputs is checkable.

Engine: solver sources identical to pin `0b452c9` (checked with
`git diff --stat 0b452c9 HEAD -- src/ pyproject.toml`, empty).

## manhattan_street_mosaic_terrain_only — run_id 7664097b73406bcd

| item | value |
|---|---|
| area | lon −74.025..−73.955, lat 40.695..40.775 (Lower Manhattan to ~79th St, plus Hudson, slivers of Hoboken/Weehawken and Brooklyn/Queens) |
| terrain | USGS 3DEP 1/3 arc-second, tiles n41w074 + n41w075 mosaicked, reprojected to EPSG:32618, 10 m |
| grid | 601 × 895 = 537,895 cells, 100 % covered |
| storm | Ida 2021 hyetograph 20/80/40/20 mm/h over 4 h, drainage 44 mm/h at 0.5 blockage (22 mm/h effective), 6 h simulated |
| buildings | **none — TERRAIN ONLY** (footprint hosts unreachable from the study host) |
| sensors | 28 St, Times Sq-42 St (Ida-flooded stations inside the box) + 4 auto low points on land |
| frames | 25 at 900 s |
| solve | 23.5 min single core, peak RSS 346 MiB |
| all-cell peak depth | 10.39 m — a 107-cell depression on the New Jersey side at (−74.0227, 40.7662), inside the box, outside Manhattan |
| Manhattan peak depth | 3.70 m at (−73.9719, 40.7678), the pond basin at the SE corner of Central Park |
| Manhattan area over 0.3 m at any time | 80.6 ha (26.6 km² land component); 45.7 ha over 0.5 m; 18.4 ha over 1 m |
| named sensors | 28 St max 0.057 m, Times Sq max 0.019 m — dry, consistent with the engine's Ida validation (POD 0/6 at 0.5 blockage) |
| alerts | 3 WARNING at T+1:15 (28 St, Times Sq, domain); no CRITICAL |
| hosted viewer | https://claude.ai/artifact/TVApTrzx8GZ3JwJTyjQEAg — decimated ×2 cells / ×3 frames (sha256 3250e7f4bab0b59e…), private |

Largest Manhattan pools (max depth over the run > 0.5 m), from `analysis_manhattan.json`:

| cells | ha | max m | mean m | ground z m | where |
|---|---|---|---|---|---|
| 420 | 4.2 | 3.48 | 1.16 | 4.9 | (−74.0007, 40.7329) West Village |
| 266 | 2.7 | 2.09 | 1.05 | 1.6 | (−74.0035, 40.7219) Canal St / Holland Tunnel approach |
| 254 | 2.5 | 3.14 | 1.93 | 7.9 | (−73.9754, 40.7660) Central Park, the Pond |
| 156 | 1.6 | 3.70 | 1.64 | 13.1 | (−73.9719, 40.7678) Central Park SE |
| 128 | 1.3 | 1.52 | 0.96 | 13.9 | (−73.9670, 40.7740) Central Park, Conservatory Water |
| 119 | 1.2 | 2.19 | 1.33 | 6.9 | (−73.9984, 40.7542) W 39th St / Lincoln Tunnel approach |

Place names are read off the coordinates, not verified against a basemap.

**Read these numbers as model properties, not observations.** The run has no
sewer network beyond the uniform 22 mm/h, no buildings, and a bare-earth DEM:
closed depressions (including Central Park's ponds and road cuts) fill until
they overtop, and river surfaces without bathymetry (the East River in this
tile) hold ponded rain. 407 of the 658 pools over 0.5 m are three cells or
fewer — DEM pits.

## manhattan_street_rehearsal_single_tile — run_id 08d2d7746f7c84f2

First rehearsal on tile n41w074 alone. The box extends west of that tile's
edge (−74.0006), so the Hudson strip was nodata; the solver treats nodata as
a wall and runoff dammed along the seam: all-cell peak 13.66 m in a 112-cell
pit at −9 m on the seam, 92 % coverage. Kept as the record of why the driver
now reports tile coverage and why the mosaic run exists. Superseded.

## Building-aware run (pending)

Same command with the official NYC footprints (NYC Open Data `nqwf-w8eh`):

    python3 examples/nyc_street_run.py \
        --dem USGS_13_n41w074.tif --dem USGS_13_n41w075.tif \
        --buildings nyc_building_footprints.gpkg \
        --buildings-url "https://data.cityofnewyork.us/Housing-Development/Building-Footprints/nqwf-w8eh" \
        --out <run_dir>

At 10 m, cells with ≥ 0.5 footprint coverage become no-flow walls of the
tallest building's height and `buildings.json` is written for the viewer.
The footprint hosts (data.cityofnewyork.us, OSM/Overpass, Microsoft
footprints) are denied by the study host's egress policy; allow
`data.cityofnewyork.us` for the environment or supply the file.

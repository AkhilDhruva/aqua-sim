# Lower Manhattan input data

Input data for the engine, not engine output. Lower Manhattan, Battery to 14th Street: WGS84 bbox west −74.022, south 40.698, east −73.968, north 40.742.

| File | What it is |
|---|---|
| `lm_lidar_buildings.geojson.gz` | 3D building massing: 1,943 connected building regions as 17,386 nested height-level polygons. EPSG:26918 meters, `height_m` is roof height above the 1 m DTM. |
| `lm_lidar_buildings_summary.json` | Provenance: sources, survey dates, input SHA-256 digests, detection counts, parameters. |
| `lm_lidar_fetch_manifest.json` | The 3,359 point-cloud tiles used, with URLs, point counts and SHA-256 digests. |
| `lm_elevated_structures.geojson` | Mapped bridges and viaducts (WGS84 lines with deck width), used to keep elevated decks out of the building walls. |
| `FETCH_FAILED.txt` | Why the official NYC footprint dataset is not here: its host was blocked from the build environment. |

## Where it came from

- **Point cloud:** USGS 3DEP LiDAR, project `NY_NewYorkCity`, public bucket `usgs-lidar-public` (Entwine Point Tiles). This is the city's 2013–2014 survey, from August 2013 to April 2014.
- **Ground:** USGS 3DEP 1 m DEM, project `NY_CMPG_2013`, tiles `x58y451` and `x58y452`. It comes from the same acquisition, in NAD83 / UTM 18N with NAVD88 heights.
- **Elevated structures:** Overture Maps transportation segments, release `2026-10-21.0`. The data is OpenStreetMap contributors' work under ODbL.

## How it was made

```bash
python3 -I examples/fetch_nyc_lidar.py --bbox -74.022 40.698 -73.968 40.742 --out <empty dir>
python3 -I examples/fetch_overture_bridges.py --release 2026-10-21.0 \
    --bbox -74.022 40.698 -73.968 40.742 --out lm_elevated_structures.geojson
python3 -I examples/build_lidar_buildings.py --laz-dir <dir> \
    --dem USGS_one_meter_x58y451_NY_CMPG_2013.tif --dem USGS_one_meter_x58y452_NY_CMPG_2013.tif \
    --bbox -74.022 40.698 -73.968 40.742 --elevated-lines lm_elevated_structures.geojson \
    --out lm_lidar_buildings.geojson.gz
```

The method is documented in `src/aqua_sim/ingestion/lidar.py`. Roof height is the highest above-ground return minus the DTM, on a 1 m grid. Tree canopy is rejected by its multi-return pulses. Vehicles, cranes and noise are rejected by size and shape. Elevated decks never become walls: open water, ground seen through a structure, and mapped bridges and viaducts are all excluded. Each building becomes nested 3 m height levels, which reproduce its setbacks.

## Checks

LiDAR roof height compared with published heights, taking the maximum within 30 m of each tower:

| Building | LiDAR | Published |
|---|---|---|
| One World Trade Center | 430 m | 417 m roof, 541 m spire |
| 4 World Trade Center | 296 m | 298 m |
| 8 Spruce Street | 268 m | 265 m |
| 70 Pine Street | 265 m | about 260 m roof, 290 m spire |
| 40 Wall Street | 262 m | about 260 m roof, 283 m spire |
| Woolworth Building | 236 m | about 230 m roof, 241 m top |

## Limits

- **Survey date.** Buildings are as of 2013–2014. Anything built later is missing or only partly built, such as 3 World Trade Center.
- **Blocks, not lots.** A region is a group of touching buildings, usually a block face, not a single tax lot. The official footprint dataset would add per-building identity once its host is reachable.
- **Thin spires.** Masts and spires are filtered as spikes, so heights are roof heights.

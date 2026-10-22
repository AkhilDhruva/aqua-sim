#!/bin/bash
# Resumable Lower Manhattan run: rerun after any container restart.
# DATA must hold dem1m/ (USGS 1 m tiles), build/lm_lidar_buildings.geojson.gz (or use
# examples/data/lm_lidar_buildings.geojson.gz) and a checkpoint folder ckpt_lm/.
: "${DATA:?set DATA to the input data folder}"
cd "$(dirname "$0")/../../.."
exec python3 examples/nyc_street_run.py --dem $DATA/dem1m/USGS_one_meter_x58y451_NY_CMPG_2013.tif --dem $DATA/dem1m/USGS_one_meter_x58y452_NY_CMPG_2013.tif --lidar-buildings $DATA/build/lm_lidar_buildings.geojson.gz --target-crs EPSG:26918 --resampling average --dx 4 --bbox -74.022 40.698 -73.968 40.742 --max-cells 2000000 --area-name "Lower Manhattan (Battery to 14th St)" --frames 24 --hours 6 --auto-nodes 0 --stations examples/data/lm_subway_stations.json --water-sink-below -1.0 --checkpoint-dir $DATA/ckpt_lm --out viz/lm_lidar_run

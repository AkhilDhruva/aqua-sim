# Lower Manhattan, 4 m, LiDAR buildings, Ida 2021 storm

Run `9dedd1c40dc9770d`. Lower Manhattan from the Battery to 14th Street, solved on a 4 m grid of 1,154 × 1,234 cells. Ground is the USGS 1 m bare-earth DEM averaged to 4 m. Buildings are the LiDAR massing in `examples/data` acting as no-flow walls. Rain follows the documented Hurricane Ida hyetograph over 4 hours, then 2 hours of drain-down, with storm drains at half capacity. Open water, meaning ground below −1 m, absorbs all runoff that reaches it.

This folder is the record: manifest, alerts, run notes, the command and the analysis. The 469 MB of frames are not committed. They regenerate with `run_command.sh` and are byte-identical on a rerun. The run was interrupted by four container restarts, plus one deliberate restart to load mid-frame checkpointing, and resumed from checkpoints each time.

## Results

| Measure | Value |
|---|---|
| Street area wetter than 10 cm at its peak | 1.34 km² |
| Street area deeper than 30 cm | 0.43 km² |
| Street area deeper than 50 cm | 0.17 km² |
| Street area deeper than 1 m | 0.02 km² |
| Deepest street water | 2.0 m |
| Fastest street flow | 5.9 m/s |
| Ponds deeper than 0.5 m and larger than 200 m² | 83 |

"Street" means open ground outside buildings, rivers and pits. Pits are closed depressions at least 1.5 m below their surroundings that held more than 1 m of water: 2013 excavations, sunken lots, the WTC site. Water collects in them, but they are not streets, so they are reported separately in `analysis.json`. The deepest is 4.05 m.

The largest ponds, by area:
1. **Canal Street near Sixth Avenue**, about 2.7 ha, up to 2.0 m. This is the historic low corridor of the old Collect Pond outflow.
2. **Sixth Avenue and West 4th Street**, about 1.8 ha, up to 1.7 m.
3. **Houston Street near Varick Street**, about 1.1 ha, up to 1.8 m.
4. **The low East Side waterfront**, several ponds of 0.7–1.3 m from Avenue D to the 14th Street Con Ed site.

## Stations

Each station sensor sits at the lowest open cell within 100 m of the entrance. In 2013 terrain that cell can be a pit, which makes the sensor's own breach alert pessimistic. The street-depth columns are the more robust reading.

| Station | Breach at | Median street peak within 50 m | 90th-percentile street peak within 50 m | Probe in a pit |
|---|---|---|---|---|
| Houston St (1) | 75 min | 0.90 m | 1.20 m | no |
| W 4 St–Washington Sq | 75 min | 0.41 m | 0.82 m | no |
| Christopher St–Sheridan Sq (1) | 90 min | 0.08 m | 0.63 m | no |
| Canal St (J/N/Q/R/W/Z/6) | 75 min | 0.12 m | 0.31 m | yes |
| South Ferry (1) | 90 min | 0.04 m | 0.34 m | yes |
| Franklin St (1) | 75 min | 0.00 m | 0.23 m | no |
| Canal St (1) | 180 min | 0.04 m | 0.20 m | no |
| Rector St (1) | none | 0.03 m | 0.19 m | no |
| Wall St (2/3) | none | 0.00 m | 0.15 m | no |
| Spring St (A/C/E) | 105 min | 0.00 m | 0.14 m | no |
| Delancey St–Essex St | 75 min | 0.03 m | 0.12 m | no |
| 2 Av (F) | 75 min | 0.01 m | 0.11 m | yes |
| Broadway–Lafayette St | 165 min | 0.01 m | 0.08 m | no |
| East Broadway (F) | 75 min | 0.00 m | 0.06 m | no |
| WTC Oculus / PATH | none | 0.00 m | 0.01 m | no |

## Limits

- **Not validated.** None of the stations documented as flooded during Ida lies in this area, so nothing here is scored against the real event.
- **Roof rain is dropped.** The solver discards rain falling on building cells, about 26% of the grid here. In New York that water goes to the combined sewer and uses up drain capacity, so street flooding is likely underestimated.
- **Terrain and buildings date from 2013–2014.** That includes excavations, such as the WTC site, that have since been built over.
- **4 m grid.** Walls come from exact 1 m building geometry, but alleys narrower than about 4 m close, and curbs and drain inlets are not resolved.

Reproduce the analysis with `python3 -I analyze.py viz/lm_lidar_run analysis.json`.

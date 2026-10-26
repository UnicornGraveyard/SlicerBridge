# Brand slicers

Files for Creality, Elegoo, Anycubic, Flashforge and QIDI printers carry settings written by OrcaSlicer. Each of
those makers ships its own fork of OrcaSlicer (or of Bambu Studio), and that's the slicer most owners open the
file in. The forks don't all read a project the same way, so SlicerBridge checks every file in its maker's slicer
too (`slicerbridge brand-check`) and writes the files so that both read them the same.

Tested on macOS with Creality Print 7.3.0, ElegooSlicer 1.5.3.5, Anycubic Slicer Next 2.0.0.3, Flash Studio
1.7.15 and QIDI Studio 2.07.02.73, against OrcaSlicer 2.4.2.

## What they read differently, and what the files do about it

| Slicer | What it does | In the file |
|---|---|---|
| QIDI Studio | Reads `skeleton_infill_line_width` and `skin_infill_line_width` as millimetres only, so OrcaSlicer's `100%` is 100 mm and it refuses to slice ("too large line width"). | Every line width given as a % of the nozzle is written in mm (100% of 0.4 mm is 0.4). OrcaSlicer reads both the same. |
| Anycubic Slicer Next | Reads `bed_exclude_area` as separate 4-point polygons; OrcaSlicer reads it as one polygon. Each takes the other's Kobra 3 frame (the bed's outline with a 2 mm smaller one inside) for the whole bed: "too close to exclusion area". | Where the two readings differ, the area is rewritten as rectangles reached along zero-width edges, as OrcaSlicer's own Kobra 3 Max preset is. Both read it the same. |
| Anycubic Slicer Next | The Kobra X's color change G-code (from OrcaSlicer 2.4.2's preset) indexes `ace_t_box_vector`, which Anycubic Slicer Next fills only once a printer's filaments are mapped. Until then: "Indexing an empty vector variable". Anycubic's own preset guards it with `size()`, a guard OrcaSlicer can't parse. | The guard is nested inside the ACE block, which OrcaSlicer skips and Anycubic Slicer Next runs. |
| All of them | Many presets have other names than OrcaSlicer's (QIDI Studio's `Q2 0.4 nozzle` is OrcaSlicer's `Qidi Q2 0.4 nozzle`; Creality Print has no `0.16mm Optimal` processes). | Nothing to fix: a preset name the app doesn't have loads as a project preset holding the file's own settings (`PresetCollection::load_external_preset`), so it prints the same. The preset menu shows OrcaSlicer's name. brand-check lists them as warnings. |

## Their command lines

brand-check runs each slicer headless. Some need help that their apps don't:

- **Creality Print 7.3** opens its window unless an argument is `--cli` (`src/CrealityPrint.cpp`,
  `parse_application_arguments`), and writes `plate_N.gcode` to `--outputdir` only with `--need-gcode-file`. Its
  command line refuses any project whose major version isn't its own (7) without `--allow-newer-file`
  (`SliceCommand.cpp`); the app has no such rule.
- **QIDI Studio**'s command line rearranges every 3MF not stamped `QIDIStudio-…`, merging the plates
  (`QIDIStudio.cpp`, `need_arrange`). Its app keeps the plates. brand-check passes `--arrange 0`. The files are
  not restamped: OrcaSlicer would then load only their geometry.
- **OrcaSlicer 2.4.2** stamps its projects `BambuStudio-02.06.00.51` and its own command line then refuses them
  as newer than itself, so SlicerBridge slices with `--allow-newer-file`.

## Adding a brand slicer

Add it to `BRAND_SLICERS` in `slicerbridge/printers.py`, keyed by the vendor folder its printers' presets live
in, with any arguments its command line needs. Then run `slicerbridge brand-check` on files for its printers, and
document what you find here.

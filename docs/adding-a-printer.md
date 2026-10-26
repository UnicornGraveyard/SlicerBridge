# Adding a printer

## OrcaSlicer-family or Bambu Lab printers

1. Find the printer's presets in its slicer's bundled profiles
   (`OrcaSlicer.app/Contents/Resources/profiles/<Vendor>/machine|process|filament/*.json`): the printer preset,
   the print preset closest to 0.16 mm layers, and a filament preset per type (PLA, PETG, ABS).
2. Add an entry to `PRINTERS` in `slicerbridge/printers.py`: label (as it should read in file names), vendor
   folder, those presets, how many colors its filament unit or toolheads hold, and `"bambu"` as the slicer for a
   Bambu Lab printer. Add it to a set if most owners will want it.
3. Build its template and convert a test project:
   ```bash
   slicerbridge templates --printers <key>
   slicerbridge convert some-project.3mf --printers <key> --slice
   ```
   The template build refuses presets that stop for a manual filament swap (`PAUSE`, `M600`): that printer
   isn't a multicolor printer to SlicerBridge.
4. If its maker ships a slicer, run `slicerbridge brand-check` on the file too (see
   [brand-slicers.md](brand-slicers.md)).
5. Add its row to the README's table, and open a pull request with the commands' output.

## Prusa printers

Add a `PrusaPrinter` to `PRUSA_PRINTERS` with the PrusaSlicer printer, print and filament presets, and its
`printer_model` id and nozzle variant from PrusaSlicer's `PrusaResearch.ini`. Then
`slicerbridge templates --printers <key>` builds its profile.

## A new engine

A printer whose slicer reads neither Bambu-format nor PrusaSlicer projects needs an engine: a module in
`slicerbridge/engines/` with `convert(src, name, keys, out, prefix, slice_it)` returning
`(written, refused_or_failed)`, a check in `slicerbridge/checks/`, and its keys in `printers.engine_of`. Open an
issue first to talk it through.

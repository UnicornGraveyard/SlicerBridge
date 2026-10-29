---
description: Converts a multicolor 3MF project (from Bambu Studio, OrcaSlicer or a printer maker's fork of either) into a ready, checked file for other multicolor printers with the SlicerBridge tools. Use when someone wants a model to print on a different printer, asks to retarget or convert a 3MF, wants printer-named files for the people who download their model, or asks whether a model fits a printer.
---

# Convert a multicolor model for other printers

SlicerBridge writes one file per printer, named for that printer (`Dragon - Creality K2 Plus.3mf`), and
checks every file before it's written: colors, paint, plates, size and settings. A file that fails a check
is never written. Its tools are on the `slicerbridge` MCP server.

## The steps

1. **First time in a session, run `doctor`.** It shows which slicers SlicerBridge finds. Files for a printer
   are built with that printer's slicer: OrcaSlicer 2.4.2 for Creality, Elegoo, Anycubic, Flashforge and
   QIDI; Bambu Studio for Bambu Lab; PrusaSlicer for Prusa. The Snapmaker U1 needs none to convert, and
   Snapmaker Orca to slice. If one the user
   needs is missing, say so and point them at the plugin's settings (`/plugin`, then **Configure**), where
   they can set where each slicer is installed.
2. **Inspect before converting.** `inspect_model` shows the palette, the filament slots the model actually
   uses, each plate's size, and whether it fits each bed. Tell the user what matters: how many colors it
   uses, and any printer it won't fit at full size.
3. **Convert** with `convert_model`, for the printers the user wants (`list_printers` gives the keys, sets
   and vendors; `popular` is a good default). Ask for the model's name if the file name isn't what they'd
   want on downloads, and a `prefix` or `suffix` if they sell under a shop name (a `suffix` goes after the
   printer: `Dragon - Creality K2 Plus - My Shop.3mf`). Files go to `out/<name>/` beside the
   model unless they give `out_dir`.
4. **Report what came back**, printer by printer: what was written, what was refused and why, and every
   warning. Don't summarize a refusal away. A print over 100 mm says which prime tower each file got: a
   heavier one, the same on the printer's own ribs, or (with a ⚠) only a slower one because the bed has no
   room. Pass the ⚠ on: that file is the one to slice and look at.
5. **To prove the files print**, offer `verify_files` with slicing, or `convert_model` with `slice_first`.
   Each slice takes a minute or two per printer, so do a few printers at a time. `brand_check` slices
   Creality, Elegoo, Anycubic, Flashforge and QIDI files in those makers' own slicers, where installed.
   A slice reports the print time, grams and the prime tower's share of the filament. If Snapmaker Orca's
   command line crashes on a U1 file, the file is kept with a warning: suggest slicing it in the app.

## Rules

- **Never resize a model**, and never suggest skipping a printer because the model is big. A model bigger
  than a bed is written at full size with a warning and the scale that would fit; whoever prints it scales
  it in their slicer.
- **Never merge colors.** A model that uses more filaments than a printer holds is refused for that printer.
  Which colors to merge is the designer's decision, made in their own slicer. Prusa printers accept an
  explicit slot-to-tool map, but only from the user.
- **Don't retry a refusal with different settings to get past it.** Explain it instead.
- SlicerBridge reads projects from Bambu Studio, OrcaSlicer and their forks, and its own files for
  non-Prusa printers. A PrusaSlicer project or a mesh-only 3MF is refused: say what the message says to do.
- Run one tool at a time; the server does too.

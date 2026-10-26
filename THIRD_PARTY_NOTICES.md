# Third-party notices

SlicerBridge is licensed under the GNU General Public License v3 or later (LICENSE). It includes or builds on:

## Panda2Prusa (vendored)

`slicerbridge/_vendor/panda2prusa/`, from https://github.com/liutang/Panda2Prusa at commit
`b6cd13143fb5ea29b9111a11bd329c7e25f25b8d`. MIT License, Copyright (c) 2026 Joseph (CupsOhJoe); the full text
is in `slicerbridge/_vendor/panda2prusa/LICENSE`.

## bl2u1 (U1 templates)

`slicerbridge/data/u1/` (`u1_template.3mf`, `u1_template_supports.3mf`, and `filament_types.json`: the filament
types and preset names from bl2u1's `filament_types.3mf`, without the model it carries), and the U1
conversion logic in `slicerbridge/engines/u1.py`, from https://github.com/josuanbn/bl2u1 at the commit in
`slicerbridge/data/u1/BL2U1_COMMIT`. GNU GPL v3.

## Slicer profiles (not distributed)

Printer, process and filament presets are read at run time from the slicers you install: OrcaSlicer (AGPL v3),
Bambu Studio (AGPL v3), PrusaSlicer (AGPL v3) and the makers' forks. SlicerBridge ships none of them; the
settings it builds from them stay in your local cache.

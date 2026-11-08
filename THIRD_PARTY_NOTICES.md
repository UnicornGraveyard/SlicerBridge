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

## Snapmaker Orca's U1 filament presets (values)

`slicerbridge/data/u1/filaments.json`: the values one U1 extruder gets from fourteen of Snapmaker Orca 2.4.1's
bundled U1 filament presets (its Generic ABS, ASA, BVOH, PA, PA-CF, PC, PCTG, PETG, PETG-CF, PLA-CF, PVA and
TPU, Snapmaker PETG HF, and Snapmaker TPU 95A HF @U1 0.4 nozzle), each flattened through its inherits chain by
`scripts/u1_filaments.py`, so a U1 file is made without Snapmaker Orca installed. Snapmaker Orca is a fork of
OrcaSlicer, AGPL v3.

## Slicer profiles (not distributed)

Printer, process and filament presets are read at run time from the slicers you install: OrcaSlicer (AGPL v3),
Bambu Studio (AGPL v3), PrusaSlicer (AGPL v3) and the makers' forks. Apart from the U1 filament values above,
SlicerBridge ships none of them; the settings it builds from them stay in your local cache.

## Unicorn Graveyard logo

`plugin/.claude-plugin/icon.png` is the Unicorn Graveyard logo, the plugin's icon in the Claude directory. It is
a mark of Unicorn Graveyard, all rights reserved, and is not licensed under the GPL: forks should use their own
icon.

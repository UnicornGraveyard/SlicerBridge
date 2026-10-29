# The checks

Every file passes these before it's written. The paint decoder the checks use is written from the format,
separately from the conversion code, so the conversion never checks its own work.

## orca engine (Bambu Lab and OrcaSlicer-family printers)

1. Not a white blob: the palette isn't all white.
2. Printer: the printer and print presets are that printer's.
3. Colors: one filament per used slot, no more than the printer holds, each the printer's preset for its type.
4. Paint present.
5. Palette: filament k is the color of the k-th used source slot.
6. Every paint state and extruder number is a filament the file has, and the part/object extruders are the
   source's, mapped.
7. Painted faces in == out.
8. Paint leaves per filament in == out after the mapping: the colors are on the right filaments.
9. On the bed: every printed vertex inside its own plate and under the max height, clear of exclusion areas. A
   model bigger than the bed gets a warning with the scale that fits; a prime tower that may touch the model or
   the bed's edge gets a warning too.
10. Version stamp: the slicer's own, from the printer's template.
11. Size unchanged: each plate measures as in the source, to 0.01 mm.
12. Settings complete: every per-filament setting has one value per filament, the flush matrix is K x K per
    nozzle, the bed type is the one the slicer's app picks, and Bambu's slice leftovers are gone. And it reads
    the same in the brands' own slicers ([brand-slicers.md](brand-slicers.md)): line widths in mm, an exclusion
    area both readings agree on, the Kobra X's ACE block guarded.

With `--slice` (or `slicerbridge verify --slice`):

13. The printer's slicer slices every plate; every color change is a tool change the printer makes itself (no
    `PAUSE`, no `M600`); the prime tower it actually printed stays clear of the model. A plate bigger than the
    bed is sliced as a scaled copy, the way a downloader would print it. A slice still running after 20 minutes
    is a failure, and a slicer crash gets one more try. The result says the slicer's time and grams, and what
    share of the filament laid goes into the prime tower.

And whatever the source:

14. A plate taller than 100 mm carries at least the heavier prime tower tall prints get
    ([tall.py](../slicerbridge/tall.py)), in the slicer's own names: brim 10 mm, extra rib length 20 mm, rib
    width 12 mm, purge spacing 150%, tower speed 60 mm/s at most. Where that wider base wouldn't fit at the
    file's tower spot (it would touch the model or leave the bed), the brim, spacing and speed are required on
    the file's own ribs; where that wouldn't fit either, only the slower tower, with a warning. The minimums are
    written out in the check, not taken from the engine.

## u1 engine (Snapmaker U1)

1. Not a white blob (unsliced Bambu projects tricked earlier converters into 4 white filaments).
2. The printer is the U1. 3. At most 4 filaments. 4. Paint present.
5-8. Palette, extruder numbers, painted faces and paint leaves per filament, as above.
9. Every printed vertex on its own U1 plate and under the max height. A prime tower whose base (its brim and the
   reach of its rib wall) may touch the model gets a warning.
10. The version stamp is the U1 template's. 11. Size unchanged.
12. A plate taller than 100 mm carries at least the heavier prime tower the engine gives tall prints: brim
    10 mm, extra rib length 20 mm, rib width 12 mm, purge spacing 150%, tower speed 60 mm/s at most, rib wall.
    Where that wider base wouldn't fit at the file's tower spot (it would touch the model or leave the bed), the
    brim, spacing and speed are required on the file's own ribs; where that wouldn't fit either, only the slower
    tower, with a warning to slice it in Snapmaker Orca and look. The minimums are written
    out in the check, not taken from the engine.

With `--slice`, check 13 as above, in Snapmaker Orca. Its command line refuses every project as newer than itself,
its own included, so it's given `--allow-newer-file`. Without Snapmaker Orca the file is still written, unsliced,
with a warning. So is a file its command line crashes on, twice: its app slices files the command line crashes on
(Snapmaker Orca 2.4.0 on Gatefang). A file it refuses is not written.

## prusa engine (Prusa printers)

Each per-plate PrusaSlicer project is checked for:

1. A single flat `3D/3dmodel.model`, as PrusaSlicer writes it.
2. All paint converted to PrusaSlicer's `mmu_segmentation`.
3. No more tools than the printer has, and the objects the plate should hold.
4. Painted faces in == out.
5. Paint leaves per tool in == out after the mapping.
6. Modifier and negative parts kept, as PrusaSlicer's volume types.
7. The config's filament colors are the mapped palette.
8. On the bed, clear of its edges.
9. PrusaSlicer loads it (`--info`).
10. With `--slice`, PrusaSlicer slices it.
11. Size unchanged.

A plate taller than 100 mm on a printer with a wipe tower (the MMU3, the XL) gets a 10 mm tower brim and 150%
purge spacing where every plate's tower finds a spot for the wider brim, and a warning where it doesn't.
PrusaSlicer's tower has no ribs or speed of its own.

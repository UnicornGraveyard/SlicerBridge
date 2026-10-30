# How SlicerBridge works

Bambu Studio, OrcaSlicer and the printer makers' forks of them (Creality Print, ElegooSlicer, Anycubic Slicer
Next, Flash Studio, QIDI Studio) all save the same 3MF project format: a zip holding the model
(`3D/3dmodel.model` and the meshes in `3D/Objects/`), its objects, parts and plates
(`Metadata/model_settings.config`), and every setting (`Metadata/project_settings.config`). Paint is stored on
each triangle as a `paint_color` string, a tree of sub-triangles each naming a filament.

So whichever of them saved a project, SlicerBridge reads it the same way, once, and writes one file per printer
through one of three engines. Its own files are that format too (Prusa files aside), so any of them can go back
in. PrusaSlicer projects are a different format, and aren't read yet.

## Colors

What counts is the filament slots a model **paints or assigns**, never the size of its palette: a project with a
15-color palette may use 4. The used slots are renumbered 1..K in slot order (slots 1, 3, 5 become 1, 2, 3), and
every `paint_color` is re-encoded to match ([Panda2Prusa](https://github.com/liutang/Panda2Prusa)'s
`transform_states`), so the paint follows its filament. Part, object and feature filaments (supports in one
color, say) are renumbered the same way.

More used slots than the printer holds is a refusal, not a merge: which two colors become one is a design
decision, for whoever made the model, in their slicer. Each filament gets the printer's own preset for its type (PLA, PETG, ABS); another type is refused.

## Templates

For each printer and filament type, the printer's own slicer opens a 20 mm cube with the printer, print and
filament presets from its bundled profiles, and saves the project. That project's settings are complete: every
key that slicer would save, every default. A file's settings are composed from those, one filament column per
color, never guessed.

Slicers' command lines don't load presets the way their apps do, so each preset is first flattened along its
`inherits` chain, and the saved project is compared key by key with the presets (more than 3 differences fails
the build). The bed type is the one the slicer's app picks for that printer; Bambu printers keep the designer's.

## Your settings

The print settings that shape the print (layer height, walls and shells, infill, surface patterns, seams, supports
and rafts, brim and skirt, ironing, fuzzy skin) are carried over where the target has the same setting, and so is
anything else the source project marks as changed from its preset, speeds and accelerations included. Bambu Studio
marks a setting changed against the preset's system parent, so a detached or imported preset marks nothing: the
settings that shape the print are carried whatever it marks ([carry.py](../slicerbridge/carry.py)). Speeds,
accelerations and line widths are tuned to the source's printer, and stay the target's unless marked. Prusa files
take what PrusaSlicer has a setting for, and the Snapmaker U1 the same as the others. Then the target's slicer
reads the whole file back: a carried
value it doesn't take keeps the printer's own, and the output says so. The U1's file has no slicer to read it
back while it's written, so a carried -1 (Bambu Studio's auto, which some Snapmaker Orca versions won't load)
keeps the template's value, and with `--slice` any setting Snapmaker Orca won't load keeps the template's and
the file is sliced again. A value the printer owns (its prime tower, purge volumes, machine limits) is never
carried.

## Tall prints

The prime tower climbs as long as tools swap, and a tall, thin tower is easy for the nozzle to knock over. A
plate taller than 100 mm gets a heavier tower ([tall.py](../slicerbridge/tall.py)): a 10 mm brim, ribs 20 mm
longer and 12 mm wide, purge lines spaced 150%, and the tower printed at 60 mm/s at most, in each slicer's own
setting names. These are minimums: a printer whose tower is already heavier keeps its own. The wider base needs
room, so each file gets the heaviest of three steps its plates have room for: the whole heavier tower; the same
brim, spacing and speed on the printer's own ribs; or only the slower tower, with a warning to slice it and
look. PrusaSlicer's wipe tower has no ribs or speed of its own, so Prusa files get the brim and spacing.

## Plates and size

Bambu Studio and its forks lay plates out 1.2 beds apart. Each plate is moved onto its own plate of the target
bed, turned 90 degrees only if that is what makes it fit, kept clear of the bed's exclusion areas and of the
edge its brim and skirt need, with room for a prime tower beside it. Its size never changes. A plate bigger than
the bed is centered at full size with a warning and the scale that would fit.

## Engines

- **orca** (Bambu Lab and OrcaSlicer-family printers): the steps above, written as a Bambu-format project.
- **prusa**: Panda2Prusa writes a PrusaSlicer project per plate (PrusaSlicer has no plates), with the printer's
  PrusaSlicer profile and the wipe tower placed beside the model.
- **u1**: the Snapmaker U1 project, from [bl2u1](https://github.com/josuanbn/bl2u1)'s U1 templates, stamped
  with the template's own version so Snapmaker Orca opens it without complaint. With `--slice`, Snapmaker Orca
  slices it; when its command line crashes (2.4.0's did, on a file its app and 2.4.1's command line slice),
  the file is kept with a warning to slice it in the app.

## Checks

Every file is audited before it's written; one that fails is never written, and an earlier file for the same
model and printer is taken back. See [checks.md](checks.md).

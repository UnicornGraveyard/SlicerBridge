# Changelog

## 0.1.1 (2026-10-26)

- Any to any: projects saved by OrcaSlicer, QIDI Studio, Anycubic Slicer Next, ElegooSlicer and Flash Studio are
  tested as input, as are SlicerBridge's own files. The docs no longer say "Bambu Studio" for the input.
- Fixed: a project saved by QIDI Studio or Anycubic Slicer Next made a U1 file without a version stamp, which
  its checks then refused.
- A PrusaSlicer project or a mesh-only 3MF is refused with what to do, instead of an error.
- Converting a file SlicerBridge wrote names the model without the printer ("Cube", not "Cube - QIDI Q2").

## 0.1.0 (2026-10-26)

First public release.

- 30 printers through three engines: Bambu Lab and the OrcaSlicer family (Creality, Elegoo, Anycubic,
  Flashforge, QIDI), Prusa (MMU3, XL, Core One INDX), and the Snapmaker U1.
- `slicerbridge` CLI: `convert`, `inspect`, `printers`, `verify`, `brand-check`, `templates`, `doctor`.
- Every file audited before it's written; optional slicing in the printer's slicer and in its maker's own slicer.
- Files written so each maker's own slicer reads them as OrcaSlicer does: line widths in mm (QIDI Studio), an
  exclusion area Anycubic Slicer Next reads the same way (Kobra 3), the Kobra X's ACE block guarded.
- `slicerbridge-mcp`: a local MCP server over the same commands.

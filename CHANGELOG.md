# Changelog

## 0.1.0 (2026-10-26)

First public release.

- 30 printers through three engines: Bambu Lab and the OrcaSlicer family (Creality, Elegoo, Anycubic,
  Flashforge, QIDI), Prusa (MMU3, XL, Core One INDX), and the Snapmaker U1.
- `slicerbridge` CLI: `convert`, `inspect`, `printers`, `verify`, `brand-check`, `templates`, `doctor`.
- Every file audited before it's written; optional slicing in the printer's slicer and in its maker's own slicer.
- Files written so each maker's own slicer reads them as OrcaSlicer does: line widths in mm (QIDI Studio), an
  exclusion area Anycubic Slicer Next reads the same way (Kobra 3), the Kobra X's ACE block guarded.
- `slicerbridge-mcp`: a local MCP server over the same commands.

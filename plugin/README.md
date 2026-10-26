# SlicerBridge for Claude

Convert a multicolor 3MF project, saved by Bambu Studio, OrcaSlicer or a printer maker's own fork of either,
into a ready, checked file for each of 30 multicolor printers: Bambu Lab, Creality, Elegoo, Anycubic,
Flashforge, QIDI, Prusa and the Snapmaker U1. Each file is named for its printer, keeps the colors where they
were painted, carries that printer's own settings, and keeps its size. A file that fails a check is never
written.

This plugin gives Claude SlicerBridge's tools and a skill for using them: inspect a model, convert it for the
printers you name, re-check or slice the files, and slice them in each maker's own slicer.

## Requirements

- macOS (Linux and Windows aren't tested yet)
- [uv](https://docs.astral.sh/uv/), which runs the server
- The slicers for the printers you want: OrcaSlicer 2.4.2 (Creality, Elegoo, Anycubic, Flashforge, QIDI),
  Bambu Studio (Bambu Lab), PrusaSlicer (Prusa). The Snapmaker U1 needs none.

SlicerBridge looks for each slicer in `/Applications`. If yours is elsewhere, set its location in the
plugin's settings: `/plugin`, select SlicerBridge, **Configure**. Leave a setting empty to use
`/Applications`.

## What it runs, reads and sends

- It starts its MCP server with `uvx slicerbridge==<version> mcp`, the version in the plugin's manifest, which
  downloads that exact version of the [slicerbridge](https://pypi.org/project/slicerbridge/) package from PyPI
  the first time. The plugin's `uv.lock` pins it and everything it needs to exact versions with their hashes;
  Claude Code versions that launch plugins from their lock install exactly that set.
- It runs the command lines of the slicers installed on your computer, to build each printer's settings and
  to slice files when you ask it to.
- It reads the 3MF files you name and writes the files it makes into the folder you name, or into `out/`
  beside the model. It keeps each printer's settings in `~/Library/Caches/SlicerBridge`.
- It sends nothing anywhere and collects nothing: no telemetry, no accounts, no uploads.

Source and documentation: [github.com/UnicornGraveyard/SlicerBridge](https://github.com/UnicornGraveyard/SlicerBridge).
Support: [issues](https://github.com/UnicornGraveyard/SlicerBridge/issues).
Privacy: [docs/privacy.md](https://github.com/UnicornGraveyard/SlicerBridge/blob/main/docs/privacy.md).
License: GPL v3 or later.

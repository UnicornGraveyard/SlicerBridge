# Changelog

## Unreleased

- Windows: the slicers are found in their installers' usual folders, and each one's version is read from its
  `.exe`. Converting and slicing for Bambu Lab printers is tried end to end with Bambu Studio 2.8.2. CI runs the
  tests and the plugin on Windows too.
- Fixed on Windows: output with ⚠ or ✓ crashed when piped or redirected, and slicer profiles and logs were read
  in the system's code page instead of UTF-8. A slicer crash is now recognized and retried there, as on macOS.
- A model name or prefix with `< > : " / \ | ? *` is written with `-` in their place, so its files open on
  Windows wherever they were made.

- The Claude plugin ships a `uv.lock` beside its `uvx slicerbridge==<version>` launcher: SlicerBridge and every
  package it needs at exact PyPI versions with their hashes, which Anthropic's directory checks for its Verified
  badge.
- After each release, `python scripts/release.py plugin <version>` moves the plugin to it and relocks, so the
  plugin only ever names a version that's on PyPI.
- The plugin has an icon: the Unicorn Graveyard logo, which isn't covered by the GPL.

## 0.2.0 (2026-10-26)

- On PyPI: `pipx install slicerbridge`, or `uvx slicerbridge` without installing.
- A Claude plugin (`plugin/`): the MCP server plus a skill that teaches Claude the workflow, with settings for
  where each slicer is installed. Install it with `/plugin marketplace add UnicornGraveyard/SlicerBridge`.
- In the MCP Registry as `io.github.UnicornGraveyard/slicerbridge`.
- `slicerbridge mcp` starts the MCP server; the MCP SDK is now a regular dependency (`slicerbridge[mcp]`
  still installs).
- Releases are a tag: `.github/workflows/release.yml` publishes to PyPI, then the MCP Registry, then GitHub.

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

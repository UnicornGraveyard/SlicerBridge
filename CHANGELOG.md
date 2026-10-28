# Changelog

## Unreleased

- Your print settings reach every file, not the printer's defaults. Settings were carried only if the project
  marked them as changed, and Bambu Studio marks nothing for a process preset with no system parent (detached,
  imported, or from a MakerWorld profile), so supports, brim, ironing and the like came out as each printer's
  default. Now every setting that shapes the print (layers, walls and shells, infill, surface patterns, seams,
  supports and rafts, brim and skirt, ironing, fuzzy skin) is carried whatever the project marks; speeds,
  accelerations and line widths still only when marked. The Snapmaker U1 file carried none of them, only the
  template's: it carries them now, and prints supports whenever the source does.
- Four more printers that change nozzles or toolheads: the Bambu Lab X2D and H2D Pro (two nozzles, written by
  Bambu Studio), and the Flashforge Creator 5 Pro and WonderMaker ZR Ultra S (four toolheads, written by
  OrcaSlicer). 34 printers in all. On Windows, OrcaSlicer 2.4.2's command line crashes opening any
  4-toolhead project, so the Creator 5, the 5 Pro and the ZR Ultra S are written on a Mac.

## 0.3.1 (2026-10-27)

- `convert --suffix "My Shop"` (and the MCP tool's `suffix`) names files `<name> - <printer> - My Shop.3mf`:
  model, printer, maker. `verify`, `brand-check` and converting a file again read the printer from either
  form, and a file's default name is what comes before its printer.

## 0.3.0 (2026-10-27)

- The Claude plugin ships a `uv.lock` beside its `uvx slicerbridge==<version>` launcher: SlicerBridge and every
  package it needs at exact PyPI versions with their hashes, which Anthropic's directory checks for its Verified
  badge.
- After each release, `python scripts/release.py plugin <version>` moves the plugin to it and relocks, so the
  plugin only ever names a version that's on PyPI.
- The plugin has an icon: the Unicorn Graveyard logo, which isn't covered by the GPL.
- Windows works ([docs/install.md](docs/install.md#windows)): the slicers are found in their installers' usual
  folders and each one's version is read from its `.exe`; the CLI hands the slicers absolute paths and writes
  UTF-8; every text file and slicer log is read as UTF-8 (a profile with an accented byte, like OrcaSlicer's
  Kobra X, was skipped as missing); and a slicer crash there is recognized and retried, as on macOS. CI runs the
  tests and the plugin on Windows too.
- A model name or prefix with `< > : " / \ | ? *` is written with `-` in their place, so its files open on
  Windows wherever they were made.
- One printer failing (a slicer that hangs or crashes, a bug) no longer ends the run: that printer fails and the
  rest of its brand, and the other engines, still get their files.
- The prime tower goes where the printer can print it: where every nozzle of an H2D or H2C reaches; sized as
  the slicers print it (Bambu Studio's square rib-walled towers, auto brims, flush towers, walls); unturned; clear
  of exclusion areas and of the skirt's reach. When no strip beside the model's box holds it, it goes where the
  model's outline from above leaves room, and a crowded auto brim drops to 3 mm. A tower on the outline, not
  just in the box, fails the check.
- The slice check reports Bambu Studio's reason from `result.json`, and a tower conflict or an unprintable-area
  exit slices a scaled copy, as a model bigger than the bed does, with a warning.

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

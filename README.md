# SlicerBridge

**Any multicolor 3MF project in. A ready, checked file for every multicolor printer out.**

<!-- mcp-name: io.github.UnicornGraveyard/slicerbridge -->

[![CI](https://github.com/UnicornGraveyard/SlicerBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/UnicornGraveyard/SlicerBridge/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

You paint a multicolor model in one slicer. The people who print it own a Bambu Lab A1, a Creality K2, an
Elegoo Centauri Carbon, a QIDI Q2, a Prusa XL, a Snapmaker U1… and each of them has to retarget your project to
their printer by hand, guessing at presets, and often losing the colors on the way.

SlicerBridge takes the project from whichever slicer you made it in (Bambu Studio, OrcaSlicer, or a printer
maker's own fork of either) and writes one file for each of 34 printers, named for that printer
(`My Model - Creality K2 Plus.3mf`), so a downloader can open theirs and print. Its files for Bambu Lab,
OrcaSlicer-family and U1 printers can go back in, for yet another printer. Every file:

- **keeps the colors where you painted them.** The filaments a model actually uses are kept in order, and every
  painted triangle is re-encoded to match.
- **carries that printer's own settings**, written by its own slicer, and that printer's filament presets. Your
  print settings (layer height, walls, infill, supports, brim, ironing and the rest that shape the print) come
  along where the printer has the same setting, whether or not your project marks them as changed.
- **keeps its size.** Nothing is ever scaled. A model bigger than a bed gets a warning with the scale that fits.
- **is checked before it's written**: colors, paint, plates, size and settings, and optionally sliced in that
  printer's slicer to prove automatic color changes and a prime tower clear of the model. A file that fails a
  check is never written.

## What goes in

A multicolor `.3mf` **project**, painted or with colors per part or object, saved by any of these. They all
write the same project format.

| Saved by | |
|---|---|
| Bambu Studio | tested (2.8) |
| OrcaSlicer | tested (2.4.2) |
| QIDI Studio, Anycubic Slicer Next, ElegooSlicer, Flash Studio | tested with projects each of them saved |
| SlicerBridge | its files for Bambu Lab, OrcaSlicer-family and U1 printers, to retarget them again |
| Creality Print | should work, not yet tested: its command line can't save a project ([tell us](https://github.com/UnicornGraveyard/SlicerBridge/issues/new?template=slicer_compat.yml)) |
| PrusaSlicer | not yet: PrusaSlicer projects are a different format |

A mesh-only 3MF (no slicer project inside) is refused with what to do: open it in your slicer, give it its
colors, and save the project.

## What comes out

| Printer | Key | Colors | File built for | Brand slicer checked |
|---|---|---|---|---|
| Bambu Lab A1 mini | `a1mini` | 4 | Bambu Studio | — |
| Bambu Lab A1 | `a1` | 4 | Bambu Studio | — |
| Bambu Lab P1S | `p1s` | 16 | Bambu Studio | — |
| Bambu Lab P2S | `p2s` | 16 | Bambu Studio | — |
| Bambu Lab X1 Carbon | `x1c` | 16 | Bambu Studio | — |
| Bambu Lab H2S | `h2s` | 16 | Bambu Studio | — |
| Bambu Lab H2D | `h2d` | 16 | Bambu Studio | — |
| Bambu Lab H2D Pro | `h2dpro` | 16 | Bambu Studio | — |
| Bambu Lab H2C | `h2c` | 16 | Bambu Studio | — |
| Bambu Lab X2D | `x2d` | 16 | Bambu Studio | — |
| Creality K2 Plus | `k2plus` | 4 | OrcaSlicer | Creality Print |
| Creality K2 Pro | `k2pro` | 4 | OrcaSlicer | Creality Print |
| Creality K2 SE | `k2se` | 4 | OrcaSlicer | Creality Print |
| Creality Hi | `hi` | 4 | OrcaSlicer | Creality Print |
| Creality K1C CFS | `k1c` | 4 | OrcaSlicer | Creality Print |
| Elegoo Centauri Carbon | `cc` | 4 | OrcaSlicer | ElegooSlicer |
| Anycubic Kobra S1 | `kobras1` | 4 | OrcaSlicer | Anycubic Slicer Next |
| Anycubic Kobra S1 Max | `kobras1max` | 4 | OrcaSlicer | Anycubic Slicer Next |
| Anycubic Kobra 3 | `kobra3` | 4 | OrcaSlicer | Anycubic Slicer Next |
| Anycubic Kobra 3 Max | `kobra3max` | 4 | OrcaSlicer | Anycubic Slicer Next |
| Anycubic Kobra X | `kobrax` | 4 | OrcaSlicer | Anycubic Slicer Next |
| Flashforge AD5X | `ad5x` | 4 | OrcaSlicer | Flash Studio |
| Flashforge Creator 5 | `creator5` | 4 | OrcaSlicer | Flash Studio |
| Flashforge Creator 5 Pro | `creator5pro` | 4 | OrcaSlicer | Flash Studio |
| QIDI Q2 | `q2` | 4 | OrcaSlicer | QIDI Studio |
| QIDI Q2C | `q2c` | 4 | OrcaSlicer | QIDI Studio |
| QIDI Plus4 | `plus4` | 4 | OrcaSlicer | QIDI Studio |
| QIDI Max4 | `max4` | 4 | OrcaSlicer | QIDI Studio |
| WonderMaker ZR Ultra S | `zrultras` | 4 | OrcaSlicer | — |
| Prusa MMU3 | `mmu3` | 5 | PrusaSlicer | — |
| Prusa XL | `xl` | 5 | PrusaSlicer | — |
| Prusa Core One INDX 4T | `indx4` | 4 | PrusaSlicer | — |
| Prusa Core One INDX 8T | `indx8` | 8 | PrusaSlicer | — |
| Snapmaker U1 | `u1` | 4 | Snapmaker Orca | — |

"File built for" is the slicer whose settings the file carries. Files for Creality, Elegoo, Anycubic, Flashforge
and QIDI printers are written by OrcaSlicer and also checked in each maker's own slicer: see
[docs/brand-slicers.md](docs/brand-slicers.md) for what those slicers read differently, and how the files handle it.
Missing your printer? [Ask for it](https://github.com/UnicornGraveyard/SlicerBridge/issues/new?template=printer_request.yml),
or [add it](docs/adding-a-printer.md).

## Install

SlicerBridge runs on **macOS** and **Windows** ([what's different there](docs/install.md#windows); Linux:
[help wanted](docs/install.md#linux)), with Python 3.10 or newer.

```bash
pipx install slicerbridge                                    # or: uv tool install slicerbridge
pipx install git+https://github.com/UnicornGraveyard/SlicerBridge   # the latest from GitHub
```

It builds each printer's settings with that printer's slicer, so install the ones you need:

| To write files for | Install |
|---|---|
| Bambu Lab printers | [Bambu Studio](https://bambulab.com/en/download/studio) |
| Creality, Elegoo, Anycubic, Flashforge, QIDI, WonderMaker | [OrcaSlicer 2.4.2](https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.4.2) |
| Prusa printers | [PrusaSlicer](https://www.prusa3d.com/page/prusaslicer_424/) |
| Snapmaker U1 | nothing: its templates are built in ([Snapmaker Orca](https://www.snapmaker.com/snapmaker-orca) to `--slice` it) |

Then `slicerbridge doctor` shows what it found. Details, other install locations and environment variables:
[docs/install.md](docs/install.md).

## Use

```bash
slicerbridge inspect model.3mf                       # colors used, plates, fit on each bed
slicerbridge convert model.3mf                       # the popular printers, into out/<model>/
slicerbridge convert "Dragon - Creality K2 Plus.3mf" --printers bambu   # retarget a file you already have
slicerbridge convert model.3mf --printers all --slice --name "My Model" --prefix "My Shop - "
slicerbridge convert model.3mf --name "My Model" --suffix "My Shop"   # My Model - Prusa XL - My Shop.3mf
slicerbridge convert model.3mf --printers creality,qidi,u1
slicerbridge printers                                # every printer, its key, engine and slicer
slicerbridge verify "out/My Model" --source model.3mf --slice
slicerbridge brand-check "out/My Model"              # slice each file in its maker's own slicer
```

`--printers` takes `all`, `popular`, a vendor (`bambu`, `creality`, `elegoo`, `anycubic`, `flashforge`, `qidi`,
`wondermaker`, `prusa`, `snapmaker`), or printer keys, comma-separated.

### What it won't do

- **Merge colors.** A model that uses more filaments than a printer holds is refused for that printer. Which two
  colors to merge is a design decision; make it in your slicer. (Prusa printers take an explicit `--map`.)
- **Resize anything.** Size is never a reason to skip a printer either: the downloader scales it in their slicer.
- **Write a file that failed a check**, or overwrite a file it didn't write (those move to `_previous/`).

## Use it from Claude or another AI assistant

SlicerBridge includes a local [MCP](https://modelcontextprotocol.io) server, so an assistant can inspect a
model, convert it for the printers you name, and read the check results for you. It runs on your machine over
stdio, with your slicers: nothing is uploaded.

**Claude Code and Cowork:** install the SlicerBridge plugin, which adds the server and a skill that teaches
Claude the workflow. It needs [uv](https://docs.astral.sh/uv/).

```text
/plugin marketplace add UnicornGraveyard/SlicerBridge
/plugin install slicerbridge@slicerbridge
```

**Any other MCP client:** run `uvx slicerbridge mcp` (or `slicerbridge mcp` once installed). Setup for Claude
Desktop and others: [docs/mcp.md](docs/mcp.md). It's also listed in the
[MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.UnicornGraveyard/slicerbridge`.

## How it works

Every input is read the same way: Bambu Studio, OrcaSlicer and their forks save the same 3MF project format
(meshes, paint, plates, settings). Three engines write the outputs, sharing one printer table, one CLI and one
set of checks:

- **orca**: Bambu Lab and OrcaSlicer-family printers. Each printer's slicer writes its settings once, from its
  own bundled presets ([templates](docs/how-it-works.md#templates)). A file is the source project with those
  settings, its filaments compacted and its paint re-encoded, each plate moved onto its own plate of the new bed
  with room for the prime tower.
- **prusa**: Prusa multi-material printers, written by [Panda2Prusa](https://github.com/liutang/Panda2Prusa) as
  PrusaSlicer projects, one per plate.
- **u1**: the Snapmaker U1, with [bl2u1](https://github.com/josuanbn/bl2u1)'s U1 templates.

[docs/how-it-works.md](docs/how-it-works.md) explains each step; [docs/checks.md](docs/checks.md) lists every check.

## Contributing

Bug reports, printer requests and pull requests are welcome: start with [CONTRIBUTING.md](CONTRIBUTING.md).
Please don't attach paid or licensed models to issues; the test suite builds its own projects.

## License and credits

SlicerBridge is free software under the [GNU GPL v3 or later](LICENSE). It stands on
[Panda2Prusa](https://github.com/liutang/Panda2Prusa) (MIT, vendored) and
[bl2u1](https://github.com/josuanbn/bl2u1) (GPLv3, its U1 templates), and on the work of the OrcaSlicer,
Bambu Studio and PrusaSlicer projects, whose profiles it reads from your own installs. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Made by [Unicorn Graveyard](https://unicorngraveyard.com), who needed every ColorStacked model to reach every
printer.

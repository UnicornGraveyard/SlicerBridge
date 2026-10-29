# Installing SlicerBridge

## The package

Python 3.10 or newer, then either:

```bash
pipx install slicerbridge                                        # from PyPI: the CLI and the MCP server
pipx install git+https://github.com/UnicornGraveyard/SlicerBridge   # the latest from GitHub
uvx slicerbridge doctor                                          # or run it without installing
```

For Claude, the plugin is the easiest route: see [mcp.md](mcp.md).

or, to work on it:

```bash
git clone https://github.com/UnicornGraveyard/SlicerBridge && cd SlicerBridge
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]"     # Windows: .venv\Scripts\pip
```

Its dependencies are lxml and the MCP Python SDK. [Panda2Prusa](https://github.com/liutang/Panda2Prusa) is vendored at a pinned
commit (`slicerbridge/_vendor/panda2prusa/VENDORED.md`).

## The slicers

SlicerBridge never ships a slicer's profiles. Each printer's settings are written by that printer's own slicer,
from the presets bundled with your install, the first time you convert for it (or with
`slicerbridge templates`). Install the ones for the printers you want:

| Printers | Slicer | Looked for on macOS | Looked for on Windows | Override with |
|---|---|---|---|---|
| Bambu Lab | Bambu Studio | `/Applications/BambuStudio.app` | `C:\Program Files\Bambu Studio\bambu-studio.exe` | `SLICERBRIDGE_BAMBU_STUDIO` |
| Creality, Elegoo, Anycubic, Flashforge, QIDI, WonderMaker | OrcaSlicer **2.4.2** | `/Applications/OrcaSlicer.app` | `C:\Program Files\OrcaSlicer\orca-slicer.exe` | `SLICERBRIDGE_ORCASLICER` |
| Prusa | PrusaSlicer | `/Applications/PrusaSlicer.app`, `/Applications/Original Prusa Drivers/PrusaSlicer.app` | `C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe` | `SLICERBRIDGE_PRUSASLICER` |
| Snapmaker U1 | none | built in | built in | |

On Windows, point `SLICERBRIDGE_PRUSASLICER` at `prusa-slicer-console.exe`, not `prusa-slicer.exe`: the window
version prints nothing for SlicerBridge to read.

**OrcaSlicer 2.4.2.** SlicerBridge is tested with it. Earlier versions' profiles for the Creality K1C and the
Elegoo Centauri Carbon stop for a manual filament swap at every color change, which SlicerBridge refuses. To keep
another version as your everyday slicer, put 2.4.2 anywhere and point SlicerBridge at it:

```bash
export SLICERBRIDGE_ORCASLICER=~/Applications/OrcaSlicer-2.4.2.app
```

On Windows (PowerShell), for every new terminal:

```powershell
[Environment]::SetEnvironmentVariable("SLICERBRIDGE_ORCASLICER", "C:\Tools\OrcaSlicer-2.4.2\orca-slicer.exe", "User")
```

`slicerbridge doctor` shows what it found and warns about an older OrcaSlicer.

On a Mac with [Homebrew](https://brew.sh), a missing OrcaSlicer is installed the first time SlicerBridge needs it
(`brew install --cask orcaslicer`, into `/Applications`). That's Homebrew's current OrcaSlicer, which may be newer
than 2.4.2. It isn't done while `SLICERBRIDGE_ORCASLICER` is set: a path you chose is yours to fill.

### Brand slicers (optional)

`slicerbridge brand-check` slices each file in the slicer its printer's maker ships. It looks for them in
`/Applications` on macOS and their installers' usual folders on Windows, or wherever these say:

| Slicer | Looked for on Windows | Override with |
|---|---|---|
| Creality Print | `C:\Program Files\Creality\Creality Print <version>\CrealityPrint.exe` (the newest) | `SLICERBRIDGE_CREALITY_PRINT` |
| ElegooSlicer | `C:\Program Files\ElegooSlicer\elegoo-slicer.exe` | `SLICERBRIDGE_ELEGOOSLICER` |
| Anycubic Slicer Next | `C:\Program Files\AnycubicSlicer\AnycubicSlicerNext.exe`, or `AnycubicSlicerNext\` | `SLICERBRIDGE_ANYCUBIC_SLICER_NEXT` |
| Flash Studio (Orca-Flashforge) | `flash-studio.exe` or `flash studio.exe` in `C:\Program Files\Orca-Flashforge\` or `Flash Studio\` | `SLICERBRIDGE_FLASH_STUDIO` |
| QIDI Studio | `C:\Program Files\QIDIStudio\qidi-studio-console.exe`, or `qidi-studio.exe` | `SLICERBRIDGE_QIDI_STUDIO` |

The Windows folders come from each slicer's installer and haven't all been tried on a real install yet. If
`slicerbridge doctor` says "not found" for one you have, set its variable and tell us where it was.

Download each from its maker's official site or GitHub releases, and check the download's signature before you
open it.

## Where things are kept

Templates (each printer's settings, written by its slicer) and Prusa profiles live in a cache:
`~/Library/Caches/SlicerBridge` on macOS, `~/.cache/slicerbridge` on Windows and Linux (`C:\Users\<you>\.cache\slicerbridge`),
or `SLICERBRIDGE_CACHE`. They're
rebuilt by themselves when a slicer's version or a printer's presets change. Delete the folder to start over.

PrusaSlicer runs against a private settings folder inside the cache, never your own.

## Windows

SlicerBridge finds the slicers in their installers' usual folders (the tables above) and reads each one's
version from its `.exe`. A portable zip goes wherever you extract it, so point its variable at the `.exe`. CI
runs the test suite on Windows as well as macOS and Linux.

It has written and sliced files for 28 printers on Windows 11 with Python 3.13, using:

| Slicer | Where |
|---|---|
| Bambu Studio 2.8.2 | `C:\Program Files\Bambu Studio\bambu-studio.exe`, found by itself |
| OrcaSlicer 2.4.2 | `orca-slicer.exe` in the extracted portable zip, via `SLICERBRIDGE_ORCASLICER` |
| PrusaSlicer | `prusa-slicer-console.exe` in the extracted portable zip, via `SLICERBRIDGE_PRUSASLICER` |

What's different there:

- **The 4-toolhead toolchangers aren't written: the Flashforge Creator 5 and 5 Pro and the WonderMaker ZR
  Ultra S.** OrcaSlicer 2.4.2's command line for Windows crashes opening any project for them, even one it
  saved itself, so their settings can't be read back. Their templates build; write their files on a Mac.
- **The Prusa CORE One INDX needs a newer Prusa bundle** than PrusaSlicer 2.9.6 ships (2.4.14). A PrusaSlicer
  that has never run hasn't downloaded one: start it once, or copy `PrusaResearch.ini` 2.5.0 or newer from
  [PrusaSlicer-settings-prusa-fff](https://github.com/prusa3d/PrusaSlicer-settings-prusa-fff/tree/main/PrusaResearch)
  into the cache's `prusaslicer/vendor/` folder.
- Bambu Studio's command line now and then never exits while reading settings back; SlicerBridge gives it three
  minutes and one more try.
- **File names:** Windows can't hold `< > : " / \ | ? *` in one, so SlicerBridge writes those as `-` in every
  name it makes, on every platform, since files made on a Mac are opened on Windows too.

The brand slicers' Windows folders come from their installers and haven't been tried on a real install. If one
misbehaves, please [open an issue](https://github.com/UnicornGraveyard/SlicerBridge/issues) with
`slicerbridge doctor`'s output.

## Linux

The engines are plain Python and the U1 engine works anywhere. The rest needs a slicer's command line and its
profiles folder: point the environment variables above at the slicer's executable. For an extracted AppImage,
that's the binary inside it, with `resources/profiles` beside or above it. This isn't tested yet. If you try it,
please [open an issue](https://github.com/UnicornGraveyard/SlicerBridge/issues) with what happened.

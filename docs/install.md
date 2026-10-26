# Installing SlicerBridge

## The package

Python 3.10 or newer, then either:

```bash
pipx install git+https://github.com/ianmacdowell13-byte/SlicerBridge             # the CLI
pipx install "slicerbridge[mcp] @ git+https://github.com/ianmacdowell13-byte/SlicerBridge"  # and the MCP server
```

or, to work on it:

```bash
git clone https://github.com/ianmacdowell13-byte/SlicerBridge && cd SlicerBridge
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]"
```

Its only dependency is lxml. [Panda2Prusa](https://github.com/liutang/Panda2Prusa) is vendored at a pinned
commit (`slicerbridge/_vendor/panda2prusa/VENDORED.md`).

## The slicers

SlicerBridge never ships a slicer's profiles. Each printer's settings are written by that printer's own slicer,
from the presets bundled with your install, the first time you convert for it (or with
`slicerbridge templates`). Install the ones for the printers you want:

| Printers | Slicer | Looked for at | Override with |
|---|---|---|---|
| Bambu Lab | Bambu Studio | `/Applications/BambuStudio.app` | `SLICERBRIDGE_BAMBU_STUDIO` |
| Creality, Elegoo, Anycubic, Flashforge, QIDI | OrcaSlicer **2.4.2** | `/Applications/OrcaSlicer.app` | `SLICERBRIDGE_ORCASLICER` |
| Prusa | PrusaSlicer | `/Applications/PrusaSlicer.app`, `/Applications/Original Prusa Drivers/PrusaSlicer.app` | `SLICERBRIDGE_PRUSASLICER` |
| Snapmaker U1 | none | built in | |

**OrcaSlicer 2.4.2.** SlicerBridge is tested with it. Earlier versions' profiles for the Creality K1C and the
Elegoo Centauri Carbon stop for a manual filament swap at every color change, which SlicerBridge refuses. To keep
another version as your everyday slicer, put 2.4.2 anywhere and point SlicerBridge at it:

```bash
export SLICERBRIDGE_ORCASLICER=~/Applications/OrcaSlicer-2.4.2.app
```

`slicerbridge doctor` shows what it found and warns about an older OrcaSlicer.

### Brand slicers (optional)

`slicerbridge brand-check` slices each file in the slicer its printer's maker ships. It looks for them in
`/Applications`, or wherever these say:

| Slicer | Override with |
|---|---|
| Creality Print | `SLICERBRIDGE_CREALITY_PRINT` |
| ElegooSlicer | `SLICERBRIDGE_ELEGOOSLICER` |
| Anycubic Slicer Next | `SLICERBRIDGE_ANYCUBIC_SLICER_NEXT` |
| Flash Studio (Orca-Flashforge) | `SLICERBRIDGE_FLASH_STUDIO` |
| QIDI Studio | `SLICERBRIDGE_QIDI_STUDIO` |

Download each from its maker's official site or GitHub releases, and check the download's signature before you
open it.

## Where things are kept

Templates (each printer's settings, written by its slicer) and Prusa profiles live in a cache:
`~/Library/Caches/SlicerBridge` on macOS, `~/.cache/slicerbridge` elsewhere, or `SLICERBRIDGE_CACHE`. They're
rebuilt by themselves when a slicer's version or a printer's presets change. Delete the folder to start over.

PrusaSlicer runs against a private settings folder inside the cache, never your own.

## Linux and Windows

The engines are plain Python and the U1 engine works anywhere. The rest needs a slicer's command line and its
profiles folder. On Linux and Windows, point the environment variables above at the slicer's executable: for an
extracted AppImage, the binary inside it, with `resources/profiles` beside or above it. This isn't tested yet.
If you try it, please [open an issue](https://github.com/ianmacdowell13-byte/SlicerBridge/issues) with what
happened.

"""
brandcheck.py -- slice each file in the slicer its downloader uses.

    slicerbridge brand-check "out/My Model"                 # every file with a brand slicer installed
    slicerbridge brand-check "out/My Model" --printers q2,cc

`slicerbridge verify --slice` proves the files in OrcaSlicer and Bambu Studio.
Someone with a QIDI printer opens theirs in QIDI Studio, an Elegoo owner in
ElegooSlicer, and so on: each a fork with its own presets, version check and
bugs. This runs every orca-engine file through its brand's own slicer from the
command line (printers.BRAND_SLICERS), with the orca check's slice step (13):

  - The brand slicer's command line takes the file's version. If not, it is
    sliced with --allow-newer-file and the warning says so.
  - Every plate slices (one bigger than the bed as a scaled copy, as a
    downloader would scale it).
  - Each multicolor plate changes filament with tool changes the printer makes
    itself (no PAUSE or M600), and the prime tower that slicer printed stays
    clear of the model.
  - Its printer, process and filament presets are named as that app names its
    own. The files carry OrcaSlicer's names, and the forks rename many (QIDI
    Studio's "Q2 0.4 nozzle" is OrcaSlicer's "Qidi Q2 0.4 nozzle"). A name the
    app doesn't have loads as a project preset holding the file's own settings
    (OrcaSlicer's PresetCollection::load_external_preset), so it prints the
    same; the preset menu just shows the file's name: a warning.

Bambu Lab files open in Bambu Studio, which `verify --slice` already uses.
Standard library only.
"""
from __future__ import annotations

import functools
import json
import zipfile
from pathlib import Path

from .checks import orca as verify
from .printers import BRAND_SLICERS, PRINTERS, Slicer, printer_of

KINDS = {"printer_settings_id": "machine", "print_settings_id": "process", "filament_settings_id": "filament"}


@functools.cache
def own_presets(slicer: Slicer) -> frozenset:
    """(kind, name) of every preset the slicer lists in its menus (base presets are hidden)."""
    found = set()
    for f in slicer.profiles.rglob("*.json"):
        kind = next((p for p in f.parts if p in KINDS.values()), None)
        try:
            d = json.loads(f.read_text()) if kind else None
        except (ValueError, OSError):
            continue
        if isinstance(d, dict) and d.get("name") and d.get("instantiation") == "true":
            found.add((kind, d["name"]))
    return frozenset(found)


def foreign_presets(f: Path, slicer: Slicer) -> list[str]:
    """The file's preset names the slicer doesn't list as its own."""
    with zipfile.ZipFile(f) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    names = []
    for key, kind in KINDS.items():
        v = cfg.get(key)
        names += [(kind, n) for n in dict.fromkeys(v if isinstance(v, list) else [v]) if n]
    return [f"{kind} {n!r}" for kind, n in names if (kind, n) not in own_presets(slicer)]



def check(f: Path) -> tuple[str | None, list[str], list[str], list[str]]:
    """(brand slicer name, info, problems, warnings) for one file; no slicer name when it has no brand slicer."""
    key = printer_of(f)
    slicer = BRAND_SLICERS.get(PRINTERS[key].vendor) if key else None
    if slicer is None:
        return None, [], [], []
    if not slicer.installed:
        return slicer.name, [], [], [f"{slicer.name} isn't installed at {slicer.app}: skipped"]
    info, problems, warnings = verify.slice_check(f, key, slicer)
    foreign = foreign_presets(f, slicer)
    if foreign:
        warnings.append(f"not among {slicer.name}'s own presets, so its menu shows the file's names "
                        f"(same settings): {', '.join(foreign)}")
    return slicer.name, info, problems, warnings

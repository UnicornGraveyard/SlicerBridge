"""
output.py -- how every engine lands a file in the output folder, and takes one back.

A file is written as `partial - <name>` beside its final name, checked there, and
only then swapped in. A file SlicerBridge didn't write is never overwritten: it's
moved to _previous/ first. When a model or printer no longer passes, the partial
file and SlicerBridge's own earlier file for it are removed, so an old file never
stands in for this run.

Standard library only.
"""
from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path

from .printers import LEGACY_STAMPS, STAMP


def partial(final: Path) -> Path:
    """Where `final` is written before it's checked: not dot-named, which macOS would flag hidden,
    and still ending in .3mf, which PrusaSlicer goes by."""
    return final.with_name(f"partial - {final.name}")


def ours(path: Path) -> bool:
    """Whether SlicerBridge wrote this file (its zip comment), so it may be replaced."""
    with zipfile.ZipFile(path) as z:
        return z.comment.startswith((STAMP, *LEGACY_STAMPS))


def unhide(path: Path) -> None:
    """Clear macOS's hidden flag on a finished file.

    iCloud's Documents sync flags any dot-named file or folder under ~/Documents as
    hidden within about 2 seconds, and the flag survives a rename. Temp files aren't
    dot-named; this is the guard in case anything else sets it.
    """
    flags = getattr(os.stat(path), "st_flags", 0)
    if flags & stat.UF_HIDDEN:
        os.chflags(path, flags & ~stat.UF_HIDDEN)


def land(tmp: Path, final: Path) -> None:
    """Swap the checked file `tmp` in as `final`; a file there that SlicerBridge didn't write goes
    to _previous/ first, never over an earlier backup."""
    if final.exists() and not ours(final):
        prev = final.parent / "_previous"
        prev.mkdir(exist_ok=True)
        dest, n = prev / final.name, 2
        while dest.exists():
            dest = prev / f"{final.stem} ({n}){final.suffix}"
            n += 1
        shutil.move(str(final), str(dest))
        print(f"       moved the existing {final.name} (not made by SlicerBridge) to _previous/{dest.name}")
    tmp.replace(final)
    unhide(final)


def withdraw(final: Path, why: str) -> None:
    """After a refusal or failure: no partial file left behind, and no earlier file of ours standing in."""
    partial(final).unlink(missing_ok=True)
    if final.exists() and ours(final):
        final.unlink()
        print(f"       removed the earlier {final.name}: {why}")


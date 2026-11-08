"""
printers.py -- every printer SlicerBridge writes for, the slicer each one uses,
and how output files are named. Standard library only.

Three engines share this table:

- orca: Bambu Lab printers (Bambu Studio's own profiles) and printers whose
  makers ship a slicer forked from OrcaSlicer (Creality Print, ElegooSlicer,
  Anycubic Slicer Next, Flash Studio, QIDI Studio). All of them read the same
  Bambu-format .3mf; each file carries settings the printer's own slicer wrote
  (`slicerbridge templates`).
- prusa: Prusa multi-material printers, written by Panda2Prusa as a PrusaSlicer
  project with the printer's PrusaSlicer profile.
- u1: the Snapmaker U1, from bl2u1's U1 templates.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def cache_dir() -> Path:
    """Where SlicerBridge keeps what it builds from your slicers (templates, profiles)."""
    env = os.environ.get("SLICERBRIDGE_CACHE")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "SlicerBridge"
    # Not %LOCALAPPDATA% on Windows: the Microsoft Store's Python writes there into a private
    # copy that the slicers, separate programs, can't see (PrusaSlicer's --datadir is in here).
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "slicerbridge"


TEMPLATES = cache_dir() / "templates"


@dataclass(frozen=True)
class Slicer:
    """An installed slicer: a macOS .app, or on Linux/Windows the executable itself (on Windows, its .exe)."""
    name: str
    app: Path
    library: str = ""  # a profiles/ folder of shared filaments that vendor presets inherit from
    exe: str = ""  # the macOS binary's name, when it isn't the slicer's name without spaces
    cli: tuple = ()  # arguments its command line needs before any other
    cask: str = ""  # its Homebrew cask, installed on macOS when the slicer isn't found

    @property
    def binary(self) -> Path:
        if self.app.suffix == ".app":
            return self.app / "Contents" / "MacOS" / (self.exe or self.name.replace(" ", ""))
        return self.app

    @property
    def profiles(self) -> Path:
        if self.app.suffix == ".app":
            return self.app / "Contents" / "Resources" / "profiles"
        # an extracted AppImage or a Windows install: resources/ beside or above the binary
        for d in (self.app.parent / "resources", self.app.parent.parent / "resources"):
            if (d / "profiles").is_dir():
                return d / "profiles"
        return self.app.parent / "resources" / "profiles"

    @property
    def installed(self) -> bool:
        return self.binary.exists()


def _app(env: str, *candidates: str) -> Path:
    """The slicer at $env, else the first candidate that exists, else the first candidate.

    Candidates may name environment variables (%ProgramFiles%) and wildcards, for
    installers that put the version in the folder name: the newest match wins.
    """
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    paths = []
    for c in candidates:
        c = os.path.expanduser(os.path.expandvars(c))
        wild = any(ch in c for ch in "*?[")
        found = sorted((Path(m) for m in glob.glob(c)), key=_newest, reverse=True) if wild else []
        paths += found or [Path(c)]
    return next((p for p in paths if p.exists()), paths[0])


def _newest(path: Path) -> tuple:
    """Sorts "Creality Print 7.0" after "Creality Print 6.2", "6.10" after "6.9", and
    "7.0" after "7.0 Beta 2": the version is the first number in the folder that has one,
    and of the same version, the one with nothing after it is the release."""
    part = next((p for p in reversed(path.parts) if re.search(r"\d", p)), "")
    found = re.search(r"(\d+(?:\.\d+)*)(.*)", part)
    if not found:
        return (), False, ()
    version, rest = found.groups()
    return (tuple(int(n) for n in version.split(".")), not re.search(r"[A-Za-z]", rest),
            tuple(int(n) for n in re.findall(r"\d+", rest)))


def _installs(mac: tuple, windows: tuple) -> tuple:
    """Where a slicer installs on this platform. Linux has no usual place, so the macOS paths stand
    in there (doctor shows them as not found) and the variable says where it is."""
    if sys.platform != "win32":
        return mac
    # A 32-bit Python reads %ProgramFiles% as Program Files (x86); %ProgramW6432% is the 64-bit one.
    return windows + tuple(c.replace("%ProgramFiles%", "%ProgramW6432%") for c in windows)


# A slicer's command line that crashed: SIGABRT and SIGSEGV, as subprocess or as the shell reports them,
# and on Windows an access violation, a heap corruption and a failed stack check (0xC0000005,
# 0xC0000374, 0xC0000409).
CRASHES = {-6, -11, 134, 139, 0xC0000005, 0xC0000374, 0xC0000409}


def run_slicer(cmd: list, timeout: int, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """Run a slicer's command line and capture what it says.

    Its output is read as UTF-8 whatever the system's code page, and on Windows
    it gets no console window of its own (the MCP server has none to share).
    """
    return subprocess.run([str(c) for c in cmd], capture_output=True, cwd=cwd or tempfile.gettempdir(), text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def refused_settings(log: str) -> dict[str, str]:
    """{setting: what the slicer said} for every setting it won't load, anywhere in its log.

    OrcaSlicer and its forks say "tree_support_wall_count: -1 not in range [0,2]", PrusaSlicer
    "Value out of range: wipe_tower_brim_width", and both "Invalid value provided for parameter
    brim_width: ...". The first word about a setting is kept.
    """
    found: dict[str, str] = {}
    for line in log.splitlines():
        if m := re.match(r"\s*(\w+): (.*\bnot in range\b.*)", line):
            found.setdefault(m.group(1), m.group(2).strip())
        elif m := re.search(r"(?:Invalid value provided for parameter|Value out of range:) (\w+)", line):
            found.setdefault(m.group(1), line[m.start():].strip())
    return found


# Characters Windows refuses in a file name: a file made on a Mac is still opened on Windows.
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def file_safe(text: str) -> str:
    """`text` with what Windows can't put in a file name replaced by "-"."""
    return UNSAFE.sub("-", text)


# The slicers files are built with. SlicerBridge is tested against OrcaSlicer
# 2.4.2: its profiles drive the Creality K1C's CFS and the Elegoo Centauri
# Carbon's CANVAS, where 2.3.1's stopped for a manual swap at every color.
SLICERS = {
    "orca":  Slicer("OrcaSlicer", _app("SLICERBRIDGE_ORCASLICER", *_installs(
        mac=("/Applications/OrcaSlicer.app", "~/Applications/OrcaSlicer.app"),
        windows=(r"%ProgramFiles%\OrcaSlicer\orca-slicer.exe",))), "OrcaFilamentLibrary", cask="orcaslicer"),
    "bambu": Slicer("Bambu Studio", _app("SLICERBRIDGE_BAMBU_STUDIO", *_installs(
        mac=("/Applications/BambuStudio.app", "~/Applications/BambuStudio.app"),
        windows=(r"%ProgramFiles%\Bambu Studio\bambu-studio.exe",)))),
    # On Windows, prusa-slicer.exe is the window and prints nothing: the console one answers.
    "prusa": Slicer("PrusaSlicer", _app("SLICERBRIDGE_PRUSASLICER", *_installs(
        mac=("/Applications/PrusaSlicer.app", "/Applications/Original Prusa Drivers/PrusaSlicer.app",
             "~/Applications/PrusaSlicer.app"),
        windows=(r"%ProgramFiles%\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",)))),
    # The U1's files come from templates, not a slicer: Snapmaker Orca only slices them
    # (--slice). Names from its source: the bundle and binary (build_release_macos.sh),
    # the Windows folder (CPACK_PACKAGE_INSTALL_DIRECTORY) and exe (src/CMakeLists.txt).
    "snapmaker": Slicer("Snapmaker Orca", _app("SLICERBRIDGE_SNAPMAKER_ORCA", *_installs(
        mac=("/Applications/Snapmaker Orca.app", "~/Applications/Snapmaker Orca.app"),
        windows=(r"%ProgramFiles%\Snapmaker_Orca\snapmaker-orca.exe",))),
                         exe="Snapmaker_Orca", cask="snapmaker-orca"),
}


def env_var(slicer: Slicer) -> str:
    """The environment variable that says where `slicer` is."""
    return "SLICERBRIDGE_" + slicer.name.upper().replace(" ", "_")


_TRIED_CASKS: set = set()


def ensure_installed(slicer: Slicer) -> None:
    """Install a missing slicer with Homebrew, on macOS, when it has a cask and brew is there.

    Not when its variable names a path: that path is someone's choice, and the cask installs
    to /Applications, where the slicer is looked for otherwise. Once a run, so a failed install
    isn't retried for every printer; brew's output goes to stderr, never into the MCP server's
    replies on stdout.
    """
    if (slicer.installed or not slicer.cask or sys.platform != "darwin" or os.environ.get(env_var(slicer))
            or slicer.cask in _TRIED_CASKS):
        return
    _TRIED_CASKS.add(slicer.cask)
    brew = shutil.which("brew") or next(
        (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(b).exists()), None)
    if not brew:
        return
    print(f"  {slicer.name} isn't installed: brew install --cask {slicer.cask}", file=sys.stderr)
    # Captured, not handed sys.stderr: the MCP server swaps it for a buffer with no file
    # descriptor, and its stdin is the MCP channel, which brew must never read.
    r = subprocess.run([brew, "install", "--cask", slicer.cask], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if r.stdout or r.stderr:
        print(r.stdout + r.stderr, end="", file=sys.stderr)


def not_found(slicer: Slicer) -> str:
    """Why `slicer` can't be used, and what to do about it."""
    env = env_var(slicer)
    if os.environ.get(env):
        return f"{slicer.name} isn't at {slicer.app}, where {env} points: install it there, or unset {env}"
    how = f"install it (brew install --cask {slicer.cask})" if slicer.cask and sys.platform == "darwin" else "install it"
    return f"{slicer.name} isn't at {slicer.app}: {how}, or point {env} at it"


# The slicer each brand's owners download, for `slicerbridge brand-check`: the
# files are built in OrcaSlicer, and each of these is a fork with its own
# presets, version check and bugs (docs/brand-slicers.md).
# - Creality Print 7.3 opens its window unless an argument is --cli
#   (src/CrealityPrint.cpp, parse_application_arguments), and writes
#   plate_N.gcode to --outputdir only with --need-gcode-file. On Windows its
#   folder carries the version ("Creality Print 7.0"): the newest wins.
# - QIDI Studio's command line rearranges any 3MF not stamped QIDIStudio, merging
#   the plates; its app keeps them (qds_3mf.cpp, QIDIStudio.cpp: need_arrange).
#   --arrange 0 slices the plates as the app opens them.
# - Windows locations are from each fork's installer rules (CMakeLists.txt, package.cmake).
#   Flash Studio's build names its exe "flash studio" while its shortcut points at
#   flash-studio.exe, so both are looked for.
BRAND_SLICERS = {
    "Creality":   Slicer("Creality Print", _app("SLICERBRIDGE_CREALITY_PRINT", *_installs(
        mac=("/Applications/CrealityPrint.app", "/Applications/Creality Print.app"),
        windows=(r"%ProgramFiles%\Creality\Creality Print *\CrealityPrint.exe",))),
                         exe="CrealityPrint", cli=("--cli", "--need-gcode-file")),
    "Elegoo":     Slicer("ElegooSlicer", _app("SLICERBRIDGE_ELEGOOSLICER", *_installs(
        mac=("/Applications/ElegooSlicer.app",),
        windows=(r"%ProgramFiles%\ElegooSlicer\elegoo-slicer.exe",)))),
    "Anycubic":   Slicer("Anycubic Slicer Next", _app("SLICERBRIDGE_ANYCUBIC_SLICER_NEXT", *_installs(
        mac=("/Applications/AnycubicSlicerNext.app", "/Applications/Anycubic Slicer Next.app"),
        windows=(r"%ProgramFiles%\AnycubicSlicer\AnycubicSlicerNext.exe",
                 r"%ProgramFiles%\AnycubicSlicerNext\AnycubicSlicerNext.exe"))),
                         exe="AnycubicSlicerNext"),
    "Flashforge": Slicer("Flash Studio", _app("SLICERBRIDGE_FLASH_STUDIO", *_installs(
        mac=("/Applications/Flash Studio.app", "/Applications/FlashStudio.app"),
        windows=(r"%ProgramFiles%\Orca-Flashforge\flash-studio.exe", r"%ProgramFiles%\Orca-Flashforge\flash studio.exe",
                 r"%ProgramFiles%\Flash Studio\flash-studio.exe", r"%ProgramFiles%\Flash Studio\flash studio.exe"))),
                         exe="Flash Studio"),
    "Qidi":       Slicer("QIDI Studio", _app("SLICERBRIDGE_QIDI_STUDIO", *_installs(
        mac=("/Applications/QIDIStudio.app", "/Applications/QIDI Studio.app"),
        windows=(r"%ProgramFiles%\QIDIStudio\qidi-studio-console.exe", r"%ProgramFiles%\QIDIStudio\qidi-studio.exe"))),
                         exe="QIDIStudio", cli=("--arrange", "0")),
}

# Zip comment on every file SlicerBridge writes. A file in the output folder
# without it came from somewhere else and is moved to _previous/, never overwritten.
STAMP = b"SlicerBridge"
LEGACY_STAMPS = (b"convert-orca.py", b"convert-u1.py", b"convert-prusa.py")


@dataclass(frozen=True)
class Printer:
    """A printer the orca engine writes for."""
    label: str        # in the file name: what the downloader owns
    vendor: str       # the slicer's profiles/<vendor>/ folder
    machine: str      # printer preset
    process: str      # print preset, the 0.16 mm one where the printer has it
    filaments: dict = field(default_factory=dict)  # filament type -> filament preset
    tools: int = 4    # colors the printer's filament unit (or toolheads) can hold
    slicer: str = "orca"


# Each printer's filaments: a preset for each of MAIN_TYPES its own slicer has one for, typed
# exactly that (filament_type), listing the printer, and printing within its own recommended
# range (templates.py checks the last). A type missing here is refused for that printer.
PRINTERS = {
    # ---- Bambu Lab, from Bambu Studio's own profiles: its Generic preset for each type. An AMS
    # chains up to 4 units (16 spools); the A1 and A1 mini's AMS lite holds 4. Every one has a
    # Generic preset for both TPUs (BAMBU_TYPES); the open A1 mini has none for ABS, ASA, PC or PA.
    "a1mini":   Printer("Bambu Lab A1 mini", "BBL", "Bambu Lab A1 mini 0.4 nozzle", "0.16mm Optimal @BBL A1M",
                        {"PLA": "Generic PLA @BBL A1M", "PETG": "Generic PETG @BBL A1M",
                         "PCTG": "Generic PCTG @BBL A1M", "PLA-CF": "Generic PLA-CF @BBL A1M",
                         "PETG-CF": "Generic PETG-CF @BBL A1M", "HIPS": "Generic HIPS @BBL A1M",
                         "PVA": "Generic PVA @BBL A1M", "BVOH": "Generic BVOH @BBL A1M",
                         "TPU": "Generic TPU @BBL A1M", "TPU-AMS": "Generic TPU for AMS @BBL A1M"}, 4, "bambu"),
    "a1":       Printer("Bambu Lab A1", "BBL", "Bambu Lab A1 0.4 nozzle", "0.16mm Optimal @BBL A1",
                        {"PLA": "Generic PLA @BBL A1", "PETG": "Generic PETG @BBL A1", "PCTG": "Generic PCTG @BBL A1",
                         "ABS": "Generic ABS @BBL A1", "ASA": "Generic ASA @BBL A1", "PC": "Generic PC @BBL A1",
                         "PA": "Generic PA @BBL A1", "PLA-CF": "Generic PLA-CF @BBL A1",
                         "PETG-CF": "Generic PETG-CF @BBL A1", "PA-CF": "Generic PA-CF @BBL A1",
                         "HIPS": "Generic HIPS @BBL A1", "PVA": "Generic PVA @BBL A1", "BVOH": "Generic BVOH @BBL A1",
                         "TPU": "Generic TPU @BBL A1", "TPU-AMS": "Generic TPU for AMS @BBL A1"}, 4, "bambu"),
    "p1s":      Printer("Bambu Lab P1S", "BBL", "Bambu Lab P1S 0.4 nozzle", "0.16mm Optimal @BBL X1C",
                        {"PLA": "Generic PLA", "PETG": "Generic PETG", "PCTG": "Generic PCTG @BBL X1C",
                         "ABS": "Generic ABS", "ASA": "Generic ASA", "PC": "Generic PC @BBL P1S", "PA": "Generic PA",
                         "PLA-CF": "Generic PLA-CF", "PETG-CF": "Generic PETG-CF @BBL X1C", "PA-CF": "Generic PA-CF",
                         "HIPS": "Generic HIPS @BBL X1C", "PVA": "Generic PVA", "BVOH": "Generic BVOH @BBL X1C",
                         "TPU": "Generic TPU", "TPU-AMS": "Generic TPU for AMS @BBL X1C"}, 16, "bambu"),
    "p2s":      Printer("Bambu Lab P2S", "BBL", "Bambu Lab P2S 0.4 nozzle", "0.16mm Standard @BBL P2S",
                        {"PLA": "Generic PLA @BBL P2S", "PETG": "Generic PETG @BBL P2S",
                         "PCTG": "Generic PCTG @BBL P2S", "ABS": "Generic ABS @BBL P2S",
                         "ASA": "Generic ASA @BBL P2S", "PC": "Generic PC @BBL P2S", "PA": "Generic PA @BBL P2S",
                         "PLA-CF": "Generic PLA-CF @BBL P2S", "PETG-CF": "Generic PETG-CF @BBL P2S",
                         "PA-CF": "Generic PA-CF @BBL P2S", "HIPS": "Generic HIPS @BBL P2S",
                         "PVA": "Generic PVA @BBL P2S", "BVOH": "Generic BVOH @BBL P2S",
                         "TPU": "Generic TPU @BBL P2S", "TPU-AMS": "Generic TPU for AMS @BBL P2S"}, 16, "bambu"),
    "x1c":      Printer("Bambu Lab X1 Carbon", "BBL", "Bambu Lab X1 Carbon 0.4 nozzle", "0.16mm Optimal @BBL X1C",
                        {"PLA": "Generic PLA", "PETG": "Generic PETG", "PCTG": "Generic PCTG @BBL X1C",
                         "ABS": "Generic ABS", "ASA": "Generic ASA", "PC": "Generic PC", "PA": "Generic PA",
                         "PLA-CF": "Generic PLA-CF", "PETG-CF": "Generic PETG-CF @BBL X1C", "PA-CF": "Generic PA-CF",
                         "HIPS": "Generic HIPS @BBL X1C", "PVA": "Generic PVA", "BVOH": "Generic BVOH @BBL X1C",
                         "TPU": "Generic TPU", "TPU-AMS": "Generic TPU for AMS @BBL X1C"}, 16, "bambu"),
    "h2s":      Printer("Bambu Lab H2S", "BBL", "Bambu Lab H2S 0.4 nozzle", "0.16mm Standard @BBL H2S",
                        {"PLA": "Generic PLA @BBL H2S", "PETG": "Generic PETG @BBL H2S",
                         "PCTG": "Generic PCTG @BBL H2S 0.4 nozzle", "ABS": "Generic ABS @BBL H2S 0.4 nozzle",
                         "ASA": "Generic ASA @BBL H2S 0.4 nozzle", "PC": "Generic PC @BBL H2S",
                         "PA": "Generic PA @BBL H2S 0.4 nozzle", "PLA-CF": "Generic PLA-CF @BBL H2S",
                         "PETG-CF": "Generic PETG-CF @BBL H2S", "PA-CF": "Generic PA-CF @BBL H2S 0.4 nozzle",
                         "HIPS": "Generic HIPS @BBL H2S 0.4 nozzle", "PVA": "Generic PVA @BBL H2S 0.4 nozzle",
                         "BVOH": "Generic BVOH @BBL H2S 0.4 nozzle", "TPU": "Generic TPU @BBL H2S",
                         "TPU-AMS": "Generic TPU for AMS @BBL H2S"}, 16, "bambu"),
    "h2d":      Printer("Bambu Lab H2D", "BBL", "Bambu Lab H2D 0.4 nozzle", "0.16mm Standard @BBL H2D",
                        {"PLA": "Generic PLA @BBL H2D", "PETG": "Generic PETG @BBL H2D",
                         "PCTG": "Generic PCTG @BBL H2D", "ABS": "Generic ABS @BBL H2D",
                         "ASA": "Generic ASA @BBL H2D", "PC": "Generic PC @BBL H2D", "PA": "Generic PA @BBL H2D",
                         "PLA-CF": "Generic PLA-CF @BBL H2D", "PETG-CF": "Generic PETG-CF @BBL H2D",
                         "PA-CF": "Generic PA-CF @BBL H2D", "HIPS": "Generic HIPS @BBL H2D",
                         "PVA": "Generic PVA @BBL H2D", "BVOH": "Generic BVOH @BBL H2D",
                         "TPU": "Generic TPU @BBL H2D", "TPU-AMS": "Generic TPU for AMS @BBL H2D"}, 16, "bambu"),
    "h2dpro":   Printer("Bambu Lab H2D Pro", "BBL", "Bambu Lab H2D Pro 0.4 nozzle", "0.16mm Standard @BBL H2DP",
                        {"PLA": "Generic PLA @BBL H2DP", "PETG": "Generic PETG @BBL H2DP",
                         "PCTG": "Generic PCTG @BBL H2DP", "ABS": "Generic ABS @BBL H2DP",
                         "ASA": "Generic ASA @BBL H2DP", "PC": "Generic PC @BBL H2DP", "PA": "Generic PA @BBL H2DP",
                         "PLA-CF": "Generic PLA-CF @BBL H2DP", "PETG-CF": "Generic PETG-CF @BBL H2DP",
                         "PA-CF": "Generic PA-CF @BBL H2DP", "HIPS": "Generic HIPS @BBL H2DP",
                         "PVA": "Generic PVA @BBL H2DP", "BVOH": "Generic BVOH @BBL H2DP",
                         "TPU": "Generic TPU @BBL H2DP", "TPU-AMS": "Generic TPU for AMS @BBL H2DP"}, 16, "bambu"),
    "h2c":      Printer("Bambu Lab H2C", "BBL", "Bambu Lab H2C 0.4 nozzle", "0.16mm Standard @BBL H2C",
                        {"PLA": "Generic PLA @BBL H2C 0.4 nozzle", "PETG": "Generic PETG @BBL H2C 0.4 nozzle",
                         "PCTG": "Generic PCTG @BBL H2C 0.4 nozzle", "ABS": "Generic ABS @BBL H2C 0.4 nozzle",
                         "ASA": "Generic ASA @BBL H2C 0.4 nozzle", "PC": "Generic PC @BBL H2C 0.4 nozzle",
                         "PA": "Generic PA @BBL H2C 0.4 nozzle", "PLA-CF": "Generic PLA-CF @BBL H2C 0.4 nozzle",
                         "PETG-CF": "Generic PETG-CF @BBL H2C 0.4 nozzle",
                         "PA-CF": "Generic PA-CF @BBL H2C 0.4 nozzle", "HIPS": "Generic HIPS @BBL H2C 0.4 nozzle",
                         "PVA": "Generic PVA @BBL H2C 0.4 nozzle", "BVOH": "Generic BVOH @BBL H2C 0.4 nozzle",
                         "TPU": "Generic TPU @BBL H2C 0.4 nozzle",
                         "TPU-AMS": "Generic TPU for AMS @BBL H2C 0.4 nozzle"}, 16, "bambu"),
    # Two nozzles, like the H2D: the left one direct drive, the right one Bowden (meant for supports),
    # which doesn't reach the bed's left 20.5 mm.
    "x2d":      Printer("Bambu Lab X2D", "BBL", "Bambu Lab X2D 0.4 nozzle", "0.16mm Standard @BBL X2D",
                        {"PLA": "Generic PLA @BBL X2D 0.4 nozzle", "PETG": "Generic PETG @BBL X2D 0.4 nozzle",
                         "PCTG": "Generic PCTG @BBL X2D 0.4 nozzle", "ABS": "Generic ABS @BBL X2D 0.4 nozzle",
                         "ASA": "Generic ASA @BBL X2D 0.4 nozzle", "PC": "Generic PC @BBL X2D 0.4 nozzle",
                         "PA": "Generic PA @BBL X2D 0.4 nozzle", "PLA-CF": "Generic PLA-CF @BBL X2D 0.4 nozzle",
                         "PETG-CF": "Generic PETG-CF @BBL X2D 0.4 nozzle",
                         "PA-CF": "Generic PA-CF @BBL X2D 0.4 nozzle", "HIPS": "Generic HIPS @BBL X2D 0.4 nozzle",
                         "PVA": "Generic PVA @BBL X2D 0.4 nozzle", "BVOH": "Generic BVOH @BBL X2D 0.4 nozzle",
                         "TPU": "Generic TPU @BBL X2D 0.4 nozzle",
                         "TPU-AMS": "Generic TPU for AMS @BBL X2D 0.4 nozzle"}, 16, "bambu"),
    # Nine toolheads on a rack, which Bambu Studio's profile models as one extruder that reaches the
    # whole bed. Its PLA, PETG, PC, PLA-CF, PETG-CF and TPU presets are named without the nozzle.
    "h3c":      Printer("Bambu Lab H3C", "BBL", "Bambu Lab H3C 0.4 nozzle", "0.16mm Standard @BBL H3C",
                        {"PLA": "Generic PLA @BBL H3C", "PETG": "Generic PETG @BBL H3C",
                         "PCTG": "Generic PCTG @BBL H3C 0.4 nozzle", "ABS": "Generic ABS @BBL H3C 0.4 nozzle",
                         "ASA": "Generic ASA @BBL H3C 0.4 nozzle", "PC": "Generic PC @BBL H3C",
                         "PA": "Generic PA @BBL H3C 0.4 nozzle", "PLA-CF": "Generic PLA-CF @BBL H3C",
                         "PETG-CF": "Generic PETG-CF @BBL H3C", "PA-CF": "Generic PA-CF @BBL H3C 0.4 nozzle",
                         "HIPS": "Generic HIPS @BBL H3C 0.4 nozzle", "PVA": "Generic PVA @BBL H3C 0.4 nozzle",
                         "BVOH": "Generic BVOH @BBL H3C 0.4 nozzle", "TPU": "Generic TPU @BBL H3C",
                         "TPU-AMS": "Generic TPU for AMS @BBL H3C"}, 9, "bambu"),

    # ---- Creality (CFS), from OrcaSlicer. From here on, each printer's TPU is its maker's own soft
    # TPU preset (95A and the like) for it; none has one for Bambu's TPU for AMS, which is refused.
    "k2plus":   Printer("Creality K2 Plus", "Creality", "Creality K2 Plus 0.4 nozzle",
                        "0.16mm Optimal @Creality K2 Plus 0.4 nozzle", {"PLA": "Creality Generic PLA @K2-all",
                                                                        "PETG": "Creality Generic PETG @K2-all",
                                                                        "PCTG": "Generic PCTG @K2 Plus-all",
                                                                        "ABS": "Creality Generic ABS @K2-all",
                                                                        "ASA": "Creality Generic ASA @K2-all",
                                                                        "PC": "Generic PC @K2 Plus-all",
                                                                        "PA": "Generic PA @K2 Plus-all",
                                                                        "PLA-CF": "Creality Generic PLA-CF @K2-all",
                                                                        "PETG-CF": "Generic PETG-CF @K2 Plus-all",
                                                                        "PA-CF": "Creality Generic PA-CF @K2-all",
                                                                        "HIPS": "Generic HIPS @K2 Plus-all",
                                                                        "PVA": "Generic PVA @K2 Plus-all",
                                                                        "BVOH": "Generic BVOH @K2 Plus-all",
                                                                        "TPU": "Creality Generic TPU @K2-all"}),
    "k2pro":    Printer("Creality K2 Pro", "Creality", "Creality K2 Pro 0.4 nozzle",
                        "0.16mm Optimal @Creality K2 Pro 0.4 nozzle", {"PLA": "Creality Generic PLA @K2-all",
                                                                       "PETG": "Creality Generic PETG @K2-all",
                                                                       "ABS": "Creality Generic ABS @K2-all",
                                                                       "ASA": "Creality Generic ASA @K2-all",
                                                                       "PC": "Generic PC @K2 Pro-all",
                                                                       "PA": "Generic PA @K2 Pro-all",
                                                                       "PLA-CF": "Creality Generic PLA-CF @K2-all",
                                                                       "PETG-CF": "Generic PETG-CF @K2 Pro-all",
                                                                       "PA-CF": "Creality Generic PA-CF @K2-all",
                                                                       "PVA": "Generic PVA @K2 Pro-all",
                                                                       "BVOH": "Generic BVOH @K2 Pro-all",
                                                                       "TPU": "Creality Generic TPU @K2-all"}),
    # Not here: the plain Creality K2. OrcaSlicer 2.4.2's own K2 preset cuts filament
    # with a 30 mm long retraction, outside the 10-18 mm its CLI accepts, so the CLI
    # won't load it -- and shortening a cut retraction risks jams in the CFS.
    # The K2 SE and the K1C with CFS-C use the shared K2-all / K1-all filaments Creality makes their defaults.
    "k2se":     Printer("Creality K2 SE", "Creality", "Creality K2 SE 0.4 nozzle",
                        "0.16mm Standard @Creality K2 SE 0.4 nozzle",
                        {"PLA": "Creality Generic PLA @K2-all", "PETG": "Creality Generic PETG @K2-all",
                         "ABS": "Creality Generic ABS @K2-all", "ASA": "Generic ASA @K2 SE-all",
                         "PLA-CF": "Generic PLA-CF @K2 SE-all", "TPU": "Generic TPU @K2 SE-all"}),
    "hi":       Printer("Creality Hi", "Creality", "Creality Hi 0.4 nozzle", "0.16mm Optimal @Creality Hi",
                        {"PLA": "Creality Generic PLA @Hi-all", "PETG": "Creality Generic PETG @Hi-all",
                         "ABS": "Creality Generic ABS @Hi-all", "ASA": "Creality Generic ASA @Hi-all",
                         "PLA-CF": "Creality Generic PLA-CF @Hi-all", "PVA": "Generic PVA @Hi-all",
                         "BVOH": "Generic BVOH @Hi-all", "TPU": "Creality Generic TPU @Hi-all"}),
    "k1c":      Printer("Creality K1C CFS", "Creality", "Creality K1C_CFS-C 0.4 nozzle",
                        "0.16mm Optimal @Creality K1C_CFS-C 0.4 nozzle",
                        {"PLA": "Creality Generic PLA @K1-all", "PETG": "Creality Generic PETG @K1-all",
                         "ABS": "Creality Generic ABS @K1-all", "ASA": "Generic ASA @K1C_CFS-C-all",
                         "PC": "Generic PC @K1C_CFS-C-all", "PA": "Generic PA @K1C_CFS-C-all",
                         "PLA-CF": "Generic PLA-CF @K1C_CFS-C-all", "PETG-CF": "Generic PETG-CF @K1C_CFS-C-all",
                         "PA-CF": "Generic PA-CF @K1C_CFS-C-all", "HIPS": "Generic HIPS @K1C_CFS-C-all",
                         "PVA": "Generic PVA @K1C_CFS-C-all", "BVOH": "Generic BVOH @K1C_CFS-C-all",
                         "TPU": "Generic TPU @K1C_CFS-C-all"}),

    # ---- Elegoo (CANVAS). OrcaSlicer 2.4.2's profile drives it; 2.3.1's stopped for M600.
    "cc":       Printer("Elegoo Centauri Carbon", "Elegoo", "Elegoo Centauri Carbon 0.4 nozzle",
                        "0.16mm Optimal @Elegoo CC 0.4 nozzle",
                        {"PLA": "Elegoo PLA @ECC", "PETG": "Elegoo PETG @ECC", "ABS": "Generic ABS @Elegoo Centauri",
                         "ASA": "Elegoo ASA @ECC", "PC": "Elegoo PC @ECC", "PA": "Generic PA @Elegoo",
                         "PETG-CF": "Generic PETG-CF @Elegoo Centauri", "TPU": "Elegoo TPU 95A @ECC"}),

    # ---- Anycubic (ACE Pro). The Kobra S1 has no 0.16 mm preset; 0.16 mm is carried over anyway.
    # Not Anycubic Generic TPU on the Kobra 3s: at 240 C it's past their PLA's 230, and OrcaSlicer
    # won't slice two filaments outside each other's range ("Selected nozzle temperatures are
    # incompatible"). Their own Kobra 3 TPUs print at 210 and 205.
    "kobras1":  Printer("Anycubic Kobra S1", "Anycubic", "Anycubic Kobra S1 0.4 nozzle",
                        "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle",
                        {"PLA": "Anycubic PLA @Anycubic Kobra S1 0.4 nozzle",
                         "PETG": "Anycubic PETG @Anycubic Kobra S1 0.4 nozzle",
                         "ABS": "Anycubic ABS @Anycubic Kobra S1 0.4 nozzle",
                         "ASA": "Anycubic ASA @Anycubic Kobra S1 0.4 nozzle",
                         "TPU": "Anycubic TPU @Anycubic Kobra S1 0.4 nozzle"}),
    "kobras1max": Printer("Anycubic Kobra S1 Max", "Anycubic", "Anycubic Kobra S1 Max 0.4 nozzle",
                          "0.16mm Standard @Anycubic Kobra S1 Max 0.4 nozzle",
                          {"PLA": "Anycubic PLA @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PETG": "Generic PETG @Anycubic Kobra S1 Max 0.4 nozzle",
                           "ABS": "Anycubic ABS @Anycubic Kobra S1 Max 0.4 nozzle",
                           "ASA": "Anycubic ASA @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PC": "Anycubic PC @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PA": "Anycubic PA @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PLA-CF": "Anycubic PLA-CF @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PETG-CF": "Anycubic PETG-CF @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PVA": "Anycubic PVA @Anycubic Kobra S1 Max 0.4 nozzle",
                           "TPU": "Anycubic TPU 95A @Anycubic Kobra S1 Max 0.4 nozzle"}),
    "kobra3":   Printer("Anycubic Kobra 3", "Anycubic", "Anycubic Kobra 3 0.4 nozzle",
                        "0.16mm Optimal @Anycubic Kobra 3 0.4 nozzle",
                        {"PLA": "Anycubic Generic PLA", "PETG": "Anycubic Generic PETG",
                         "ABS": "Anycubic Generic ABS", "ASA": "Anycubic Generic ASA",
                         "TPU": "Generic TPU @Anycubic Kobra 3 0.4 nozzle"}),
    "kobra3max": Printer("Anycubic Kobra 3 Max", "Anycubic", "Anycubic Kobra 3 Max 0.4 nozzle",
                         "0.16mm Standard @Anycubic Kobra 3 Max 0.4 nozzle",
                         {"PLA": "Anycubic Generic PLA", "PETG": "Anycubic Generic PETG",
                          "ABS": "Anycubic Generic ABS", "ASA": "Anycubic Generic ASA",
                          "TPU": "Anycubic TPU @Anycubic Kobra 3 Max 0.4 nozzle"}),
    # Not ABS, ASA or PVA on the Kobra X: Anycubic's presets for it print all three at 205 C, its PLA's
    # temperature, below their own recommended 230-280 (220-240 for PVA) -- in OrcaSlicer 2.4.2.
    "kobrax":   Printer("Anycubic Kobra X", "Anycubic", "Anycubic Kobra X 0.4 nozzle",
                        "0.16mm Standard @Anycubic Kobra X",
                        {"PLA": "Anycubic PLA @Anycubic Kobra X 0.4 nozzle",
                         "PETG": "Generic PETG @Anycubic Kobra X 0.4 nozzle",
                         "TPU": "Anycubic TPU 95A @Anycubic Kobra X 0.4 nozzle"}),

    # ---- Flashforge: the AD5X (IFS), and the Creator 5 and 5 Pro, 4-toolhead toolchangers. The
    # Creator 5's TPU presets in OrcaSlicer 2.4.2 are typed TPU-95A, TPU-90A and TPU-64D, not TPU:
    # none stands for a Bambu TPU here yet, so a TPU is refused for them. Nor PETG-CF: their
    # PETG-CF prints at 250 C, past its own recommended 235-240.
    "ad5x":     Printer("Flashforge AD5X", "Flashforge", "Flashforge AD5X 0.4 nozzle", "0.16mm Standard @FF AD5X",
                        {"PLA": "Flashforge Generic PLA", "PETG": "Flashforge Generic PETG",
                         "ABS": "Flashforge Generic ABS", "ASA": "Flashforge Generic ASA",
                         "PLA-CF": "Flashforge PLA-CF", "PETG-CF": "Flashforge PETG-CF",
                         "PA-CF": "Flashforge PA12-CF", "TPU": "Flashforge TPU 95A @FF AD5X"}),
    "creator5": Printer("Flashforge Creator 5", "Flashforge", "Flashforge Creator 5 0.4 nozzle",
                        "0.20mm Standard @FF C5",
                        {"PLA": "Generic PLA @FF C5", "PETG": "Flashforge PETG Pro @FF C5",
                         "ABS": "Flashforge ABS Basic @FF C5", "ASA": "Flashforge ASA Basic @FF C5",
                         "PLA-CF": "Flashforge PLA-CF @FF C5", "PA-CF": "Flashforge PA-CF @FF C5",
                         "HIPS": "Flashforge HIPS @FF C5", "PVA": "Flashforge PVA @FF C5",
                         "BVOH": "Generic BVOH @FF C5"}),
    "creator5pro": Printer("Flashforge Creator 5 Pro", "Flashforge", "Flashforge Creator 5 Pro 0.4 nozzle",
                           "0.20mm Standard @FF C5",
                           {"PLA": "Generic PLA @FF C5P", "PETG": "Flashforge PETG Pro @FF C5P",
                            "ABS": "Flashforge ABS Basic @FF C5P", "ASA": "Flashforge ASA Basic @FF C5P",
                            "PLA-CF": "Flashforge PLA-CF @FF C5P", "PA-CF": "Flashforge PA-CF @FF C5P",
                            "HIPS": "Flashforge HIPS @FF C5P", "PVA": "Flashforge PVA @FF C5P",
                            "BVOH": "Generic BVOH @FF C5P"}),

    # ---- QIDI (QIDI Box). Sold as the Plus4 and Max4; OrcaSlicer still calls them X-Plus 4 / X-Max 4.
    "q2":       Printer("QIDI Q2", "Qidi", "Qidi Q2 0.4 nozzle", "0.16mm Optimal @Qidi Q2",
                        {"PLA": "Generic PLA @Qidi Q2 0.4 nozzle", "PETG": "Generic PETG @Qidi Q2 0.4 nozzle",
                         "ABS": "Generic ABS @Qidi Q2 0.4 nozzle", "ASA": "QIDI ASA @Qidi Q2 0.4 nozzle",
                         "PC": "Generic PC @Qidi Q2 0.4 nozzle", "PA": "Qidi Generic PA",
                         "PLA-CF": "QIDI PLA-CF @Qidi Q2 0.4 nozzle", "PETG-CF": "QIDI PETG-CF @Qidi Q2 0.4 nozzle",
                         "PA-CF": "Qidi Generic PA-CF", "PVA": "Qidi Generic PVA",
                         "TPU": "Generic TPU 95A @Qidi Q2 0.4 nozzle"}),
    "q2c":      Printer("QIDI Q2C", "Qidi", "Qidi Q2C 0.4 nozzle", "0.16mm Optimal @Qidi Q2C",
                        {"PLA": "Generic PLA @Qidi Q2C 0.4 nozzle", "PETG": "Generic PETG @Qidi Q2C 0.4 nozzle",
                         "ABS": "Generic ABS @Qidi Q2C 0.4 nozzle", "ASA": "QIDI ASA @Qidi Q2C 0.4 nozzle",
                         "PC": "Generic PC @Qidi Q2C 0.4 nozzle", "PLA-CF": "QIDI PLA-CF @Qidi Q2C 0.4 nozzle",
                         "PETG-CF": "QIDI PETG-CF @Qidi Q2C 0.4 nozzle",
                         "TPU": "Generic TPU 95A @Qidi Q2C 0.4 nozzle"}),
    "plus4":    Printer("QIDI Plus4", "Qidi", "Qidi X-Plus 4 0.4 nozzle", "0.16mm Optimal @Qidi XPlus4",
                        {"PLA": "Qidi Generic PLA @Qidi X-Plus 4 0.4 nozzle",
                         "PETG": "Qidi Generic PETG @Qidi X-Plus 4 0.4 nozzle",
                         "ABS": "Qidi Generic ABS @Qidi X-Plus 4 0.4 nozzle",
                         "ASA": "Qidi Generic ASA @Qidi X-Plus 4 0.4 nozzle",
                         "PC": "Qidi Generic PC @Qidi X-Plus 4 0.4 nozzle", "PA": "Qidi Generic PA",
                         "PLA-CF": "Qidi Generic PLA-CF", "PETG-CF": "Qidi Generic PETG-CF",
                         "PA-CF": "Qidi Generic PA-CF", "PVA": "Qidi Generic PVA",
                         "TPU": "Qidi Generic TPU @Qidi X-Plus 4 0.4 nozzle"}),
    "max4":     Printer("QIDI Max4", "Qidi", "Qidi X-Max 4 0.4 nozzle", "0.16mm Standard @X-Max 4",
                        {"PLA": "Generic PLA @Qidi X-Max 4 0.4 nozzle",
                         "PETG": "Generic PETG @Qidi X-Max 4 0.4 nozzle",
                         "ABS": "Generic ABS @Qidi X-Max 4 0.4 nozzle", "ASA": "QIDI ASA @Qidi X-Max 4 0.4 nozzle",
                         "PC": "Generic PC @Qidi X-Max 4 0.4 nozzle",
                         "PLA-CF": "QIDI PLA-CF @Qidi X-Max 4 0.4 nozzle",
                         "PETG-CF": "QIDI PETG-CF @Qidi X-Max 4 0.4 nozzle",
                         "TPU": "Generic TPU 95A @Qidi X-Max 4 0.4 nozzle"}),

    # ---- WonderMaker: the ZR Ultra S, a 4-toolhead toolchanger.
    "zrultras": Printer("WonderMaker ZR Ultra S", "WonderMaker", "WonderMaker ZR Ultra S 0.4 nozzle",
                        "0.16mm Optimal @WonderMaker ZR Ultra",
                        {"PLA": "WonderMaker PLA Basic", "PETG": "WonderMaker PETG Basic",
                         "ABS": "WonderMaker ABS @WonderMaker ZR Ultra S",
                         "ASA": "WonderMaker ASA @WonderMaker ZR Ultra S", "PVA": "WonderMaker PVA",
                         "TPU": "WonderMaker TPU 95A"}),
}


# What feeds each orca-engine printer's colors, by vendor, for the word on soft TPU: Bambu's
# AMS can't feed it, so it goes on the external spool, by hand. A toolchanger's heads each
# take their own spool.
FEEDERS = {"BBL": "AMS", "Creality": "CFS", "Elegoo": "CANVAS", "Anycubic": "ACE Pro", "Flashforge": "IFS",
           "Qidi": "QIDI Box", "WonderMaker": ""}
TOOLCHANGERS = {"creator5", "creator5pro", "zrultras"}


def feeder_of(key: str) -> str:
    """The filament changer that feeds printer `key`'s colors ("AMS", "CFS", ...), or "" for a toolchanger."""
    return "" if key in TOOLCHANGERS else FEEDERS.get(PRINTERS[key].vendor, "")


@dataclass(frozen=True)
class PrusaPrinter:
    """A printer the prusa engine writes for."""
    tools: int
    label: str            # in the file name
    preset: str           # PrusaSlicer system printer preset
    model: str            # its printer_model id, installed in SlicerBridge's private datadir
    variant: str          # nozzle variant of that model
    print_preset: str     # 0.15 mm: the closest Prusa profile to a Bambu 0.16 mm project
    filament_preset: str  # loaded on every tool; only PLA is mapped so far


PRUSA_PRINTERS = {
    # The MMU3 needs a host printer. Core One: 270 mm of Z, so tall prints fit;
    # an MK4S MMU3 stops at 220 mm.
    "mmu3":  PrusaPrinter(5, "Prusa MMU3", "Prusa CORE One MMU3 0.4 nozzle", "COREONEMMU3", "0.4",
                          "0.15mm SPEED @COREONE 0.4", "Prusament PLA @COREONE"),
    "xl":    PrusaPrinter(5, "Prusa XL", "Original Prusa XL - 5T Input Shaper 0.4 nozzle", "XL5IS", "0.4",
                          "0.15mm SPEED @XLIS 0.4", "Prusament PLA @XLIS"),
    # 4T and 8T get their own names: the embedded printer profile differs, so one
    # file must never silently overwrite the other.
    "indx4": PrusaPrinter(4, "Prusa Core One INDX 4T", "Prusa CORE One INDX 4T HF0.4 nozzle", "COREONE_INDX4T",
                          "HF0.4", "0.15mm Detail @COREONEINDX HF0.4", "Prusament PLA @COREONEINDX HF0.4"),
    "indx8": PrusaPrinter(8, "Prusa Core One INDX 8T", "Prusa CORE One INDX 8T HF0.4 nozzle", "COREONE_INDX8T",
                          "HF0.4", "0.15mm Detail @COREONEINDX HF0.4", "Prusament PLA @COREONEINDX HF0.4"),
}

U1_LABEL = "Snapmaker U1"
U1_TOOLS = 4


@dataclass(frozen=True)
class PrinterInfo:
    """What every printer has, whichever engine writes its files."""
    key: str
    label: str    # in the file name
    tools: int    # colors it holds
    vendor: str   # for --printers <vendor> and its brand slicer; the orca engine's is its profiles/ folder
    engine: str   # orca, prusa or u1
    slicer: str   # the SLICERS key of the slicer its file is built (or sliced) with


# One record per printer, from each engine's own table, in --printers all order.
INFO = {
    **{k: PrinterInfo(k, p.label, p.tools, p.vendor, "orca", p.slicer) for k, p in PRINTERS.items()},
    **{k: PrinterInfo(k, p.label, p.tools, "Prusa", "prusa", "prusa") for k, p in PRUSA_PRINTERS.items()},
    "u1": PrinterInfo("u1", U1_LABEL, U1_TOOLS, "Snapmaker", "u1", "snapmaker"),
}


def info(key: str) -> PrinterInfo:
    """The printer's record; a KeyError for a key SlicerBridge doesn't know."""
    return INFO[key]


def engine_of(key: str) -> str:
    """Which engine writes the file for this printer key: orca, prusa or u1."""
    return INFO[key].engine


def label_of(key: str) -> str:
    return INFO[key].label


def tools_of(key: str) -> int:
    return INFO[key].tools


def vendor_of(key: str) -> str:
    return INFO[key].vendor


ALL = list(INFO)

# Named selections for --printers, beside "all" and any vendor (bambu, creality,
# elegoo, anycubic, flashforge, qidi, wondermaker, prusa, snapmaker). "popular": the printers
# most downloaders own, one file each.
SETS = {
    "popular": ["a1mini", "a1", "p1s", "p2s", "x1c", "h2d",
                "k2plus", "k2pro", "hi", "k1c", "cc", "kobras1", "kobra3", "ad5x", "q2", "plus4",
                "mmu3", "xl", "u1"],
}
VENDOR_SETS = {"bambu": "BBL"}  # set name -> vendor, where the two differ


def select(spec: list[str] | None, default: str = "popular") -> list[str]:
    """Printer keys from --printers values: 'all', a set, a vendor, or keys, comma-separated or repeated."""
    keys: list[str] = []
    vendors = {vendor_of(k).lower() for k in ALL}
    for item in (spec or [default]):
        for word in filter(None, (w.strip().lower() for w in item.split(","))):
            if word == "all":
                group = ALL
            elif word in SETS:
                group = SETS[word]
            elif word in VENDOR_SETS or word in vendors:
                vendor = VENDOR_SETS.get(word, word).lower()
                group = [k for k in ALL if vendor_of(k).lower() == vendor]
            else:
                group = [word]
            for k in group:
                if k not in ALL:
                    raise SystemExit(f"!! unknown printer {k!r}: use all, {', '.join(SETS)}, a vendor "
                                     f"({', '.join(sorted(vendors | set(VENDOR_SETS)))}), or any of {', '.join(ALL)}")
                if k not in keys:
                    keys.append(k)
    return keys


# The filament types SlicerBridge converts: each one Bambu Studio's filament_type names, and
# each a printer's own preset of that same type stands for (PRINTERS). A type is never
# printed as another. The fibre-filled ones (PLA-CF, PETG-CF, PA-CF) are their own types: they
# need a hardened nozzle, and their presets print slower.
MAIN_TYPES = ("PLA", "PETG", "PCTG", "ABS", "ASA", "PC", "PA", "PLA-CF", "PETG-CF", "PA-CF",
              "HIPS", "PVA", "BVOH", "TPU", "TPU-AMS")

# Source filament_type -> the type a printer's preset stands for. Anything else is
# refused: a silk, matte or galaxy PLA is still "PLA" in Bambu's filament_type, and
# Snapmaker Orca's PETG-HF is PETG.
# Bambu has two TPUs, kept apart: "TPU" is soft (95A and softer, Generic TPU's
# 3.2 mm^3/s), which an AMS can't feed; "TPU-AMS", Bambu's TPU for AMS, is about
# 68D, feeds through the AMS and prints at 10.5 to 18. A TPU never becomes the other.
BAMBU_TYPES = {**{t: t for t in MAIN_TYPES}, "PETG-HF": "PETG"}


def name_suffix(text: str) -> str:
    """What --suffix adds after the printer: "Unicorn Graveyard" (or " - Unicorn Graveyard") is
    " - Unicorn Graveyard", so the printer stays a field of its own that split_name() finds."""
    text = text.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return f" - {text}" if text else ""


def out_name(name: str, key: str, prefix: str = "", plate: str = "", suffix: str = "") -> str:
    """The file name for model `name` on printer `key`: the printer named in full,
    so a downloader can print and go without guessing which file is theirs.
    `<prefix><name> - <printer>[ - Plate n][ - <suffix>].3mf`."""
    return f"{prefix}{name} - {label_of(key)}{plate}{name_suffix(suffix)}.3mf"


def out_path(out: Path, name: str, key: str, prefix: str = "", suffix: str = "") -> Path:
    return out / name / out_name(name, key, prefix, suffix=suffix)


def split_name(filename: str) -> tuple[str, str | None]:
    """(what comes before the printer, the printer key) of a file named by out_name(), with
    or without a suffix after the printer; (the stem, None) when no printer is named.

    A label counts where it's a whole " - " field: "QIDI Q2" isn't found in "QIDI Q2C",
    nor "Bambu Lab A1" in "Bambu Lab A1 mini". Of several, the last one wins, so a
    model named after a printer still reads right.
    """
    stem = filename[:-4] if filename.lower().endswith(".3mf") else filename
    best = None
    for key in ALL:
        field = f" - {label_of(key)}"
        start = stem.rfind(field)
        while start >= 0:
            rest = stem[start + len(field):]
            if rest == "" or rest.startswith(" - "):
                if best is None or start > best[0] or (start == best[0] and len(field) > best[2]):
                    best = (start, key, len(field))
                break
            start = stem.rfind(field, 0, start)
    return (stem[:best[0]], best[1]) if best else (stem, None)


def printer_of(path: Path) -> str | None:
    """The orca-engine PRINTERS key a file is named for, if any."""
    key = split_name(path.name)[1]
    return key if key in PRINTERS else None

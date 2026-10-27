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


def run_slicer(cmd: list, timeout: int, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """Run a slicer's command line and capture what it says.

    Its output is read as UTF-8 whatever the system's code page, and on Windows
    it gets no console window of its own (the MCP server has none to share).
    """
    return subprocess.run([str(c) for c in cmd], capture_output=True, cwd=cwd or tempfile.gettempdir(), text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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
        windows=(r"%ProgramFiles%\OrcaSlicer\orca-slicer.exe",))), "OrcaFilamentLibrary"),
    "bambu": Slicer("Bambu Studio", _app("SLICERBRIDGE_BAMBU_STUDIO", *_installs(
        mac=("/Applications/BambuStudio.app", "~/Applications/BambuStudio.app"),
        windows=(r"%ProgramFiles%\Bambu Studio\bambu-studio.exe",)))),
    # On Windows, prusa-slicer.exe is the window and prints nothing: the console one answers.
    "prusa": Slicer("PrusaSlicer", _app("SLICERBRIDGE_PRUSASLICER", *_installs(
        mac=("/Applications/PrusaSlicer.app", "/Applications/Original Prusa Drivers/PrusaSlicer.app",
             "~/Applications/PrusaSlicer.app"),
        windows=(r"%ProgramFiles%\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",)))),
}

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


def _generic(prefix: str, suffix: str, types=("PLA", "PETG", "ABS")) -> dict:
    return {t: f"{prefix}{t}{suffix}" for t in types}


PRINTERS = {
    # ---- Bambu Lab, from Bambu Studio's own profiles. An AMS chains up to 4
    # units (16 spools); the A1 and A1 mini's AMS lite holds 4.
    "a1mini":   Printer("Bambu Lab A1 mini", "BBL", "Bambu Lab A1 mini 0.4 nozzle", "0.16mm Optimal @BBL A1M",
                        _generic("Generic ", " @BBL A1M", ("PLA", "PETG")), 4, "bambu"),
    "a1":       Printer("Bambu Lab A1", "BBL", "Bambu Lab A1 0.4 nozzle", "0.16mm Optimal @BBL A1",
                        _generic("Generic ", " @BBL A1"), 4, "bambu"),
    "p1s":      Printer("Bambu Lab P1S", "BBL", "Bambu Lab P1S 0.4 nozzle", "0.16mm Optimal @BBL X1C",
                        _generic("Generic ", ""), 16, "bambu"),
    "p2s":      Printer("Bambu Lab P2S", "BBL", "Bambu Lab P2S 0.4 nozzle", "0.16mm Standard @BBL P2S",
                        _generic("Generic ", " @BBL P2S"), 16, "bambu"),
    "x1c":      Printer("Bambu Lab X1 Carbon", "BBL", "Bambu Lab X1 Carbon 0.4 nozzle", "0.16mm Optimal @BBL X1C",
                        _generic("Generic ", ""), 16, "bambu"),
    "h2s":      Printer("Bambu Lab H2S", "BBL", "Bambu Lab H2S 0.4 nozzle", "0.16mm Standard @BBL H2S",
                        {"PLA": "Generic PLA @BBL H2S", "PETG": "Generic PETG @BBL H2S",
                         "ABS": "Generic ABS @BBL H2S 0.4 nozzle"}, 16, "bambu"),
    "h2d":      Printer("Bambu Lab H2D", "BBL", "Bambu Lab H2D 0.4 nozzle", "0.16mm Standard @BBL H2D",
                        _generic("Generic ", " @BBL H2D"), 16, "bambu"),
    "h2dpro":   Printer("Bambu Lab H2D Pro", "BBL", "Bambu Lab H2D Pro 0.4 nozzle", "0.16mm Standard @BBL H2DP",
                        _generic("Generic ", " @BBL H2DP"), 16, "bambu"),
    "h2c":      Printer("Bambu Lab H2C", "BBL", "Bambu Lab H2C 0.4 nozzle", "0.16mm Standard @BBL H2C",
                        _generic("Generic ", " @BBL H2C 0.4 nozzle"), 16, "bambu"),
    # Two nozzles, like the H2D: the left one direct drive, the right one Bowden (meant for supports),
    # which doesn't reach the bed's left 20.5 mm.
    "x2d":      Printer("Bambu Lab X2D", "BBL", "Bambu Lab X2D 0.4 nozzle", "0.16mm Standard @BBL X2D",
                        _generic("Generic ", " @BBL X2D 0.4 nozzle"), 16, "bambu"),

    # ---- Creality (CFS), from OrcaSlicer.
    "k2plus":   Printer("Creality K2 Plus", "Creality", "Creality K2 Plus 0.4 nozzle",
                        "0.16mm Optimal @Creality K2 Plus 0.4 nozzle", _generic("Creality Generic ", " @K2-all")),
    "k2pro":    Printer("Creality K2 Pro", "Creality", "Creality K2 Pro 0.4 nozzle",
                        "0.16mm Optimal @Creality K2 Pro 0.4 nozzle", _generic("Creality Generic ", " @K2-all")),
    # Not here: the plain Creality K2. OrcaSlicer 2.4.2's own K2 preset cuts filament
    # with a 30 mm long retraction, outside the 10-18 mm its CLI accepts, so the CLI
    # won't load it -- and shortening a cut retraction risks jams in the CFS.
    # The K2 SE and the K1C with CFS-C use the shared K2-all / K1-all filaments Creality makes their defaults.
    "k2se":     Printer("Creality K2 SE", "Creality", "Creality K2 SE 0.4 nozzle",
                        "0.16mm Standard @Creality K2 SE 0.4 nozzle", _generic("Creality Generic ", " @K2-all")),
    "hi":       Printer("Creality Hi", "Creality", "Creality Hi 0.4 nozzle", "0.16mm Optimal @Creality Hi",
                        _generic("Creality Generic ", " @Hi-all")),
    "k1c":      Printer("Creality K1C CFS", "Creality", "Creality K1C_CFS-C 0.4 nozzle",
                        "0.16mm Optimal @Creality K1C_CFS-C 0.4 nozzle", _generic("Creality Generic ", " @K1-all")),

    # ---- Elegoo (CANVAS). OrcaSlicer 2.4.2's profile drives it; 2.3.1's stopped for M600.
    "cc":       Printer("Elegoo Centauri Carbon", "Elegoo", "Elegoo Centauri Carbon 0.4 nozzle",
                        "0.16mm Optimal @Elegoo CC 0.4 nozzle",
                        {"PLA": "Elegoo PLA @ECC", "PETG": "Elegoo PETG @ECC", "ABS": "Generic ABS @Elegoo Centauri"}),

    # ---- Anycubic (ACE Pro). The Kobra S1 has no 0.16 mm preset; 0.16 mm is carried over anyway.
    "kobras1":  Printer("Anycubic Kobra S1", "Anycubic", "Anycubic Kobra S1 0.4 nozzle",
                        "0.20mm Standard @Anycubic Kobra S1 0.4 nozzle",
                        _generic("Anycubic ", " @Anycubic Kobra S1 0.4 nozzle")),
    "kobras1max": Printer("Anycubic Kobra S1 Max", "Anycubic", "Anycubic Kobra S1 Max 0.4 nozzle",
                          "0.16mm Standard @Anycubic Kobra S1 Max 0.4 nozzle",
                          {"PLA": "Anycubic PLA @Anycubic Kobra S1 Max 0.4 nozzle",
                           "PETG": "Generic PETG @Anycubic Kobra S1 Max 0.4 nozzle",
                           "ABS": "Anycubic ABS @Anycubic Kobra S1 Max 0.4 nozzle"}),
    "kobra3":   Printer("Anycubic Kobra 3", "Anycubic", "Anycubic Kobra 3 0.4 nozzle",
                        "0.16mm Optimal @Anycubic Kobra 3 0.4 nozzle", _generic("Anycubic Generic ", "")),
    "kobra3max": Printer("Anycubic Kobra 3 Max", "Anycubic", "Anycubic Kobra 3 Max 0.4 nozzle",
                         "0.16mm Standard @Anycubic Kobra 3 Max 0.4 nozzle", _generic("Anycubic Generic ", "")),
    "kobrax":   Printer("Anycubic Kobra X", "Anycubic", "Anycubic Kobra X 0.4 nozzle",
                        "0.16mm Standard @Anycubic Kobra X",
                        {"PLA": "Anycubic PLA @Anycubic Kobra X 0.4 nozzle",
                         "PETG": "Generic PETG @Anycubic Kobra X 0.4 nozzle",
                         "ABS": "Anycubic ABS @Anycubic Kobra X 0.4 nozzle"}),

    # ---- Flashforge: the AD5X (IFS), and the Creator 5 and 5 Pro, 4-toolhead toolchangers.
    "ad5x":     Printer("Flashforge AD5X", "Flashforge", "Flashforge AD5X 0.4 nozzle", "0.16mm Standard @FF AD5X",
                        _generic("Flashforge Generic ", "")),
    "creator5": Printer("Flashforge Creator 5", "Flashforge", "Flashforge Creator 5 0.4 nozzle",
                        "0.20mm Standard @FF C5",
                        {"PLA": "Generic PLA @FF C5", "PETG": "Flashforge PETG Pro @FF C5",
                         "ABS": "Flashforge ABS Basic @FF C5"}),
    "creator5pro": Printer("Flashforge Creator 5 Pro", "Flashforge", "Flashforge Creator 5 Pro 0.4 nozzle",
                           "0.20mm Standard @FF C5",
                           {"PLA": "Generic PLA @FF C5P", "PETG": "Flashforge PETG Pro @FF C5P",
                            "ABS": "Flashforge ABS Basic @FF C5P"}),

    # ---- QIDI (QIDI Box). Sold as the Plus4 and Max4; OrcaSlicer still calls them X-Plus 4 / X-Max 4.
    "q2":       Printer("QIDI Q2", "Qidi", "Qidi Q2 0.4 nozzle", "0.16mm Optimal @Qidi Q2",
                        _generic("Generic ", " @Qidi Q2 0.4 nozzle")),
    "q2c":      Printer("QIDI Q2C", "Qidi", "Qidi Q2C 0.4 nozzle", "0.16mm Optimal @Qidi Q2C",
                        _generic("Generic ", " @Qidi Q2C 0.4 nozzle")),
    "plus4":    Printer("QIDI Plus4", "Qidi", "Qidi X-Plus 4 0.4 nozzle", "0.16mm Optimal @Qidi XPlus4",
                        _generic("Qidi Generic ", " @Qidi X-Plus 4 0.4 nozzle")),
    "max4":     Printer("QIDI Max4", "Qidi", "Qidi X-Max 4 0.4 nozzle", "0.16mm Standard @X-Max 4",
                        _generic("Generic ", " @Qidi X-Max 4 0.4 nozzle")),

    # ---- WonderMaker: the ZR Ultra S, a 4-toolhead toolchanger.
    "zrultras": Printer("WonderMaker ZR Ultra S", "WonderMaker", "WonderMaker ZR Ultra S 0.4 nozzle",
                        "0.16mm Optimal @WonderMaker ZR Ultra",
                        {"PLA": "WonderMaker PLA Basic", "PETG": "WonderMaker PETG Basic",
                         "ABS": "WonderMaker ABS @WonderMaker ZR Ultra S"}),
}


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


def engine_of(key: str) -> str:
    """Which engine writes the file for this printer key: orca, prusa or u1."""
    if key in PRINTERS:
        return "orca"
    if key in PRUSA_PRINTERS:
        return "prusa"
    if key == "u1":
        return "u1"
    raise KeyError(key)


def label_of(key: str) -> str:
    return {"orca": lambda: PRINTERS[key].label, "prusa": lambda: PRUSA_PRINTERS[key].label,
            "u1": lambda: U1_LABEL}[engine_of(key)]()


def tools_of(key: str) -> int:
    return {"orca": lambda: PRINTERS[key].tools, "prusa": lambda: PRUSA_PRINTERS[key].tools,
            "u1": lambda: U1_TOOLS}[engine_of(key)]()


def vendor_of(key: str) -> str:
    return {"orca": lambda: PRINTERS[key].vendor, "prusa": lambda: "Prusa", "u1": lambda: "Snapmaker"}[engine_of(key)]()


ALL = [*PRINTERS, *PRUSA_PRINTERS, "u1"]

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


# Bambu filament_type -> the type a printer's preset stands for. Anything else is
# refused: a silk, matte or galaxy PLA is still "PLA" in Bambu's filament_type.
BAMBU_TYPES = {"PLA": "PLA", "PETG": "PETG", "PETG-HF": "PETG", "ABS": "ABS"}


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

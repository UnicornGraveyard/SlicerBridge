"""
slicerbridge -- any multicolor 3MF project in, a ready file for every multicolor printer out.

Reads projects saved by Bambu Studio, OrcaSlicer, or a maker's fork of either (Creality Print, ElegooSlicer,
Anycubic Slicer Next, Flash Studio, QIDI Studio), including any file SlicerBridge wrote.

    slicerbridge convert model.3mf                         # the popular printers
    slicerbridge convert model.3mf --printers all --slice  # every printer, each file sliced first
    slicerbridge convert model.3mf --printers creality,u1 --name "My Model"
    slicerbridge convert "Dragon - Creality K2 Plus.3mf" --printers bambu   # retarget a file you have
    slicerbridge inspect model.3mf                         # colors used, plates, fit on each bed
    slicerbridge printers                                  # every printer, its engine and slicer
    slicerbridge verify "out/My Model" --source model.3mf --slice
    slicerbridge brand-check "out/My Model"                # slice each file in its brand's own slicer
    slicerbridge templates --refresh --printers all        # after updating a slicer
    slicerbridge doctor                                    # what's installed, where things are kept
    slicerbridge mcp                                       # the MCP server, for AI assistants
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
import zipfile
from pathlib import Path

from . import __version__
from .errors import Refusal
from .printers import (ALL, BRAND_SLICERS, PRINTERS, PRUSA_PRINTERS, SETS, SLICERS, U1_LABEL, cache_dir,
                       engine_of, file_safe, label_of, printer_of, select, tools_of, vendor_of)

def _engine(name: str):
    """Import an engine only when it's used: they pull in Panda2Prusa and lxml."""
    return importlib.import_module(f".engines.{name}", __package__)


def _by_engine(keys: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(engine_of(k), []).append(k)
    return groups


def _parse_map(text: str | None) -> dict | None:
    if not text:
        return None
    mapping = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        old, sep, new = pair.partition("=")
        if not sep or not old.strip().isdigit() or not new.strip().isdigit():
            raise SystemExit(f"!! bad --map entry {pair!r}; expected e.g. --map 10=2,11=3")
        mapping[int(old)] = int(new)
    return mapping


def _abs(path: str) -> Path:
    """A path the slicers can open: they run in the temp folder, so a relative one isn't found there."""
    return Path(path).expanduser().resolve()


def _files(paths: list[str]) -> list[Path]:
    """The .3mf files named, and those in the folders named (one level down too)."""
    out: list[Path] = []
    for p in map(_abs, paths):
        if p.is_dir():
            out += sorted(f for f in [*p.glob("*.3mf"), *p.glob("*/*.3mf")]
                          if not f.name.startswith("partial - ") and "_previous" not in f.parts)
        elif p.suffix.lower() == ".3mf":
            out.append(p)
        else:
            raise SystemExit(f"!! not a .3mf or a folder: {p}")
    return out


def _report(f: Path, info: list[str], problems: list[str], warnings: list[str]) -> bool:
    print("=" * 78)
    print(f.name)
    for line in info + [f"⚠ {w}" for w in warnings]:
        print(f"  {line}")
    if problems:
        print("  RESULT    : ✗ FAIL")
        for p in problems:
            print(f"              !! {p}")
        return False
    print("  RESULT    : ✓ pass" + (f" ({len(warnings)} warning{'s' * (len(warnings) != 1)})" if warnings else ""))
    return True


def _model(path: str) -> Path:
    """A project SlicerBridge can read: a multicolor .3mf saved by Bambu Studio, OrcaSlicer or a fork of either."""
    src = _abs(path)
    if src.suffix.lower() != ".3mf" or not src.is_file():
        raise SystemExit(f"!! not a .3mf file: {src}")
    try:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        raise SystemExit(f"!! {src.name} isn't a 3MF (not a zip archive)") from None
    if "Metadata/project_settings.config" not in names:
        if "Metadata/Slic3r_PE.config" in names:
            raise SystemExit(f"!! {src.name} is a PrusaSlicer project. SlicerBridge reads projects saved by Bambu "
                             f"Studio, OrcaSlicer or a maker's fork of either; PrusaSlicer projects aren't read yet.")
        raise SystemExit(f"!! {src.name} carries no slicer project (a mesh-only 3MF?): open it in Bambu Studio or "
                         f"OrcaSlicer, give it its colors, and save the project")
    return src


def _default_name(src: Path) -> str:
    """The file's name, without a printer SlicerBridge named it for: "Cube - QIDI Q2" is "Cube"."""
    labels = sorted((label_of(k) for k in ALL), key=len, reverse=True)
    stem = re.sub(r" - Plate \d+$", "", src.stem)
    for label in labels:
        if stem.endswith(f" - {label}"):
            return stem[: -len(f" - {label}")]
    return src.stem


def cmd_convert(a: argparse.Namespace) -> int:
    src = _model(a.model)
    given = a.name or _default_name(src)
    # Windows can't open a file named with < > : " / \ | ? *, or ending in a dot or space.
    name, prefix = file_safe(given).rstrip(". "), file_safe(a.prefix)
    if not name:
        raise SystemExit(f'!! "{given}" leaves nothing to name the files with: give --name a word')
    if (name, prefix) != (given, a.prefix):
        print(f'"{a.prefix}{given}" isn\'t a file name Windows can open: naming the files "{prefix}{name} - <printer>.3mf"')
    keys = select(a.printers)
    explicit = _parse_map(a.map)
    if explicit and set(_by_engine(keys)) != {"prusa"}:
        raise SystemExit("!! --map is for Prusa printers only: pick them with --printers prusa (or keys)")
    made = failed = 0
    for engine, ks in _by_engine(keys).items():
        mod = _engine(engine)
        kwargs = {"explicit": explicit} if engine == "prusa" else {}
        m, f = mod.convert(src, name, ks, _abs(a.out), prefix, a.slice, **kwargs)
        made, failed = made + m, failed + f
    print(f"\n{made} file(s) written to {Path(a.out) / name}, {failed} refused or failed.")
    return 1 if failed else 0


def cmd_inspect(a: argparse.Namespace) -> int:
    src = _model(a.model)
    keys = select(a.printers)
    groups = _by_engine(keys)
    print(f"{src.name}")
    for line in _engine("orca").inspect(src, groups.get("orca", [])):
        print(f"  {line}")
    if "prusa" in groups:
        for line in _engine("prusa").inspect(src, groups["prusa"]):
            print(f"  {line}")
    if "u1" in groups:
        for line in _engine("u1").inspect(src):
            print(f"  {line}")
    return 0


def cmd_printers(a: argparse.Namespace) -> int:
    keys = select(a.printers, default="all")
    for k in keys:
        engine = engine_of(k)
        slicer = (SLICERS[PRINTERS[k].slicer].name if engine == "orca" else
                  SLICERS["prusa"].name if engine == "prusa" else "Snapmaker Orca")
        brand = BRAND_SLICERS.get(vendor_of(k))
        sets = [n for n, members in SETS.items() if k in members]
        print(f"  {k:11} {label_of(k):26} {engine:6} {slicer:13} up to {tools_of(k):>2} colors"
              + (f"   brand slicer: {brand.name}" if brand else "")
              + (f"   [{', '.join(sets)}]" if sets else ""))
    print(f"\n{len(keys)} printers. Select with --printers: all, {', '.join(SETS)}, a vendor, or keys.")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    from .checks import orca as orca_check, u1 as u1_check
    files = _files(a.paths)
    src = _abs(a.source) if a.source else None
    if not files:
        print("No .3mf files found.")
        return 0
    bad = 0
    for f in files:
        key = printer_of(f)
        if key:
            try:
                info, problems, warnings = orca_check.audit(f, src, key)
                if a.slice and not problems:
                    si, sp, sw = orca_check.slice_check(f, key)
                    info, problems, warnings = info + si, problems + sp, warnings + sw
            except Exception as e:  # one file's failure doesn't stop the others being checked
                info, problems, warnings = [], [f"the check itself failed: {type(e).__name__}: {e}"], []
        elif f.name.endswith(f" - {U1_LABEL}.3mf"):
            (info, problems), warnings = u1_check.audit(f, src), []
        elif any(p.label in f.name for p in PRUSA_PRINTERS.values()):
            info, problems, warnings = [], [], ["Prusa files are checked while they're written: convert again to re-check"]
        else:
            info, problems, warnings = [], ["not named for a printer SlicerBridge knows (see slicerbridge printers)"], []
        bad += not _report(f, info, problems, warnings)
    print("=" * 78)
    print(f"{len(files) - bad}/{len(files)} passed.")
    return 1 if bad else 0


def cmd_brand_check(a: argparse.Namespace) -> int:
    from . import brandcheck
    keys = set(select(a.printers, default="all"))
    files = [f for f in _files(a.paths) if printer_of(f) in keys]
    bad = checked = 0
    for f in files:
        slicer, info, problems, warnings = brandcheck.check(f)
        if slicer is None:
            continue  # Bambu Lab: Bambu Studio is its brand slicer, and verify --slice uses it
        checked += 1
        bad += not _report(f, [f"slicer    : {slicer}", *info], problems, warnings)
    print("=" * 78)
    print(f"{checked - bad}/{checked} passed in their brand's own slicer.")
    return 1 if bad else 0


def cmd_templates(a: argparse.Namespace) -> int:
    from . import templates
    keys = select(a.printers, default="all")
    failed = 0
    for engine, ks in _by_engine(keys).items():
        for k in ks:
            if engine == "orca":
                if not a.refresh and (templates.TEMPLATES / k / "meta.json").exists():
                    print(f"  have {label_of(k)}")
                    continue
                problems = templates.build(k)
                print(f"  {'ok  ' if not problems else 'FAIL'} {label_of(k)}" + "".join(f"\n       !! {p}" for p in problems))
                failed += bool(problems)
            elif engine == "prusa":
                prusa = _engine("prusa")
                if not a.refresh and (prusa.PROFILES / f"{k}.ini").exists():
                    print(f"  have {label_of(k)}")
                    continue
                try:
                    prusa.refresh_profile(k)
                except Refusal as e:
                    print(f"  FAIL {label_of(k)}\n       !! {e}")
                    failed += 1
            else:
                print(f"  {label_of(k)}: built in (bl2u1's U1 templates)")
    return 1 if failed else 0


TESTED_ORCASLICER = "2.4.2"


def _older(version: str, than: str) -> bool:
    def parts(v: str) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    return bool(parts(version)) and parts(version) < parts(than)


def cmd_doctor(a: argparse.Namespace) -> int:
    from . import templates
    print(f"SlicerBridge {__version__}, Python {sys.version.split()[0]}")
    try:
        import lxml  # noqa: F401

        from ._vendor import panda2prusa  # noqa: F401
        print("  dependencies : ok (lxml, Panda2Prusa vendored)")
    except ImportError as e:
        print(f"  dependencies : MISSING ({e}) -- pip install slicerbridge again")
    print(f"  cache        : {cache_dir()}")
    print("\nSlicers that build files:")
    for key, s in SLICERS.items():
        version = ""
        if s.installed:
            try:
                version = templates.app_version(s)
                found = re.search(r"\d+(?:\.\d+)+", version)
                version = found.group(0) if found else version
            except Exception:  # an odd install still shows up as found
                version = "?"
        print(f"  {s.name:22} {'found ' + version if s.installed else 'not found'}  {s.app}")
        if key == "orca" and s.installed and _older(version, TESTED_ORCASLICER):
            print(f"  {'':22} ⚠ SlicerBridge is tested with OrcaSlicer {TESTED_ORCASLICER}: older profiles stop for a\n"
                  f"  {'':22}   manual color change on some printers (Creality K1C, Elegoo Centauri Carbon). Install\n"
                  f"  {'':22}   {TESTED_ORCASLICER} beside it and set SLICERBRIDGE_ORCASLICER (docs/install.md).")
    print("\nBrand slicers (brand-check only):")
    for vendor, s in BRAND_SLICERS.items():
        print(f"  {s.name:22} {'found' if s.installed else 'not found'}  {s.app}")
    built = sorted(p.parent.name for p in templates.TEMPLATES.glob("*/meta.json"))
    print(f"\nTemplates built: {len(built)}/{len(PRINTERS)}" + (f" ({', '.join(built)})" if built else ""))
    print("\nEnvironment variables override where each slicer is looked for: SLICERBRIDGE_ORCASLICER,\n"
          "SLICERBRIDGE_BAMBU_STUDIO, SLICERBRIDGE_PRUSASLICER, SLICERBRIDGE_CREALITY_PRINT, ... (docs/install.md)")
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    from . import mcp_server
    mcp_server.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="slicerbridge", description=__doc__.split("\n\n")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    ap.add_argument("--version", action="version", version=f"slicerbridge {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    which = dict(action="append", metavar="WHICH",
                 help="all, a set (" + ", ".join(SETS) + "), a vendor (bambu, creality, prusa, ...), or printer keys; "
                      "comma-separated or repeated")

    c = sub.add_parser("convert", help="write a file per printer for one Bambu project")
    c.add_argument("model", help="a multicolor .3mf project from Bambu Studio, OrcaSlicer or a fork of either")
    c.add_argument("--name", help="the model's name in file names (default: the file's name, less any printer SlicerBridge put in it)")
    c.add_argument("--printers", "-p", **which)
    c.add_argument("--out", "-o", default="out", help="output folder; files go in <out>/<name>/ (default: out)")
    c.add_argument("--prefix", default="", help='text before every file name, e.g. "My Shop - "')
    c.add_argument("--slice", action="store_true", help="slice each file in its slicer before keeping it")
    c.add_argument("--map", help="Prusa only: explicit slot -> tool map, e.g. 1=1,10=2,11=3")
    c.set_defaults(run=cmd_convert)

    i = sub.add_parser("inspect", help="colors used, plates and fit on each bed")
    i.add_argument("model", help="a multicolor .3mf project")
    i.add_argument("--printers", "-p", **which)
    i.set_defaults(run=cmd_inspect)

    p = sub.add_parser("printers", help="every printer, its engine, slicer and colors")
    p.add_argument("--printers", "-p", **which)
    p.set_defaults(run=cmd_printers)

    v = sub.add_parser("verify", help="re-run the checks on written files")
    v.add_argument("paths", nargs="+", help=".3mf files or folders")
    v.add_argument("--source", help="the project they were made from (enables the color and size checks)")
    v.add_argument("--slice", action="store_true", help="also slice each file in its slicer")
    v.set_defaults(run=cmd_verify)

    b = sub.add_parser("brand-check", help="slice each file in its brand's own slicer")
    b.add_argument("paths", nargs="+", help=".3mf files or folders")
    b.add_argument("--printers", "-p", **which)
    b.set_defaults(run=cmd_brand_check)

    t = sub.add_parser("templates", help="build each printer's settings from its slicer")
    t.add_argument("--refresh", action="store_true", help="rebuild even if built")
    t.add_argument("--printers", "-p", **which)
    t.set_defaults(run=cmd_templates)

    d = sub.add_parser("doctor", help="what's installed and where things are kept")
    d.set_defaults(run=cmd_doctor)

    m = sub.add_parser("mcp", help="run the MCP server over stdio, for AI assistants (docs/mcp.md)")
    m.set_defaults(run=cmd_mcp)
    return ap


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # not when the MCP server captures it
        sys.stdout.reconfigure(line_buffering=True)  # a long run shows each file as it lands
        # Windows writes a pipe or a file in its ANSI code page, which has no ✓ or ⚠: the
        # first warning ended the run with a UnicodeEncodeError, files still to write.
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
    a = build_parser().parse_args(argv)
    try:
        return a.run(a)
    except Refusal as e:
        print(f"!! {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

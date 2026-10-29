"""
templates.py -- each printer's settings, written by its own slicer.

For every printer and filament type in printers.PRINTERS, the printer's slicer
(OrcaSlicer, or Bambu Studio for Bambu Lab) loads the printer, print and
filament presets onto a 20 mm cube from its command line and saves the project:
<cache>/templates/<printer>/<TYPE>.3mf. Its project_settings.config is the complete
settings that slicer would save for that printer with one filament of that type
-- every key, every default -- so the orca engine composes a model's settings
from those, one filament column per color, instead of guessing.

The CLI half-applies several filaments loaded at once (filament_type stays one
long while nozzle_temperature grows), hence one filament per file.

Standard library only; needs the slicers in printers.SLICERS.
"""
from __future__ import annotations

import json
import re
import plistlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .errors import Refusal
from .printers import PRINTERS, SLICERS, TEMPLATES, Slicer, ensure_installed, not_found, run_slicer

# Not filament settings, though some filament preset files carry them.
NOT_FILAMENT = {"from", "name", "inherits", "instantiation", "setting_id", "filament_id",
                "compatible_printers", "compatible_printers_condition", "compatible_prints",
                "compatible_prints_condition", "type", "version", "is_custom_defined",
                "default_filament_profile", "description"}


def app_version(slicer: Slicer) -> str:
    """The slicer's version: a template built by another version is rebuilt."""
    if slicer.app.suffix == ".app":
        with open(slicer.app / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)["CFBundleShortVersionString"]
    if sys.platform == "win32" and (version := _exe_version(slicer.binary)):
        return version
    run = run_slicer([slicer.binary, *slicer.cli, "--help"], timeout=120)
    found = re.search(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b", run.stdout + run.stderr)
    return found.group(1) if found else "unknown"


def _exe_version(exe: Path) -> str | None:
    """A Windows program's version, from the .exe itself: these slicers' --help doesn't say."""
    import ctypes
    from ctypes import wintypes

    lib = ctypes.WinDLL("version")
    size = lib.GetFileVersionInfoSizeW(str(exe), None)
    data = ctypes.create_string_buffer(size)
    if not size or not lib.GetFileVersionInfoW(str(exe), 0, size, data):
        return None
    info, length = ctypes.c_void_p(), wintypes.UINT()
    if not lib.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(length)) or not info.value:
        return None
    fixed = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD * 13)).contents  # VS_FIXEDFILEINFO
    ms, ls = fixed[4], fixed[5]  # dwProductVersionMS, dwProductVersionLS
    parts = [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
    return ".".join(str(n) for n in (parts if parts[3] else parts[:3]))


def preset_index(vendor: str, slicer: Slicer = SLICERS["orca"]) -> dict:
    """{(kind, preset name): file} for one vendor's bundled profiles in a slicer.

    Filament presets may inherit from the slicer's shared filament library
    (OrcaSlicer's OrcaFilamentLibrary), so that's indexed too -- under the
    vendor's own, which wins a name clash.
    """
    index = {}
    folders = ([(slicer.library, ("filament",))] if slicer.library else []) + [(vendor, ("machine", "process", "filament"))]
    for folder, kinds in folders:
        for kind in kinds:
            for f in (slicer.profiles / folder / kind).rglob("*.json"):
                try:
                    name = json.loads(f.read_text(encoding="utf-8")).get("name")
                except (ValueError, OSError):
                    continue
                if name:
                    index[(kind, name)] = f
    return index


def filament_keys(settings: dict, slicer: Slicer) -> list[str]:
    """Settings that hold one value (or one block of values) per filament in a project.

    Every key any of the slicer's bundled filament presets sets, that the
    project stores as a list -- plus the three the slicer fills in itself.
    """
    keys = set()
    for f in slicer.profiles.glob("*/filament/**/*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(d, dict):
            keys |= set(d)
    keys = {k for k in keys - NOT_FILAMENT if isinstance(settings.get(k), list)}
    keys |= {k for k in PROJECT_FILAMENT_KEYS if isinstance(settings.get(k), list)}
    return sorted(keys | {"filament_colour", "filament_ids", "filament_settings_id"})


# Per-filament settings the slicer adds to a project itself, which no filament
# preset sets -- found as the lists one long per filament in a real 15-filament
# Bambu Studio 2.8 project. filament_self_index (which filament each per-nozzle
# entry belongs to) is renumbered by engines.orca.compose().
PROJECT_FILAMENT_KEYS = {
    "default_filament_colour", "filament_is_mixed", "filament_map", "filament_mixed_components",
    "filament_mixed_gradient", "filament_mixed_gradient_curve", "filament_mixed_gradient_per_part",
    "filament_mixed_gradient_range", "filament_mixed_sublayer_ratios", "first_x_layer_part_fan_speed",
    "ironing_fan_speed", "overhang_threshold_participating_cooling",
}


# Bed types by the number a printer preset's default_bed_type may give
# (OrcaSlicer's BedType enum, PrintConfig.hpp).
BED_TYPES = {1: "Cool Plate", 2: "Engineering Plate", 3: "High Temp Plate", 4: "Textured PEI Plate",
             5: "Textured Cool Plate", 6: "Supertack Plate"}


def default_bed_type(settings: dict) -> str:
    """The plate OrcaSlicer's app picks for a new project on this printer.

    The CLI leaves curr_bed_type at "Cool Plate", which prints PLA at 35 C on most
    of these printers. The app takes the printer preset's default_bed_type, and
    otherwise the High Temp (smooth PEI) plate -- Preset::get_default_bed_type
    in OrcaSlicer 2.3.1. On a printer without multiple bed types it forces that
    choice even when opening a project, so it's the only right answer there.
    """
    raw = str(settings.get("default_bed_type") or "").strip()
    if raw in BED_TYPES.values():
        return raw
    return BED_TYPES.get(int(raw) if raw.isdigit() else 0, "High Temp Plate")


def bed_type_for(key: str, template: dict, source: dict | None) -> str:
    """The plate a file for printer `key` names.

    Bambu Lab sells one set of plates for all its printers, so a Bambu target
    keeps the plate the designer chose (Textured PEI when it names none). An
    Orca-family printer gets the plate OrcaSlicer's app would pick for it.
    """
    if PRINTERS[key].slicer == "bambu":
        return (source or {}).get("curr_bed_type") or "Textured PEI Plate"
    return default_bed_type(template)


# A filament change the printer can't make on its own: OrcaSlicer's K1C profile
# says PAUSE, its Centauri Carbon profile M600 -- a stop at every color change.
MANUAL_CHANGE = ("PAUSE", "M600", "M601", "M0", "M1")


def manual_change(settings: dict) -> str | None:
    """The command that hands a filament change to a person, if the printer's change G-code has one."""
    for line in settings.get("change_filament_gcode", "").splitlines():
        word = line.split(";")[0].strip().split(" ")[0].upper()
        if word in MANUAL_CHANGE:
            return word
    return None


def _cube(path: Path) -> None:
    v = [(x, y, z) for x in (0, 20) for y in (0, 20) for z in (0, 20)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    lines = ["solid cube"]
    for face in faces:
        lines += ["facet normal 0 0 0", "outer loop", *(f"vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in face),
                  "endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid cube", ""]), encoding="utf-8")


def flatten(kind: str, name: str, index: dict) -> dict:
    """A preset with its whole `inherits` chain applied, parent first, as the app loads it."""
    chain, seen = [], set()
    while name:
        if name in seen or (kind, name) not in index:
            raise Refusal(f"{kind} preset '{name}' is missing from its own inheritance chain")
        seen.add(name)
        d = json.loads(index[(kind, name)].read_text(encoding="utf-8"))
        chain.append(d)
        name = d.get("inherits") or ""
    out: dict = {}
    for d in reversed(chain):
        out.update(d)
    return out


# Presets whose bundled value the CLI's range check refuses, though the app
# loads them: Anycubic's Kobra S1 Max and Kobra X set the long-retraction
# distance to 0 with long retraction switched off (OrcaSlicer allows 10-18).
# Unused while it's off, so it takes the bottom of the range.
CLI_RANGE_FIXES = {"retraction_distances_when_cut": ("enable_long_retraction_when_cut", "10")}


def _for_cli(kind: str, name: str, index: dict, tmp: Path, printer: str | None = None) -> Path:
    """A preset file the CLI reads the way the app does: its chain applied, standing alone.

    OrcaSlicer's CLI doesn't follow `inherits` through a vendor's base presets:
    QIDI's 0.16mm Optimal @Qidi Q2 inherits 0.16 mm layers, 0.42 mm lines and 200
    mm/s walls from two parents, and the CLI saved 0.2 mm, auto-width lines and
    60 mm/s -- OrcaSlicer's bare defaults, which don't even slice (negative flow
    spacing). It also refuses a file without "from" and a print preset naming no
    compatible printer, which Elegoo's 0.16mm Optimal @Elegoo CC leaves to its
    parent. So every preset goes in flattened, marked as the system preset it is.
    """
    d = _flat_for_cli(kind, name, index)
    d.pop("inherits", None)
    d.update({"name": name, "from": "system", "instantiation": "true"})
    if printer and not d.get("compatible_printers") and not d.get("compatible_printers_condition"):
        d["compatible_printers"] = [printer]
    out = tmp / f"{kind}-{len(list(tmp.glob(f'{kind}-*')))}.json"
    out.write_text(json.dumps(d, indent=4), encoding="utf-8")
    return out


def _flat_for_cli(kind: str, name: str, index: dict) -> dict:
    """flatten(), with CLI_RANGE_FIXES applied where their switch is off."""
    d = flatten(kind, name, index)
    for key, (switch, floor) in CLI_RANGE_FIXES.items():
        if key in d and str(d.get(switch, "0")) == "0":
            vals = d[key] if isinstance(d[key], list) else [d[key]]
            d[key] = [floor if v in ("0", 0) else v for v in vals]
    return d


def _norm(key: str, v) -> list[str]:
    """A setting as a list of canonical strings: "0.50" == "0.5", "a,b" == ["a", "b"], "" == []."""
    items = v if isinstance(v, list) else [v]
    out = []
    for item in items:
        for part in str(item).split(",") if key in ("bed_exclude_area", "thumbnails") else [str(item)]:
            part = part.strip()
            if key == "thumbnails" and part and "/" not in part:
                part += "/PNG"  # OrcaSlicer's default thumbnail format, spelled out when it saves
            if re.fullmatch(r"-?[\d.]+x-?[\d.]+", part):
                part = part.replace("x", ",")  # Bambu Studio saves a point as 0.7,0.5, its presets say 0.7x0.5
            try:
                part = repr(float(part[:-1] if part.endswith("%") else part))  # "100" and "100%" alike
            except ValueError:
                pass
            out.append(part)
    return [] if out == [""] else out


def _as_list(raw: str) -> list[list[str]]:
    """Ways a preset file may spell a list as one string: "a,b", or '"a";"b"' with escapes."""
    semi = [x.strip().strip('"').replace("\\n", "\n") for x in raw.split(";")]
    return [raw.split(","), semi]


def _legacy(key: str, raw, w: list[str], g: list[str]) -> bool:
    """Values the slicer itself rewrites when it loads a preset, app and CLI alike."""
    if key.endswith("_pattern") and w == ["zig-zag"] and g == ["rectilinear"]:
        return True  # renamed in OrcaSlicer
    if key == "wall_direction" and w == ["auto"] and g == ["ccw"]:
        return True  # OrcaSlicer 2.4 reads "auto" as counter-clockwise
    raws = raw if isinstance(raw, list) else [raw]
    if raws and all(str(r).endswith("%") for r in raws) and not any(str(x).endswith("%") for x in g):
        return True  # an option that no longer takes a percentage gets it turned into mm or mm/s
    return False


# More disagreements than this, after the formatting and legacy rewrites above,
# means the presets didn't go in: QIDI's unresolved inheritance changed dozens.
# Fewer are the loader's own adjustments, kept in meta.json and printed.
MAX_ADJUSTED = 3


def disagreements(settings: dict, presets: list[dict]) -> list[str]:
    """Keys where the saved project doesn't say what the flattened presets say."""
    out = []
    for preset in presets:
        for k, want in preset.items():
            if (k in NOT_FILAMENT or k in ("inherits", "renamed_from", "filament_vendor")
                    or k.endswith("_settings_id") or k not in settings):
                continue
            saved = settings[k]
            w, g = _norm(k, want), _norm(k, saved)
            if isinstance(want, str) and isinstance(saved, list) and len(saved) > 1:
                if any(_norm(k, cand) == g for cand in _as_list(want)):
                    continue  # a list the preset file wrote as one string
            # A preset may list a value per extruder variant; the project keeps the printer's own.
            pruned = len(g) < len(w) and set(g) <= set(w)
            if w != g and not pruned and not _legacy(k, want, w, g):
                out.append(f"{k}: preset {want!r}, saved {saved!r}")
    return out


def _rewrite_settings(path: Path, settings: dict) -> None:
    """Replace a 3MF's project_settings.config in place."""
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps(settings, indent=4).encode()
            zout.writestr(item, data)
    tmp.replace(path)


def build(key: str) -> list[str]:
    """Write <cache>/templates/<key>/<TYPE>.3mf for one printer; returns problems (empty when fine)."""
    p = PRINTERS[key]
    slicer = SLICERS[p.slicer]
    ensure_installed(slicer)
    if not slicer.binary.exists():
        return [not_found(slicer)]
    index = preset_index(p.vendor, slicer)
    machine = index.get(("machine", p.machine))
    process = index.get(("process", p.process))
    problems = [f"no {kind} preset '{name}' in {slicer.profiles / p.vendor}"
                for kind, name, f in (("machine", p.machine, machine), ("process", p.process, process)) if f is None]
    if problems:
        return problems
    flat_machine, flat_process = _flat_for_cli("machine", p.machine, index), _flat_for_cli("process", p.process, index)
    compat = flat_process.get("compatible_printers")
    if compat and p.machine not in compat:
        return [f"print preset '{p.process}' doesn't list '{p.machine}' as compatible"]

    dest = TEMPLATES / key
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        _cube(tmp / "cube.stl")
        (tmp / "out").mkdir()
        machine = _for_cli("machine", p.machine, index, tmp)
        process = _for_cli("process", p.process, index, tmp, p.machine)
        made, adjusted = {}, {}
        for ftype, name in p.filaments.items():
            if ("filament", name) not in index:
                problems.append(f"no filament preset '{name}' in {slicer.profiles / p.vendor}")
                continue
            run = run_slicer(
                [slicer.binary, "--outputdir", tmp / "out",
                 "--load-settings", f"{machine};{process}",
                 "--load-filaments", _for_cli("filament", name, index, tmp),
                 "--export-3mf", f"{ftype}.3mf", tmp / "cube.stl"],
                timeout=600, cwd=tmp)
            out = tmp / "out" / f"{ftype}.3mf"
            if run.returncode != 0 or not out.exists():
                tail = " | ".join(line for line in (run.stdout + run.stderr).splitlines()[-4:] if line.strip())
                problems.append(f"{slicer.name} couldn't save {p.label} with {name} (exit {run.returncode}): {tail}")
                continue
            with zipfile.ZipFile(out) as z:
                s = json.loads(z.read("Metadata/project_settings.config"))
            got = (s.get("printer_settings_id"), s.get("print_settings_id"), s.get("filament_settings_id"))
            if got != (p.machine, p.process, [name]):
                problems.append(f"{slicer.name} saved {got} for {p.label}, not the presets asked for")
                continue
            if slicer.name == "Bambu Studio":
                # Bambu Studio's CLI swaps in "safe" machine limits for cloud slicing
                # (cli_config.json: acceleration 6000 on an A1 mini whose preset says
                # 20000). The app keeps the preset's; so do these files.
                limits = {k: v for k, v in flat_machine.items() if k.startswith("machine_max_") and k in s}
                s.update(limits)
                _rewrite_settings(out, s)
            # The proof the presets went in whole: every key they set, as saved.
            off = disagreements(s, [flat_machine, flat_process, _flat_for_cli("filament", name, index)])
            if len(off) > MAX_ADJUSTED:
                problems.append(f"{p.label} with {name}: {slicer.name} saved {len(off)} setting(s) other than "
                                f"its presets say: " + "; ".join(off[:5]))
                continue
            adjusted[ftype] = off
            word = manual_change(s)
            if word:
                problems.append(f"{slicer.name}'s {p.machine} preset changes filament with {word}: a stop "
                                f"for a person at every color change, not a multicolor printer")
                continue
            made[ftype] = out
        if problems:
            return problems
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for ftype, out in made.items():
            shutil.move(str(out), str(dest / f"{ftype}.3mf"))

    with zipfile.ZipFile(dest / "PLA.3mf") as z:
        settings = json.loads(z.read("Metadata/project_settings.config"))
    (dest / "meta.json").write_text(json.dumps({
        "slicer": slicer.name, "slicer_version": app_version(slicer),
        "machine": p.machine, "process": p.process, "filaments": p.filaments,
        "filament_keys": filament_keys(settings, slicer),
        "slicer_adjusted": {t: a for t, a in adjusted.items() if a},
    }, indent=2) + "\n", encoding="utf-8")
    return []


def load(key: str, install: bool = True) -> tuple[dict, dict]:
    """(meta, {filament type: project settings}) for one printer.

    Built first if missing, or rebuilt if its slicer or presets have changed
    since: settings from another slicer version, or another preset, would
    disagree with the stamp and the slice check. A missing slicer is installed
    first (printers.ensure_installed) unless `install` is False.
    """
    dest = TEMPLATES / key
    p = PRINTERS[key]
    slicer = SLICERS[p.slicer]
    if install:
        ensure_installed(slicer)
    if not slicer.installed:
        raise Refusal(not_found(slicer))
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8")) if (dest / "meta.json").exists() else {}
    stale = (meta.get("slicer_version") != app_version(slicer) or meta.get("machine") != p.machine
             or meta.get("process") != p.process or meta.get("filaments") != p.filaments)
    if stale:
        problems = build(key)
        if problems:
            raise Refusal("\n".join(problems))
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    settings = {}
    for ftype in PRINTERS[key].filaments:
        with zipfile.ZipFile(dest / f"{ftype}.3mf") as z:
            settings[ftype] = json.loads(z.read("Metadata/project_settings.config"))
    return meta, settings

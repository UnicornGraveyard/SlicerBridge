"""
engines/u1.py -- a multicolor project (Bambu Studio, OrcaSlicer or a fork) -> a Snapmaker U1 project.

The conversion logic of bl2u1 (https://github.com/josuanbn/bl2u1, GPLv3), run
offline on any project, with its U1 templates (data/u1/).

The U1 has 4 extruders. What counts is the filament slots a model actually
PAINTS or ASSIGNS, never the palette size: a 15-color palette may use 4. Used
slots are compacted to extruders 1..K in slot order, and every paint_color is
re-encoded to match (Panda2Prusa's paint.transform_states) -- so the paint
follows its filament instead of silently landing on the wrong one. More than 4
used slots is a refusal: merging two colors is a design decision, made in
the designer's slicer, not here.

Size: never changed. Bambu plates are laid out for the source printer's bed (a
330 mm H2C, a 256 mm P2S), and Snapmaker Orca lays them out again for the U1's
270 mm one, so each plate is moved onto its own U1 plate -- turned 90 degrees
only if that is what makes it fit. The U1 prints a prime tower, so the model goes
to one edge and the tower into the space left: the strip beside it, or where its
outline leaves room. A model bigger than the bed is centered on its plate at full
size, with a warning and the scale that would fit: scaling it is the call of
whoever prints it, in Snapmaker Orca. Placed as every other printer's file is, by
the orca engine (place_all), with the tower at the size Snapmaker Orca prints it
(checks/u1.py tower_footprint): placed as 30 x 40 mm, SoftMaw's ran off the bed.

Prime tower: the U1 template's, except on a model taller than tall.TALL_PRINT_MM.
That one gets the heaviest of tall.py's steps its plates have room for: the whole
heavier tower; else the same on the template's ribs; else the template's, and a
warning to slice it in Snapmaker Orca and look. The same steps as every other
printer's.

Every file passes checks/u1.py's audit before it is written, and with --slice
Snapmaker Orca slices it (checks/orca.py's check 13) or it isn't written.
"""
from __future__ import annotations

import gc
import json
import math
import posixpath
import re
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .._vendor.panda2prusa.paint import transform_states

from .. import brim as model_brim
from .. import tall
from .. import templates as templates_build
from ..carry import carry_print_settings, skipped_note
from ..checks import orca as orca_check, u1 as u1check
from ..errors import Refusal
from ..output import land, partial, withdraw
from ..printers import (SLICERS, STAMP, U1_LABEL, ensure_installed, not_found, out_path as named_path, refused_settings,
                        run_slicer)
from ..source import palette
from . import orca as orca_engine

DATA = Path(__file__).resolve().parent.parent / "data" / "u1"  # bl2u1's U1 templates (GPLv3)
TARGET_FILAMENTS = 4
DEFAULT_PROFILE = "Snapmaker PLA SnapSpeed @U1"
# Source filament_type -> the U1 preset an extruder printing it gets, with that preset's
# own values (FILAMENTS): the template's are PLA's, and a TPU left on them printed at
# 220 C and 15 mm^3/s, where Snapmaker's TPU 95A HF says 230 and 9 (2026-11-07). Each
# type gets Snapmaker Orca's Generic preset for it, as Bambu printers get Bambu Studio's.
# Plain TPU gets Generic TPU, 240 C and 3.2 mm^3/s like Bambu's own Generic TPU; Bambu's
# TPU for AMS (about 68D), which the U1 has no preset for, Snapmaker's TPU 95A HF, at
# 230 C like it. PLA keeps the template's. Any other type is refused: Snapmaker Orca
# 2.4.1 has no HIPS for the U1, and a type printed as another prints at its temperature.
U1_PRESETS = {
    "PETG": "Generic PETG",
    "PETG-HF": "Snapmaker PETG HF",
    "PCTG": "Generic PCTG",
    "ABS": "Generic ABS",
    "ASA": "Generic ASA @U1 0.4 nozzle",
    "PC": "Generic PC",
    "PA": "Generic PA",
    "PLA-CF": "Generic PLA-CF",
    "PETG-CF": "Generic PETG-CF",
    "PA-CF": "Generic PA-CF",
    "PVA": "Generic PVA",
    "BVOH": "Generic BVOH",
    "TPU": "Generic TPU",
    "TPU-AMS": "Snapmaker TPU 95A HF @U1 0.4 nozzle",
}
# Those presets' values for one extruder, each through its inherits chain in Snapmaker
# Orca's profiles: written by scripts/u1_filaments.py, so a file needs no Snapmaker Orca.
FILAMENTS = DATA / "filaments.json"
U1_MACHINE = "Snapmaker U1 (0.4 nozzle)"

PAINT = re.compile(rb'( paint_color=")([0-9A-Fa-f]+)(")')
# Filament numbers in model_settings.config: part/object extruders, and settings
# like support_filament or wall_filament. 0 means "the default" and never moves.
FILAMENT_META = re.compile(r'(key="(?:extruder|[a-z_]+_filament)" value=")(\d+)(")')
LAYER_RANGE_EXTRUDER = re.compile(r'(<option opt_key="extruder">)(\d+)(</option>)')
ITEM = re.compile(r'<item\b[^>]*>')

ROOT_MODEL = "3D/3dmodel.model"


def load_u1_profiles() -> list[dict]:
    profiles = []
    # bl2u1's filament_types.3mf, reduced to the two lists read here (data/u1/filament_types.json).
    s = json.loads((DATA / "filament_types.json").read_text(encoding="utf-8"))
    for t, sid in zip(s.get("filament_type", []), s.get("filament_settings_id", [])):
        profiles.append({"type": t, "settings_id": sid})
    return profiles


def preset_values(name: str, index: dict, template: dict) -> dict:
    """{setting: value} one U1 extruder gets from filament preset `name`, its inherits chain applied.

    Every setting the template keeps one of per extruder that the preset sets, and
    filament_ids. Where a preset lists two values, the second is for the high-flow
    hotend (filament_flow_support: standard, high_flow): the U1's standard one takes
    the first, as Snapmaker Orca saved the template's SnapSpeed and the TPU 95A HF.
    """
    flat = templates_build.flatten("filament", name, index)
    if flat.get("instantiation") != "true" or U1_MACHINE not in (flat.get("compatible_printers") or []):
        raise Refusal(f"'{name}' isn't a filament preset for the {U1_MACHINE} in Snapmaker Orca")
    values = {}
    for k, v in flat.items():
        slot = template.get(k)
        if (k in templates_build.NOT_FILAMENT or k in ("filament_settings_id", "filament_colour")
                or not isinstance(slot, list) or len(slot) != TARGET_FILAMENTS):
            continue
        values[k] = (v[0] if v else "") if isinstance(v, list) else v
    values["filament_ids"] = flat.get("filament_id", "")
    return values


def filament_data(slicer=None) -> dict:
    """FILAMENTS' contents, from the installed Snapmaker Orca (scripts/u1_filaments.py)."""
    slicer = slicer or SLICERS["snapmaker"]
    index = templates_build.preset_index("Snapmaker", slicer)
    template = load_template(False)
    presets = {}
    for name in sorted(set(U1_PRESETS.values())):
        values = preset_values(name, index, template)
        presets[name] = {"filament_type": values["filament_type"], "values": values}
    return {"source": "Snapmaker Orca's U1 filament presets, each through its inherits chain (scripts/u1_filaments.py)",
            "snapmaker_orca": templates_build.app_version(slicer),
            "keys": sorted({k for p in presets.values() for k in p["values"]}),
            "presets": presets}


def u1_filaments() -> dict:
    return json.loads(FILAMENTS.read_text(encoding="utf-8"))


def is_u1_project(cfg: dict) -> bool:
    return str(cfg.get("printer_settings_id", "")).startswith("Snapmaker U1") or cfg.get("printer_model") == U1_LABEL


def slot_filament(f: dict, slot: int, src_cfg: dict, pla: str, bundled: dict) -> tuple[str, str, dict, str]:
    """(filament_type, preset, {setting: value}, note) for the extruder printing source slot `slot`.

    A slot on a preset in FILAMENTS gets its values there: an older SlicerBridge U1 file's
    TPU kept the template's PLA ones. A U1 project's own slot keeps its preset and values,
    whatever its type. Otherwise the type's U1_PRESETS preset; PLA keeps the template's
    values under `pla`; any other type is refused (U1_PRESETS).
    """
    t = f["type"]
    names = src_cfg.get("filament_settings_id")
    name = names[slot - 1] if isinstance(names, list) and slot <= len(names) else ""
    if name in bundled["presets"]:
        return bundled["presets"][name]["filament_type"], name, bundled["presets"][name]["values"], ""
    if is_u1_project(src_cfg) and name:
        values = {k: src_cfg[k][slot - 1] for k in bundled["keys"]
                  if isinstance(src_cfg.get(k), list) and slot <= len(src_cfg[k])}
        return values.get("filament_type") or t, name, values, ""
    if t in U1_PRESETS:
        name = U1_PRESETS[t]
        note = (f"Bambu's TPU for AMS: the U1 has no preset for it, so it prints on {name} "
                f"({bundled['presets'][name]['values'].get('nozzle_temperature')} C)" if t == "TPU-AMS" else "")
        return bundled["presets"][name]["filament_type"], name, bundled["presets"][name]["values"], note
    if t == "PLA":
        return "PLA", pla, {}, ""
    raise Refusal(f"filament {slot} is {t}, and Snapmaker Orca has no U1 preset for it here (u1.U1_PRESETS): "
                  f"printed as another type, it would print at that type's temperature. Pick another filament "
                  f"in the designer's slicer, or leave the U1 out.")


def mixed_note(types: list[str]) -> str:
    """What the output says when Snapmaker Orca won't slice these extruders' types together as it's
    set up: a high-temperature type with a low one, its four nozzles apart or not
    (checks/orca.py HIGH_TEMP_TYPES), or "". PETG mixes with either."""
    high = [f"{n} ({t})" for n, t in enumerate(types, start=1) if t in orca_check.HIGH_TEMP_TYPES]
    low = [f"{n} ({t})" for n, t in enumerate(types, start=1) if t in orca_check.LOW_TEMP_TYPES]
    if not high or not low:
        return ""
    return (f"extruder{'s' * (len(high) > 1)} {', '.join(high)} {'print' if len(high) > 1 else 'prints'} hot and "
            f"{', '.join(low)} cool: Snapmaker Orca won't slice them together until Preferences > Allow high/low "
            f"temperature filament mixing is on")


def template_path(has_support: bool) -> Path:
    return DATA / ("u1_template_supports.3mf" if has_support else "u1_template.3mf")


def load_template(has_support: bool) -> dict:
    with zipfile.ZipFile(template_path(has_support)) as z:
        return json.loads(z.read("Metadata/project_settings.config").decode())


def template_stamp(has_support: bool) -> tuple[str, str]:
    """(Application, X-BBL-Client-Version) of the U1 template.

    The sources carry the Bambu Studio that saved them (2.6.1-2.7.1 today), while
    the project settings come from the template (version 2.2.1). The file then
    claims a writer newer than Snapmaker Orca 2.4.0 itself. Stamping the template's
    BambuStudio-2.2.1 makes the file agree with its own settings and stay older
    than the app.

    Snapmaker Orca's CLI can't confirm this either way: its version check refuses
    every file, its own 2.4.0 projects included ("File Version 2.4.0.0 not
    supported by current cli version 01.10.01.50"), so --slice skips it
    (--allow-newer-file).
    """
    with zipfile.ZipFile(template_path(has_support)) as z:
        model = z.read(ROOT_MODEL).decode()
        info = z.read("Metadata/slice_info.config").decode()
    app = re.search(r'<metadata name="Application">([^<]*)</metadata>', model)
    client = re.search(r'key="X-BBL-Client-Version" value="([^"]*)"', info)
    if not app:
        raise Refusal(f"no Application stamp in {template_path(has_support)}")
    return app.group(1), client.group(1) if client else ""


def retransform_items(xml: str, transforms: dict) -> str:
    """Rewrite each <item>'s transform in 3D/3dmodel.model; every item must be placed."""
    done = set()

    def one(m: re.Match) -> str:
        tag = m.group(0)
        oid = re.search(r'\bobjectid="([^"]+)"', tag).group(1)
        if oid not in transforms:
            raise Refusal(f"build item for object {oid} wasn't placed")
        if not re.search(r'\btransform="', tag):
            raise Refusal(f"build item for object {oid} has no transform to rewrite")
        done.add(oid)
        return re.sub(r'\btransform="[^"]*"', f'transform="{transforms[oid]}"', tag)

    xml = ITEM.sub(one, xml)
    if done != set(transforms):
        raise Refusal(f"build items {sorted(set(transforms) - done)} not found in {ROOT_MODEL}")
    return xml


# --------------------------------------------------------------- colors ----

def plan(slug: str, src: Path, used: list[int]) -> tuple[list[dict], dict]:
    """(filaments in extruder order, {old slot: new extruder}), or exit refusing."""
    fils = palette(src)
    if not fils:
        raise Refusal(f"could not parse filaments from {src}")
    by_id = {int(f["id"]): f for f in fils}
    missing = [s for s in used if s not in by_id]
    if missing:
        raise Refusal(f"REFUSING {slug}: slots {missing} are painted or assigned but not in the palette")

    if len(used) > TARGET_FILAMENTS:
        lines = "\n".join(f"       {s:>2}  {by_id[s]['type']:8} {by_id[s]['color']}" for s in used)
        raise Refusal(
            f"REFUSING to convert {slug}: it uses {len(used)} filament slots, the U1 has "
            f"{TARGET_FILAMENTS} extruders.\n\n{lines}\n\n"
            f"   Fitting these onto {TARGET_FILAMENTS} means painting two colors with one filament.\n"
            f"   That is a design decision -- which colors merge changes the piece. Do it in\n"
            f"   your slicer: merge deliberately, re-paint, save, and run this again.\n"
        )
    mapping = {old: new for new, old in enumerate(used, start=1)}
    return [by_id[s] for s in used], mapping


def remap_model_settings(text: str, mapping: dict, used: list[int]) -> str:
    def ext(m: re.Match) -> str:
        n = int(m.group(2))
        return f"{m.group(1)}{mapping.get(n, n) if n else 0}{m.group(3)}"

    text = FILAMENT_META.sub(ext, text)
    # Per-palette-slot plate lists. filament_maps is the H2C/H2D's nozzle choice;
    # U1 projects saved by Snapmaker Orca 2.4.0 itself write all 1s, so that's what the U1 gets.
    text = re.sub(r'(key="filament_maps" value=")[^"]*(")',
                  lambda m: m.group(1) + " ".join(["1"] * TARGET_FILAMENTS) + m.group(2), text)

    def volume_maps(m: re.Match) -> str:
        vals = m.group(2).split()
        kept = [vals[s - 1] if s <= len(vals) else "0" for s in used]
        kept += ["0"] * (TARGET_FILAMENTS - len(kept))
        return m.group(1) + " ".join(kept) + m.group(3)

    return re.sub(r'(key="filament_volume_maps" value=")([^"]*)(")', volume_maps, text)


# -------------------------------------------------------------- convert ----

# Bambu Studio's "auto" for some settings is -1, where some Snapmaker Orca versions start
# the range at 0 and won't load the file ("raft_first_layer_expansion: -1 not in range
# [0,2147483647]", "tree_support_wall_count: -1 not in range [0,2]": Gatefang, 2026-10-29).
# A carried -1 where the template has a value of its own keeps the template's.
AUTO = "-1"


class SettingsRefused(Refusal):
    """Snapmaker Orca won't load these carried settings ({key: why}): write the file again without them."""

    def __init__(self, refused: dict):
        super().__init__("Snapmaker Orca won't load " + ", ".join(refused))
        self.refused = refused


def not_carried(carried: dict, u1: dict, refused: dict) -> dict:
    """{key: why} for the carried settings the U1 keeps its template's value of instead."""
    kept = {k: "Bambu Studio's auto, -1, which not every Snapmaker Orca takes" for k, v in carried.items()
            if all(x == AUTO for x in (v if isinstance(v, list) else [v])) and u1.get(k) != v}
    kept.update({k: why for k, why in refused.items() if k in carried})
    return kept


READ_BACK_LIMIT = 180  # seconds; Snapmaker Orca 2.4.1 loads and saves the cube in about 2


def read_back(f: Path) -> dict:
    """{setting: what Snapmaker Orca said} for each of `f`'s settings it won't load.

    The settings go into the U1 template's 20 mm cube, and Snapmaker Orca opens it and
    saves it again: seconds, where slicing the model takes minutes. So a carried value it
    won't take keeps the template's whether or not the file is sliced. Nothing when it
    isn't installed (it isn't installed for this), or when it hangs or fails otherwise:
    check 13 still slices what's asked.
    """
    slicer = SLICERS["snapmaker"]
    if not slicer.installed:
        return {}
    with zipfile.ZipFile(f) as z:
        settings = z.read("Metadata/project_settings.config")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        probe = Path(tmp) / "probe.3mf"
        with zipfile.ZipFile(template_path(False)) as zin, \
             zipfile.ZipFile(probe, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, settings if item.filename == "Metadata/project_settings.config" else zin.read(item))
        try:
            # Its version check refuses every project, its own included (checks/orca.py slice_check).
            run = run_slicer([slicer.binary, *slicer.cli, "--allow-newer-file", "--outputdir", tmp,
                              "--export-3mf", "back.3mf", probe], timeout=READ_BACK_LIMIT)
        except subprocess.TimeoutExpired:
            return {}
    return refused_settings(run.stdout + run.stderr) if run.returncode != 0 else {}


def slice_check(f: Path) -> orca_check.Slice:
    """Slice `f` in Snapmaker Orca: checks/orca.py's check 13.

    On a Mac a missing Snapmaker Orca is installed with Homebrew first; without one the
    file is still checked, just not sliced, and the warning says how to get it. When its
    command line crashes (again, on check 13's second try), the file is kept, with a warning
    to slice it in the app: what it refuses is still a problem. Snapmaker Orca 2.4.0's
    command line crashed (exit -11, twice) slicing Gatefang's file, which its app sliced
    fine (2026-10-29); 2.4.1's slices the same file (2026-10-30). A crash is the command
    line's, not the file's.
    """
    slicer = SLICERS["snapmaker"]
    ensure_installed(slicer)
    if not slicer.installed:
        return orca_check.Slice(slicer.name, skipped=f"not sliced: {not_found(slicer)}")
    result = orca_check.slice_check(f, "u1")
    if result.crashed:
        result.crash_note = (f"not sliced: Snapmaker Orca's command line crashed on it (exit {result.exit}), twice. "
                             f"Its app slices files its command line crashes on: open it there and slice it to check")
    return result


def with_settings(f: Path, changes: dict) -> None:
    """Rewrite the file `f` with `changes` to its project settings, and everything else as it is."""
    again = f.with_name(f.name + ".again")
    with zipfile.ZipFile(f) as zin, zipfile.ZipFile(again, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.comment = zin.comment
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps({**json.loads(data.decode()), **changes}, indent=4, ensure_ascii=False).encode()
            zout.writestr(item, data)
    again.replace(f)


def write(src: Path, slug: str, profiles: list[dict], out: Path, prefix: str = "", suffix: str = "",
          slice_it: bool = False, refused: Optional[dict] = None) -> Path:
    """Write `slug`'s U1 file into out/<slug>/ and return it; `slug` is the model's display name.
    `slice_it`: slice it in Snapmaker Orca first, and refuse it if it won't slice. `refused`:
    carried settings ({key: why}) Snapmaker Orca wouldn't load, which keep the template's."""
    refused = refused or {}
    if not src.exists():
        raise Refusal(f"no such file: {src}")

    # ---- template: with supports if the source prints them ---------------
    # By the source's own value: bl2u1 looked for enable_support among the settings
    # it marks as changed, which a preset with no system parent never marks, and
    # which turning supports off marks too.
    with zipfile.ZipFile(src) as z:
        orig = json.loads(z.read("Metadata/project_settings.config").decode())
    has_support = str(orig.get("enable_support", "0")) == "1"
    u1 = load_template(has_support)
    application, client_version = template_stamp(has_support)

    out_path = named_path(out, slug, "u1", prefix, suffix)

    # The designer's print settings, which the file's room is worked out with: the layer
    # height sizes the prime tower, the brim and skirt the room kept at the bed's edge.
    carried, skipped = carry_print_settings(orig, u1)
    kept = not_carried(carried, u1, refused)
    carried = {k: v for k, v in carried.items() if k not in kept}
    # An automatic brim Bambu Studio leaves out, Snapmaker Orca would print (brim.py).
    brim_whole, brim_objects = model_brim.left_out(src, orig)
    carried = model_brim.carried(carried, u1, brim_whole)
    likely = {**u1, **carried}

    # Every decision first: nothing is written for a model that would be refused,
    # and an older file SlicerBridge wrote for it doesn't get to stand in for this run.
    try:
        used, plates = orca_engine.analyze(slug, src)
        ordered, mapping = plan(slug, src, used)
        tallest = max(p.size[2] for p in plates)
        # A tall print gets the heaviest tower every plate, placed with that tower's base, has room for.
        tower_extra, tower_step = orca_engine.tall_tower(slug, plates, likely, "orca")
        bed = orca_engine.Bed.of({**likely, **tower_extra})
        placements, _width, _brim = orca_engine.place_all(slug, plates, bed, {**likely, **tower_extra})
    except Refusal:
        withdraw(out_path, "it no longer passes")
        raise
    transforms = {oid: tr for pl in placements for oid, tr in pl.transforms.items()}
    identity = all(old == new for old, new in mapping.items())
    # Each extruder's filament: its type, preset and that preset's values (slot_filament).
    pmap = {p["type"]: p["settings_id"] for p in profiles}
    pla = pmap.get("PLA") or (profiles[0]["settings_id"] if profiles else DEFAULT_PROFILE)
    bundled = u1_filaments()
    try:
        extruders = [slot_filament(f, s, orig, pla, bundled) for s, f in zip(used, ordered)]
    except Refusal as e:
        withdraw(out_path, "it no longer passes")
        raise Refusal(f"REFUSING {slug} for the {U1_LABEL}: {e}") from None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = partial(out_path)

    recoded: dict[bytes, bytes] = {}

    def recode(m: re.Match) -> bytes:
        code = m.group(2)
        if code not in recoded:
            recoded[code] = transform_states(code.decode(), mapping).encode()
        return m.group(1) + recoded[code] + m.group(3)

    with zipfile.ZipFile(src) as zin, \
         zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.comment = STAMP

        # ---- slice_info.config: retarget printer, renumber filaments ----
        xml = zin.read("Metadata/slice_info.config").decode()
        xml = re.sub(r'key="printer_model_id" value="[^"]*"',
                     'key="printer_model_id" value="Snapmaker U1"', xml)
        root = ET.fromstring(xml)

        # The stub has no printer_model_id header to rewrite, so stamp one in.
        header = root.find("header")
        if header is None:
            header = ET.SubElement(root, "header")
        if not any(h.get("key") == "printer_model_id" for h in header.findall("header_item")):
            ET.SubElement(header, "header_item",
                          {"key": "printer_model_id", "value": "Snapmaker U1"})
        # The template's slicer version, not the source's. A project saved by QIDI Studio or
        # Anycubic Slicer Next has no such header: stamp it in, or the file has no version.
        versions = [h for h in header.findall("header_item") if h.get("key") == "X-BBL-Client-Version"]
        if not versions:
            versions = [ET.SubElement(header, "header_item", {"key": "X-BBL-Client-Version"})]
        for h in versions:
            h.set("value", client_version)
        parent = root.find(".//plate")
        if parent is None:
            # Unsliced project: slice_info is a bare <config> stub with no <plate>.
            # Build one so the U1 gets a real filament table instead of bl2u1's
            # 4 dummy white entries.
            parent = ET.SubElement(root, "plate")
            ET.SubElement(parent, "metadata", {"key": "index", "value": "1"})

        for node in list(parent.findall("filament")):  # clear, then rebuild in extruder order
            parent.remove(node)

        n = 1
        for f, (ftype, _preset, _values, _note) in zip(ordered, extruders):
            node = ET.SubElement(parent, "filament")
            node.set("id", str(n))
            node.set("type", ftype)
            node.set("color", f["color"])
            node.set("used_m", "0")
            node.set("used_g", "0")
            n += 1
        while n <= TARGET_FILAMENTS:  # pad the extruders this model doesn't use
            d = ET.SubElement(parent, "filament")
            d.set("id", str(n)); d.set("type", "PLA")
            d.set("color", "#FFFFFFFF"); d.set("used_m", "0"); d.set("used_g", "0")
            n += 1
        slice_info = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        # ---- model_settings.config: extruders follow the mapping --------
        model_settings = model_brim.on_objects(remap_model_settings(
            zin.read("Metadata/model_settings.config").decode(), mapping, used), brim_objects).encode()

        # ---- 3D/3dmodel.model: every plate placed on its U1 plate, U1 stamp
        root_model = retransform_items(zin.read(ROOT_MODEL).decode(), transforms)
        root_model = re.sub(r'(<metadata name="Application">)[^<]*(</metadata>)',
                            lambda m: m.group(1) + application + m.group(2), root_model).encode()

        # ---- project_settings.config: U1 profile + the designer's print settings + our palette
        combined = u1.copy()
        combined.update(carried)
        if carried:  # marked as the project's own, as Snapmaker Orca marks a changed setting
            marked = list(combined.get("different_settings_to_system") or [""] * (TARGET_FILAMENTS + 2))
            marked[0] = ";".join(sorted({k for k in str(marked[0]).split(";") if k} | set(carried)))
            combined["different_settings_to_system"] = marked
        combined.update(tower_extra)
        colors, types, presets = [], [], []
        for i, (f, (ftype, preset, values, _note)) in enumerate(zip(ordered, extruders)):
            c = f["color"]
            c = (c + "FF") if len(c) == 7 else c  # RGBA
            colors.append(c.upper())
            types.append(ftype)
            presets.append(preset)
            for k, v in values.items():  # this extruder's column only, on a copy of the template's list
                if isinstance(combined.get(k), list) and len(combined[k]) == TARGET_FILAMENTS:
                    combined[k] = [*combined[k][:i], v, *combined[k][i + 1:]]
        while len(colors) < TARGET_FILAMENTS:
            colors.append("#FFFFFFFF"); types.append("PLA"); presets.append(pla)

        combined["filament_colour"] = colors
        combined["filament_type"] = types
        combined["filament_settings_id"] = presets

        for k, v in list(combined.items()):
            if k.startswith("filament_") and isinstance(v, list) and 0 < len(v) != TARGET_FILAMENTS:
                if len(v) < TARGET_FILAMENTS:
                    v.extend([v[-1]] * (TARGET_FILAMENTS - len(v)))
                else:
                    combined[k] = v[:TARGET_FILAMENTS]

        # One prime tower position per plate, in that plate's own coordinates.
        if bed.tower is not None:
            combined["wipe_tower_x"] = [f"{pl.tower[0]:.3f}" for pl in placements]
            combined["wipe_tower_y"] = [f"{pl.tower[1]:.3f}" for pl in placements]

        settings = json.dumps(combined, indent=4, ensure_ascii=False).encode()

        # ---- copy the archive through, swapping configs, re-encoding paint ----
        for item in zin.infolist():
            safe = posixpath.normpath(item.filename).lstrip("/")
            if safe.startswith(".."):
                continue  # zip-slip guard
            if item.filename == "Metadata/project_settings.config":
                zout.writestr(item, settings)
            elif item.filename == "Metadata/slice_info.config":
                zout.writestr(item, slice_info)
            elif item.filename == "Metadata/model_settings.config":
                zout.writestr(item, model_settings)
            elif item.filename == ROOT_MODEL:
                zout.writestr(item, PAINT.sub(recode, root_model) if not identity else root_model)
            elif item.filename == "Metadata/layer_config_ranges.xml" and not identity:
                xml = zin.read(item.filename).decode()
                zout.writestr(item, LAYER_RANGE_EXTRUDER.sub(
                    lambda m: f"{m.group(1)}{mapping.get(int(m.group(2)), int(m.group(2)))}{m.group(3)}",
                    xml))
            elif item.filename.lower().endswith(".model") and not identity:
                zout.writestr(item, PAINT.sub(recode, zin.read(item.filename)))
            else:
                zout.writestr(item, zin.read(item.filename))

    # Check 12 tries the tower at the spot it got; where a heavier step clears there than
    # placement's estimate allowed, the file gets that step instead, at the same spot.
    steps = [name for name, _rules in tall.STEPS["orca"]]
    here = steps.index(tower_step) if tower_step in steps else 0
    owed = u1check.owed_step_of(tmp) if here > 0 else None
    if owed is not None and owed < here:
        tower_step, tower_extra = steps[owed], tall.settings(likely, tall.STEPS["orca"][owed][1])
        with_settings(tmp, tower_extra)

    # The checks, before anything lands in the output folder.
    info, problems = u1check.audit(tmp, src)
    sliced, slice_warnings = [], []
    # A carried value Snapmaker Orca won't load is written again with the template's: read back
    # whenever it's installed, and found again in the slice's log if it gets that far. A value of
    # the template's or the tower's it won't load is a problem.
    refusals = read_back(tmp) if not problems else {}
    problems += [f"Snapmaker Orca won't load {k}: {said}" for k, said in refusals.items() if k not in carried]
    if slice_it and not problems and not refusals:
        result = slice_check(tmp)
        sliced, problems, slice_warnings = result.text()
        refusals = result.refused
    wont = {k: f"Snapmaker Orca won't take {carried[k]!r} ({said})" for k, said in refusals.items() if k in carried}
    if problems or wont:
        tmp.unlink()
        if wont:
            raise SettingsRefused(wont)
        withdraw(out_path, "it no longer passes")
        raise Refusal(f"{slug} for the {U1_LABEL} failed its checks:\n" + "\n".join(f"   {p}" for p in problems))

    land(tmp, out_path)

    mb = out_path.stat().st_size / 1e6
    slots = ",".join(map(str, used))
    news = ",".join(str(mapping[s]) for s in used)
    how = "1:1" if identity else f"{len(recoded):,} paint strings re-encoded"
    print(f"  ok  {slug:12} slots [{slots}] -> [{news}] ({how}) -> {out_path.name}  ({mb:.0f} MB)"
          + ("  sliced" if sliced else ""))
    for line in sliced:
        print(f"       {line.split(': ', 1)[-1]}")
    for i, (_t, _p, _v, note) in enumerate(extruders, start=1):
        if note:
            print(f"       extruder {i} is {note}")
    mixed = mixed_note([t for t, _p, _v, _n in extruders])
    if mixed and not any(w.startswith("Snapmaker Orca won't slice these filaments") for w in slice_warnings):
        print(f"       ⚠ {mixed}")
    if kept:
        print("       kept the U1's own " + "; ".join(f"{tall.describe({k: u1[k]})} ({why})" for k, why in kept.items()))
    if skipped:
        print(f"       not carried: {skipped_note(skipped)}")
    if model_brim.note(brim_whole, brim_objects):
        print(f"       {model_brim.note(brim_whole, brim_objects)}")
    for w in slice_warnings:
        print(f"       ⚠ {w}")
    if tower_step:
        print(f"       {tall.say(tower_step, tallest, tower_extra)}")
    for p, pl in zip(plates, placements):
        where = f"plate {p.number}: " if len(plates) > 1 else ""
        print(f"       {where}{pl.size[0]:.1f} x {pl.size[1]:.1f} x {pl.size[2]:.1f} mm"
              + (", turned 90 deg" if pl.rotated else "")
              + ("" if pl.fits else f"  ⚠ bigger than the bed: centered at full size, "
                 f"fits at {math.floor(pl.max_scale * 1000) / 10:g}% or less")
              + (f", prime tower at {pl.tower[0]:.0f},{pl.tower[1]:.0f}" if pl.tower else "")
              + ("  ⚠ tight for the prime tower -- slice it in Snapmaker Orca and look"
                 if pl.crowded and pl.fits and not sliced else ""))
    return out_path


def inspect(src: Path) -> list[str]:
    """Lines describing how a source fits the U1: used slots and each plate's fit."""
    bed = orca_engine.Bed.of(load_template(False))
    used, plates = orca_engine.analyze(src.stem, src)
    lines = []
    if len(used) > TARGET_FILAMENTS:
        lines.append(f"{U1_LABEL}: uses {len(used)} slots, {len(used) - TARGET_FILAMENTS} must be merged in the designer's slicer")
    for p in plates:
        rotated = orca_engine.orientation(p.size, bed)
        if rotated is None:
            fit = f"bigger than the bed (fits at <= {math.floor(orca_engine.fit_scale(p.size, bed) * 1000) / 10:g}%)"
        else:
            fit = "fits turned 90" if rotated else "fits"
        where = f"plate {p.number}" if len(plates) > 1 else "size"
        height = (", tall: heavier prime tower if there's room for it"
                  if bed.tower is not None and p.size[2] > tall.TALL_PRINT_MM else "")
        lines.append(f"    {where} on the {U1_LABEL}: {fit}{height}")
    return lines


def convert(src: Path, name: str, keys: list[str], out: Path, prefix: str = "", slice_it: bool = False,
            suffix: str = "") -> tuple[int, int]:
    """Write `name`'s U1 file into out/<name>/; returns (written, refused or failed).
    `slice_it`: slice it in Snapmaker Orca first."""
    refused: dict = {}
    try:
        while True:
            try:
                write(src, name, load_u1_profiles(), out, prefix, suffix, slice_it, refused)
                break
            except SettingsRefused as e:  # each round keeps more of the template's: it ends
                refused.update(e.refused)
    except Refusal as e:
        print(f"  SKIP {name} {U1_LABEL}\n!! {e}")
        withdraw(named_path(out, name, "u1", prefix, suffix), "it was refused")
        return 0, 1
    except Exception as e:  # a bug: this printer fails, the other engines still run
        print(f"  FAIL {name} {U1_LABEL}\n!! {type(e).__name__}: {e}")
        withdraw(named_path(out, name, "u1", prefix, suffix), "it failed")
        return 0, 1
    finally:
        gc.collect()
    return 1, 0

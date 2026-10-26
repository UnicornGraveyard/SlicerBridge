"""Remap tools in a PrusaSlicer .3mf without converting it.

A PrusaSlicer project is already in the target format, so it is patched instead of
re-emitted: every zip entry is copied verbatim and only the places that name an
extruder are rewritten. That keeps print settings, modifiers, layer ranges, seam and
support painting and anything else the writer for Bambu projects doesn't model.

The filaments follow the remap. Moving tool 1 to tool 3 also moves tool 1's filament
preset, colour, temperatures and purge volumes to slot 3 (and whatever was loaded in
slot 3 takes the vacated slot), so the print comes out the same from different tool
heads. Tool-head hardware settings (nozzle, retraction, offsets) stay where they are.

The model part is rewritten as a byte stream, matching the literal
``slic3rpe:mmu_segmentation`` attribute exactly as PrusaSlicer's own reader does, so
large painted meshes never have to be parsed into a tree.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import lxml.etree as ET

from .paint import transform_states, used_states, PaintError
from .reader import ROOT_MODEL
from .writer import WriteStats, extruder_conflicts

MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"
PRINT_CONFIG = "Metadata/Slic3r_PE.config"
CUSTOM_GCODE = "Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml"
LAYER_RANGES = "Metadata/Prusa_Slicer_layer_config_ranges.xml"
# PrusaSlicer 3 keeps settings and painting in JSON parts this module doesn't rewrite.
V3_PROJECT = "Metadata/PrusaSlicer3_project.json"

_CHUNK = 1 << 20
_PAINT = re.compile(rb'(slic3rpe:mmu_segmentation=")([0-9A-Fa-f]+)(")')
_OBJECT = re.compile(rb"<object[\s>]")
_ITEM = re.compile(rb"<item[\s/>]")
_APPLICATION = re.compile(rb'<metadata\s+name="Application"\s*>([^<]*)<')
_CONFIG_LINE = re.compile(r"^; (\S+) = (.*)$")
_TOOL_CHANGE = "2"  # CustomGCode::ToolChange

# Filament-preset options that aren't named filament_*. Vectors with one value per
# extruder that aren't listed here belong to the tool head and stay put.
_FILAMENT_KEYS = {
    "bed_temperature",
    "bridge_fan_speed",
    "chamber_minimal_temperature",
    "chamber_temperature",
    "cooling",
    "cooling_perimeter_transition_distance",
    "cooling_slowdown_logic",
    "custom_parameters_filament",
    "disable_fan_first_layers",
    "enable_dynamic_fan_speeds",
    "end_filament_gcode",
    "extrusion_multiplier",
    "fan_always_on",
    "fan_below_layer_time",
    "first_layer_bed_temperature",
    "first_layer_temperature",
    "full_fan_speed_layer",
    "idle_temperature",
    "max_fan_speed",
    "min_fan_speed",
    "min_print_speed",
    "overhang_fan_speed_0",
    "overhang_fan_speed_1",
    "overhang_fan_speed_2",
    "overhang_fan_speed_3",
    "slowdown_below_layer_time",
    "start_filament_gcode",
    "temperature",
    # A printer option, but it is the colour PrusaSlicer shows for the slot, so it
    # has to travel with the filament it describes.
    "extruder_colour",
}

# Per-preset lists laid out as [print, filament 1..N, printer]: key -> index of filament 1.
_CUMULATIVE_KEYS = {
    "inherits_cummulative": 1,
    "compatible_printers_condition_cummulative": 1,
    "compatible_prints_condition_cummulative": 0,
}


@dataclass
class PrusaProject:
    source_path: str
    producer: str
    extruder_count: Optional[int]  # tools in the embedded printer profile, if any
    used: list  # sorted 1-based extruders the project prints with
    objects: int = 0
    volumes: int = 0
    build_items: int = 0
    painted: int = 0
    filament_colors: list = field(default_factory=list)  # colour shown per slot
    filament_types: list = field(default_factory=list)
    plate_ids: list = field(default_factory=list)  # always empty: no plates to pick
    plate_of_object: dict = field(default_factory=dict)
    source: str = "prusa"


def is_prusa_3mf(path: str) -> bool:
    """True for a PrusaSlicer project (including this tool's own output)."""
    with zipfile.ZipFile(path, "r") as z:
        names = set(z.namelist())
        if "Metadata/model_settings.config" in names:
            return False
        if MODEL_CONFIG in names or PRINT_CONFIG in names:
            return True
        if ROOT_MODEL not in names:
            return False
        with z.open(ROOT_MODEL) as fh:
            m = _APPLICATION.search(fh.read(1 << 16))
        return bool(m) and m.group(1).strip().startswith((b"PrusaSlicer", b"panda2prusa"))


# -- Slic3r_PE.config ---------------------------------------------------------
def _config_values(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        m = _CONFIG_LINE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def _split_strings(value: str) -> list:
    """Split a ';'-separated string vector, leaving quoted elements intact."""
    parts, start, quoted, i = [], 0, False, 0
    while i < len(value):
        c = value[i]
        if quoted and c == "\\":
            i += 1
        elif c == '"':
            quoted = not quoted
        elif c == ";" and not quoted:
            parts.append(value[start:i])
            start = i + 1
        i += 1
    parts.append(value[start:])
    return parts


def _split_vector(value: str, n: int):
    """Split a per-extruder vector into (elements, separator), or None if it isn't one."""
    parts = _split_strings(value)
    if len(parts) == n:
        return parts, ";"
    parts = value.split(",")
    if len(parts) == n:
        return parts, ","
    return None


def _unquote(token: str) -> str:
    token = token.strip()
    return token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token


def _int(value) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _is_extruder_key(key: str) -> bool:
    return key == "extruder" or key.endswith("_extruder")


def _moved(items: list, perm: dict, offset: int = 0) -> list:
    """Move the element in slot ``old`` to slot ``perm[old]`` (slots are 1-based)."""
    out = list(items)
    for old, new in perm.items():
        out[offset + new - 1] = items[offset + old - 1]
    return out


def _remap_print_config(text: str, perm: dict) -> str:
    n = len(perm)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _CONFIG_LINE.match(line.rstrip("\r"))
        if not m:
            continue
        key, value = m.groups()
        new = None
        if _is_extruder_key(key):
            if value.strip().isdigit():
                new = str(perm.get(int(value), int(value)))
        elif key == "wiping_volumes_matrix":
            cells = value.split(",")
            if len(cells) == n * n:
                out = list(cells)
                for a, pa in perm.items():
                    for b, pb in perm.items():
                        out[(pa - 1) * n + pb - 1] = cells[(a - 1) * n + b - 1]
                new = ",".join(out)
        elif key in _CUMULATIVE_KEYS:
            parts, offset = _split_strings(value), _CUMULATIVE_KEYS[key]
            if len(parts) >= offset + n:
                new = ";".join(_moved(parts, perm, offset))
        elif key.startswith("filament_") or key in _FILAMENT_KEYS:
            split = _split_vector(value, n)
            if split:
                new = split[1].join(_moved(split[0], perm))
        if new is not None and new != value:
            lines[i] = f"; {key} = {new}" + ("\r" if line.endswith("\r") else "")
    return "\n".join(lines)


# -- small XML parts ----------------------------------------------------------
# These are patched as text so untouched bytes stay identical, and because some of
# them hold several root elements (one per bed), which an XML parser rejects.
_ATTR = r'\b%s="([^"]*)"'


def _attr(tag: str, name: str) -> Optional[str]:
    m = re.search(_ATTR % name, tag)
    return m.group(1) if m else None


def _remap_attr(tag: str, name: str, perm: dict) -> str:
    def swap(m):
        return f'{name}="{perm.get(_int(m.group(1)), m.group(1))}"'

    return re.sub(_ATTR % name, swap, tag, count=1)


def _remap_model_config(text: str, perm: dict) -> str:
    def meta(m):
        tag = m.group(0)
        return _remap_attr(tag, "value", perm) if _is_extruder_key(_attr(tag, "key") or "") else tag

    return re.sub(r"<metadata\b[^>]*>", meta, text)


def _remap_custom_gcode(text: str, perm: dict) -> str:
    return re.sub(r"<code\b[^>]*>", lambda m: _remap_attr(m.group(0), "extruder", perm), text)


def _remap_layer_ranges(text: str, perm: dict) -> str:
    def option(m):
        if not _is_extruder_key(_attr(m.group(1), "opt_key") or ""):
            return m.group(0)
        return f"{m.group(1)}{perm.get(_int(m.group(2)), m.group(2))}{m.group(3)}"

    return re.sub(r"(<option\b[^>]*>)\s*(\d+)\s*(</option>)", option, text)


# -- model stream -------------------------------------------------------------
def _xml_chunks(fh):
    """Yield the stream in pieces that end on a '>', so no tag is split across two."""
    carry = b""
    while chunk := fh.read(_CHUNK):
        buf = carry + chunk
        cut = buf.rfind(b">") + 1
        carry = buf[cut:]
        if cut:
            yield buf[:cut]
    if carry:
        yield carry


# -- reading ------------------------------------------------------------------
def read_prusa_3mf(path: str) -> PrusaProject:
    with zipfile.ZipFile(path, "r") as z:
        names = set(z.namelist())
        if ROOT_MODEL not in names:
            raise ValueError(
                f"{path!r} does not contain {ROOT_MODEL}; not a recognizable 3mf project."
            )
        if V3_PROJECT in names:
            raise ValueError(
                "This project was saved by PrusaSlicer 3, whose format isn't supported yet; "
                "only projects saved by PrusaSlicer 2.x can be remapped."
            )

        def text(name: str) -> str:
            return z.read(name).decode("utf-8", "replace") if name in names else ""

        config = _config_values(text(PRINT_CONFIG))
        used: set = set()

        producer = ""
        objects = build_items = painted = 0
        codes: set = set()
        with z.open(ROOT_MODEL) as fh:
            for chunk in _xml_chunks(fh):
                if not producer:
                    m = _APPLICATION.search(chunk)
                    if m:
                        producer = m.group(1).decode("utf-8", "replace").strip()
                objects += len(_OBJECT.findall(chunk))
                build_items += len(_ITEM.findall(chunk))
                for m in _PAINT.finditer(chunk):
                    painted += 1
                    codes.add(m.group(2))
        for code in codes:
            try:
                used |= used_states(code.decode("ascii"))
            except PaintError:
                pass

        # Objects and volumes on extruder 0 print with the print profile's defaults.
        volumes = configured = 0
        on_default = False
        if MODEL_CONFIG in names:
            root = ET.fromstring(z.read(MODEL_CONFIG), parser=ET.XMLParser(recover=True))
            for obj in root.findall("object") if root is not None else []:
                configured += 1
                own = {md.get("key"): md.get("value") for md in obj.findall("metadata")}
                obj_ext = _int(own.get("extruder") or 0)
                used.update(_int(v) for k, v in own.items() if _is_extruder_key(k or ""))
                vols = obj.findall("volume")
                volumes += len(vols)
                for vol in vols:
                    meta = {md.get("key"): md.get("value") for md in vol.findall("metadata")}
                    used.update(_int(v) for k, v in meta.items() if _is_extruder_key(k or ""))
                    if not (_int(meta.get("extruder") or 0) or obj_ext):
                        on_default = True
                if not vols and not obj_ext:
                    on_default = True
        if objects > configured:
            on_default = True
        if on_default:
            for key in ("perimeter_extruder", "infill_extruder", "solid_infill_extruder"):
                used.add(_int(config.get(key, "1")))
        if config.get("support_material") == "1":
            used.add(_int(config.get("support_material_extruder", "0")))
            used.add(_int(config.get("support_material_interface_extruder", "0")))
        if config.get("wipe_tower") == "1":
            used.add(_int(config.get("wipe_tower_extruder", "0")))

        for tag in re.findall(r"<code\b[^>]*>", text(CUSTOM_GCODE)):
            if _attr(tag, "type") == _TOOL_CHANGE:
                used.add(_int(_attr(tag, "extruder") or 0))
        for m in re.finditer(r"(<option\b[^>]*>)\s*(\d+)\s*</option>", text(LAYER_RANGES)):
            if _is_extruder_key(_attr(m.group(1), "opt_key") or ""):
                used.add(_int(m.group(2)))
        used.discard(0)

    count = len(config["nozzle_diameter"].split(",")) if config.get("nozzle_diameter") else None
    colors: list = []
    types: list = []
    if count:
        def slots(key: str) -> list:
            split = _split_vector(config.get(key, ""), count)
            return [_unquote(v) for v in split[0]] if split else [""] * count

        colors = [e or f for e, f in zip(slots("extruder_colour"), slots("filament_colour"))]
        types = slots("filament_type")
        if not any(types):
            types = []

    return PrusaProject(
        source_path=path,
        producer=producer,
        extruder_count=count,
        used=sorted(used),
        objects=objects,
        volumes=volumes,
        build_items=build_items,
        painted=painted,
        filament_colors=colors,
        filament_types=types,
    )


# -- remapping ----------------------------------------------------------------
def _full_permutation(project: PrusaProject, extruder_map: dict) -> dict:
    """Extend a partial {old: new} map to a permutation of every slot.

    Slots the map doesn't mention keep their number when it is still free. A slot
    whose place was taken goes to the slot that move vacated, so moving tool 1 to an
    idle tool 3 swaps the two.
    """
    wanted = {int(k): int(v) for k, v in extruder_map.items()}
    problems = extruder_conflicts(set(project.used) | set(wanted), wanted)
    if problems:
        raise ValueError(" ".join(problems))

    if any(k < 1 or v < 1 for k, v in wanted.items()):
        raise ValueError("Filament and extruder numbers must be 1 or greater.")
    count = project.extruder_count
    if count and any(v > count for k, v in wanted.items() if k != v):
        raise ValueError(
            f"This project's printer profile has {count} tool(s); "
            f"extruder {max(wanted.values())} doesn't exist."
        )
    slots = range(1, (count or max([0, *project.used, *wanted, *wanted.values()])) + 1)
    perm = {old: wanted.get(old, old) for old in set(project.used) | set(wanted) if old in slots}
    source_of = {new: old for old, new in perm.items()}
    for slot in slots:
        if slot not in perm:
            vacated = slot
            while vacated in source_of:  # follow the moves back to the slot left empty
                vacated = source_of[vacated]
            perm[slot] = vacated
    return perm


def remap_prusa_3mf(
    project: PrusaProject, out_path: str, extruder_map: Optional[dict] = None
) -> WriteStats:
    """Copy a PrusaSlicer project, moving its extruders (and their filaments) around."""
    perm = _full_permutation(project, extruder_map or {})
    changed = {old: new for old, new in perm.items() if old != new}

    recoded: dict = {}

    def recode(m):
        code = m.group(2)
        if code not in recoded:
            try:
                recoded[code] = transform_states(code.decode("ascii"), changed).encode("ascii")
            except PaintError:
                recoded[code] = code  # leave unreadable paint untouched
        return m.group(1) + recoded[code] + m.group(3)

    patches = {
        PRINT_CONFIG: _remap_print_config,
        MODEL_CONFIG: _remap_model_config,
        CUSTOM_GCODE: _remap_custom_gcode,
        LAYER_RANGES: _remap_layer_ranges,
    }
    with zipfile.ZipFile(project.source_path, "r") as zin, zipfile.ZipFile(
        out_path, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            if changed and info.filename == ROOT_MODEL and project.painted:
                target = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                target.compress_type = zipfile.ZIP_DEFLATED
                # The model's size isn't known up front, so allow it to exceed 2 GiB.
                with zin.open(info) as src, zout.open(target, "w", force_zip64=True) as dst:
                    for chunk in _xml_chunks(src):
                        dst.write(_PAINT.sub(recode, chunk))
            elif changed and info.filename in patches:
                text = zin.read(info).decode("utf-8")
                zout.writestr(info, patches[info.filename](text, perm).encode("utf-8"))
            else:
                with zin.open(info) as src, zout.open(info, "w", force_zip64=True) as dst:
                    while chunk := src.read(_CHUNK):
                        dst.write(chunk)

    return WriteStats(
        objects_written=project.objects,
        volumes_written=project.volumes,
        build_items_written=project.build_items,
        painted_triangles=project.painted,
        extruder_map={k: v for k, v in changed.items() if k in project.used} or None,
    )

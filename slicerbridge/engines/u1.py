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
to one edge and the tower into the space left. A model bigger than the bed is
centered on its plate at full size, with a warning and the scale that would
fit: scaling it is the call of whoever prints it, in Snapmaker Orca.

Prime tower: the U1 template's, except on a model taller than TALL_PRINT_MM.
That one gets TALL_TOWER, a heavier tower built for the height, if the strip
left for the tower holds its wider base; else TALL_TOWER_STOCK_RIBS, the same on
the template's ribs, if the strip holds that; else TALL_TOWER_TIGHT, and a
warning to slice it in Snapmaker Orca and look. The same steps as every other
printer's (tall.py).

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .._vendor.panda2prusa import ns
from .._vendor.panda2prusa.convert import describe, used_filaments
from .._vendor.panda2prusa.paint import transform_states
from .._vendor.panda2prusa.transform import Transform

from .. import tall
from ..carry import carry_print_settings
from ..checks import orca as orca_check, u1 as u1check
from ..errors import Refusal
from ..output import land, partial, withdraw
from ..printers import (SLICERS, STAMP, U1_LABEL, ensure_installed, not_found, out_path as named_path, refused_settings,
                        run_slicer)
from ..source import palette

DATA = Path(__file__).resolve().parent.parent / "data" / "u1"  # bl2u1's U1 templates (GPLv3)
TARGET_FILAMENTS = 4
VALID_TYPES = {"PLA", "ABS", "TPU", "PETG-HF"}
DEFAULT_PROFILE = "Snapmaker PLA SnapSpeed @U1"

PAINT = re.compile(rb'( paint_color=")([0-9A-Fa-f]+)(")')
# Filament numbers in model_settings.config: part/object extruders, and settings
# like support_filament or wall_filament. 0 means "the default" and never moves.
FILAMENT_META = re.compile(r'(key="(?:extruder|[a-z_]+_filament)" value=")(\d+)(")')
LAYER_RANGE_EXTRUDER = re.compile(r'(<option opt_key="extruder">)(\d+)(</option>)')
ITEM = re.compile(r'<item\b[^>]*>')

# Bambu Studio and its forks (Orca, Snapmaker Orca) lay plates out in a grid,
# each plate one bed plus a fifth of a bed apart (PartPlate's LOGICAL_PART_PLATE_GAP).
PLATE_STRIDE = 1.2
EPS = 1e-4                 # mm of tolerance on bed edges
TOWER_DEPTH_GUESS = 40.0   # mm; the slicer sizes the prime tower from the purges

# Tall prints get a heavier prime tower. The tower climbs as long as tools swap,
# and a face-up wall mount swaps tools nearly to its top: Gatefang is 200 mm tall
# with three swaps a layer to about 176 mm. U1 users reported its tower knocked
# off around layer 665 (2026-10-28). The file they printed, GateFang_SnamakerU1.3mf
# (0.16 mm layers, so about 107 mm up), had a far lighter tower than the template:
# prime volume 10, ribs +3 mm x 3 mm, no fillet. The template's tower (prime volume
# 45, ribs +8 mm x 8 mm, fillet) is bigger but unproven at 200 mm, so tall files
# also get the levers Snapmaker's U1 guide names for a collapsing tower: a wider
# brim, longer and wider ribs, wider purge line spacing. The slower tower makes a
# nozzle that catches it hit softer. A starting point, not yet proven on a print.
# checks/u1.py check 12 holds every tall file whose tower spot has room to at least these.
TALL_PRINT_MM = 100.0
TALL_TOWER = {
    "prime_tower_brim_width": "10",
    "wipe_tower_extra_rib_length": "20",
    "wipe_tower_rib_width": "12",
    "wipe_tower_extra_spacing": "150%",
    "wipe_tower_max_purge_speed": "60",
}
# A strip that can't hold the longer ribs may still hold the wider brim: the rest of
# the tower on the template's ribs, since the ribs are what reach furthest.
TALL_TOWER_STOCK_RIBS = {k: v for k, v in TALL_TOWER.items()
                         if k not in ("wipe_tower_extra_rib_length", "wipe_tower_rib_width")}
# A tall model whose tower strip can't hold either keeps the template's base and
# gets only what costs no room: the slower tower.
TALL_TOWER_TIGHT = {"wipe_tower_max_purge_speed": TALL_TOWER["wipe_tower_max_purge_speed"]}

ROOT_MODEL = "3D/3dmodel.model"
CORE_OBJECT = ns.q(ns.CORE, "object")
CORE_MESH = ns.q(ns.CORE, "mesh")
CORE_VERTICES = ns.q(ns.CORE, "vertices")
CORE_VERTEX = ns.q(ns.CORE, "vertex")
CORE_COMPONENTS = ns.q(ns.CORE, "components")
CORE_COMPONENT = ns.q(ns.CORE, "component")
P_PATH = ns.q(ns.PRODUCTION, "path")


def load_u1_profiles() -> list[dict]:
    profiles = []
    # bl2u1's filament_types.3mf, reduced to the two lists read here (data/u1/filament_types.json).
    s = json.loads((DATA / "filament_types.json").read_text(encoding="utf-8"))
    for t, sid in zip(s.get("filament_type", []), s.get("filament_settings_id", [])):
        profiles.append({"type": t, "settings_id": sid})
    return profiles


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


@dataclass(frozen=True)
class Bed:
    x0: float
    y0: float
    x1: float
    y1: float
    height: float
    tower: Optional[float]  # prime tower width, if the profile prints one
    tower_brim: float = 0.0
    tower_at: tuple = (0.0, 0.0)  # the profile's own prime tower spot
    rib_reach: float = 0.0  # how far a rib wall reaches past the tower on each side, at the base

    @classmethod
    def of(cls, settings: dict) -> "Bed":
        pts = [tuple(float(c) for c in p.split("x")) for p in settings["printable_area"]]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        tower = float(settings.get("prime_tower_width") or 30) if settings.get("enable_prime_tower") == "1" else None
        at = (float((settings.get("wipe_tower_x") or ["0"])[0]), float((settings.get("wipe_tower_y") or ["0"])[0]))
        return cls(min(xs), min(ys), max(xs), max(ys), float(settings["printable_height"]), tower,
                   float(settings.get("prime_tower_brim_width") or 0), at, rib_reach(settings))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def depth(self) -> float:
        return self.y1 - self.y0


def rib_reach(settings: dict) -> float:
    """How far past the tower's sides a rib wall reaches, at the base.

    The ribs run along the tower's diagonals and pass each corner by about half
    the extra rib length, plus half the rib width. They taper to nothing at the
    top, so the first layer is the widest. Only slicing knows it exactly.
    """
    if settings.get("enable_prime_tower") != "1" or settings.get("wipe_tower_wall_type") != "rib":
        return 0.0
    extra = float(settings.get("wipe_tower_extra_rib_length") or 0)
    width = float(settings.get("wipe_tower_rib_width") or 0)
    return max(0.0, (extra + width) / (2 * math.sqrt(2)))


def plate_origin(index: int, count: int, bed: Bed) -> tuple[float, float]:
    """Where plate `index` (0-based) of `count` sits, as Bambu's PartPlateList lays them out."""
    v = math.sqrt(count)
    cols = round(v) + 1 if v > round(v) else round(v)
    row, col = divmod(index, cols)
    return col * bed.width * PLATE_STRIDE, -row * bed.depth * PLATE_STRIDE


# ------------------------------------------------------------- geometry ----

@dataclass
class Plate:
    number: int                                   # Bambu's plater_id, 1-based
    items: list = field(default_factory=list)     # (object id, 3MF transform string)
    lo: list = field(default_factory=lambda: [math.inf] * 3)
    hi: list = field(default_factory=lambda: [-math.inf] * 3)

    @property
    def size(self) -> tuple:
        return tuple(self.hi[i] - self.lo[i] for i in range(3))


def _norm_path(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("3D", target))


def _leaves(objs: dict, key, xf, depth: int = 0):
    """(leaf key, cumulative transform) for every mesh under a root object."""
    o = objs.get(key)
    if o is None or depth > 16:
        return
    if o.find(CORE_MESH) is not None:
        yield key, xf
    comps = o.find(CORE_COMPONENTS)
    if comps is not None:
        for c in comps.findall(CORE_COMPONENT):
            child = (_norm_path(c.get(P_PATH)) if c.get(P_PATH) else key[0], c.get("objectid"))
            yield from _leaves(objs, child, Transform.parse(c.get("transform")).compose(xf), depth + 1)


def analyze(slug: str, src: Path) -> tuple[list[int], list[Plate]]:
    """(slots the model paints or assigns, plates with their printed parts' bounding boxes)."""
    project = describe(str(src))
    used = used_filaments(project)

    objs = {}
    for path, tree in [(ROOT_MODEL, project.root_tree), *project.object_trees.items()]:
        for o in tree.getroot().iter(CORE_OBJECT):
            objs[(path, o.get("id"))] = o

    numbers = project.plate_ids or [1]
    plates = {n: Plate(n) for n in numbers}
    seen = set()
    for bi in project.build_items:
        n = project.plate_of_object.get(bi.objectid) if project.plate_ids else 1
        if n not in plates:
            raise Refusal(f"REFUSING {slug}: build item for object {bi.objectid} is on no plate")
        if bi.objectid in seen:
            raise Refusal(f"REFUSING {slug}: object {bi.objectid} has two build items; not handled here")
        seen.add(bi.objectid)
        plate = plates[n]
        plate.items.append((bi.objectid, bi.transform))

        # Only printed parts sit on the bed: not modifiers, negative parts or blockers.
        so = project.settings_objects.get(bi.objectid)
        parts = so.parts if so else []
        by_id = {p.id: p.subtype for p in parts}
        leaves = list(_leaves(objs, (ROOT_MODEL, bi.objectid), Transform.parse(bi.transform)))
        for i, (key, xf) in enumerate(leaves):
            subtype = by_id.get(key[1]) or (parts[i].subtype if len(parts) == len(leaves) else "normal_part")
            if subtype != "normal_part":
                continue
            m = xf.m
            lo, hi = plate.lo, plate.hi
            verts = objs[key].find(CORE_MESH).find(CORE_VERTICES)
            for v in (verts.iterchildren(CORE_VERTEX) if verts is not None else ()):
                x, y, z = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                p = (x * m[0] + y * m[3] + z * m[6] + m[9],
                     x * m[1] + y * m[4] + z * m[7] + m[10],
                     x * m[2] + y * m[5] + z * m[8] + m[11])
                for k in range(3):
                    if p[k] < lo[k]:
                        lo[k] = p[k]
                    if p[k] > hi[k]:
                        hi[k] = p[k]
    del project, objs
    gc.collect()
    return used, [plates[n] for n in numbers]


ROT90 = Transform((0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0))


@dataclass
class Placement:
    rotated: bool
    size: tuple          # footprint and height after the turn
    transforms: dict     # object id -> new 3MF transform string
    tower: Optional[tuple]  # prime tower position, plate-local
    tight: bool = False  # the strip left for the tower is smaller than its likely footprint
    fits: bool = True
    max_scale: float = 1.0  # largest scale that would fit, when it doesn't -- for the warning only


def fit_scale(size: tuple, bed: Bed) -> tuple[float, bool]:
    """Largest uniform scale that fits, and whether it needs the 90 degree turn."""
    plain = min(bed.width / size[0], bed.depth / size[1], bed.height / size[2])
    turned = min(bed.width / size[1], bed.depth / size[0], bed.height / size[2])
    return (turned, True) if turned > plain else (plain, False)


def orientation(size: tuple, bed: Bed) -> Optional[bool]:
    """False if it fits as Bambu had it, True if only turned 90 degrees, None if neither."""
    sx, sy, sz = size
    for rotated, w, d in ((False, sx, sy), (True, sy, sx)):
        if w <= bed.width + EPS and d <= bed.depth + EPS and sz <= bed.height + EPS:
            return rotated
    return None


def place(slug: str, plate: Plate, count: int, bed: Bed) -> Placement:
    """Move the plate onto its own U1 plate: a turn about Z and a move, never a scale."""
    if not all(math.isfinite(d) and d > 0 for d in plate.size):
        raise Refusal(f"REFUSING {slug}: plate {plate.number} has no printed part to place")
    rotated = orientation(plate.size, bed)
    fits = rotated is not None
    rotated = bool(rotated)
    sx, sy, sz = plate.size
    w, d = (sy, sx) if rotated else (sx, sy)

    # Where the model goes on its plate. With a prime tower, the model moves to one
    # edge and the tower goes in the middle of the strip that's left.
    spare_x, spare_y = bed.width - w, bed.depth - d
    mid = ((bed.x0 + bed.x1) / 2, (bed.y0 + bed.y1) / 2)
    tower, tight = None, False
    if not fits:
        # Bigger than the bed: centered at full size, as Bambu had it. There's no
        # strip to give the tower, so it keeps the profile's own spot.
        center = mid
        tower = bed.tower_at if bed.tower is not None else None
    elif bed.tower is None:
        center = mid
    else:
        side = bed.depth if spare_x >= bed.tower + 10 else 0.0
        if side > spare_y:
            m = min(5.0, spare_x / 4)
            center = (bed.x0 + m + w / 2, mid[1])
            strip = (bed.x0 + m + w, bed.y0, bed.x1, bed.y1)
        else:
            m = min(5.0, spare_y / 4)
            center = (mid[0], bed.y0 + m + d / 2)
            strip = (bed.x0, bed.y0 + m + d, bed.x1, bed.y1)
        tx = min(max((strip[0] + strip[2] - bed.tower) / 2, bed.x0), bed.x1 - bed.tower)
        ty = min(max((strip[1] + strip[3] - TOWER_DEPTH_GUESS) / 2, bed.y0), bed.y1 - TOWER_DEPTH_GUESS)
        tower = (tx, ty)
        pad = bed.tower_brim + bed.rib_reach
        tight = (strip[2] - strip[0] < bed.tower + 2 * pad
                 or strip[3] - strip[1] < TOWER_DEPTH_GUESS + 2 * pad)

    ox, oy = plate_origin(plate.number - 1, count, bed)
    pre = ROT90 if rotated else Transform.identity()
    corners = [pre.apply_point(x, y, z) for x in (plate.lo[0], plate.hi[0])
               for y in (plate.lo[1], plate.hi[1]) for z in (plate.lo[2], plate.hi[2])]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    move = pre.compose(Transform((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0,
                                  ox + center[0] - (lo[0] + hi[0]) / 2,
                                  oy + center[1] - (lo[1] + hi[1]) / 2,
                                  plate.lo[2] - lo[2])))  # the bottom stays on the bed
    transforms = {oid: Transform.parse(tr).compose(move).to_string() for oid, tr in plate.items}
    return Placement(rotated, (w, d, sz), transforms, tower, tight, fits,
                     1.0 if fits else fit_scale(plate.size, bed)[0])


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
    bed = Bed.of(u1)

    out_path = named_path(out, slug, "u1", prefix, suffix)

    # Every decision first: nothing is written for a model that would be refused,
    # and an older file SlicerBridge wrote for it doesn't get to stand in for this run.
    try:
        used, plates = analyze(slug, src)
        ordered, mapping = plan(slug, src, used)
        tallest = max(p.size[2] for p in plates)
        tower_extra: dict = {}
        if bed.tower is not None and tallest > TALL_PRINT_MM:
            tower_extra = TALL_TOWER_TIGHT
            for step in (TALL_TOWER, TALL_TOWER_STOCK_RIBS):
                heavy = Bed.of({**u1, **step})
                if not any(pl.tight or not pl.fits for pl in (place(slug, p, len(plates), heavy) for p in plates)):
                    tower_extra, bed = step, heavy  # placed, and checked for room, with that tower's base
                    break
        placements = [place(slug, p, len(plates), bed) for p in plates]
    except Refusal:
        withdraw(out_path, "it no longer passes")
        raise
    transforms = {oid: tr for pl in placements for oid, tr in pl.transforms.items()}
    identity = all(old == new for old, new in mapping.items())
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
        for f in ordered:
            node = ET.SubElement(parent, "filament")
            node.set("id", str(n))
            node.set("type", f["type"] if f["type"] in VALID_TYPES else "PLA")
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
        model_settings = remap_model_settings(
            zin.read("Metadata/model_settings.config").decode(), mapping, used).encode()

        # ---- 3D/3dmodel.model: every plate placed on its U1 plate, U1 stamp
        root_model = retransform_items(zin.read(ROOT_MODEL).decode(), transforms)
        root_model = re.sub(r'(<metadata name="Application">)[^<]*(</metadata>)',
                            lambda m: m.group(1) + application + m.group(2), root_model).encode()

        # ---- project_settings.config: U1 profile + the designer's print settings + our palette
        combined = u1.copy()
        carried, _ = carry_print_settings(orig, u1)
        kept = not_carried(carried, u1, refused)
        carried = {k: v for k, v in carried.items() if k not in kept}
        combined.update(carried)
        if carried:  # marked as the project's own, as Snapmaker Orca marks a changed setting
            marked = list(combined.get("different_settings_to_system") or [""] * (TARGET_FILAMENTS + 2))
            marked[0] = ";".join(sorted({k for k in str(marked[0]).split(";") if k} | set(carried)))
            combined["different_settings_to_system"] = marked
        combined.update(tower_extra)
        colors, types = [], []
        for f in ordered:
            c = f["color"]
            c = (c + "FF") if len(c) == 7 else c  # RGBA
            colors.append(c.upper())
            types.append(f["type"] if f["type"] in VALID_TYPES else "PLA")
        while len(colors) < TARGET_FILAMENTS:
            colors.append("#FFFFFFFF"); types.append("PLA")

        combined["filament_colour"] = colors
        combined["filament_type"] = types
        pmap = {p["type"]: p["settings_id"] for p in profiles}
        fallback = profiles[0]["settings_id"] if profiles else DEFAULT_PROFILE
        combined["filament_settings_id"] = [pmap.get(t, fallback) for t in types]

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
    if kept:
        print("       kept the U1's own " + "; ".join(f"{tall.describe({k: u1[k]})} ({why})" for k, why in kept.items()))
    for w in slice_warnings:
        print(f"       ⚠ {w}")
    if tower_extra:
        step = {id(TALL_TOWER): 0, id(TALL_TOWER_STOCK_RIBS): 1}.get(id(tower_extra), -1)
        print(f"       {tall.say(tall.STEPS['orca'][step][0], tallest, tower_extra)}")
    for p, pl in zip(plates, placements):
        where = f"plate {p.number}: " if len(plates) > 1 else ""
        print(f"       {where}{pl.size[0]:.1f} x {pl.size[1]:.1f} x {pl.size[2]:.1f} mm"
              + (", turned 90 deg" if pl.rotated else "")
              + ("" if pl.fits else f"  ⚠ bigger than the bed: centered at full size, "
                 f"fits at {math.floor(pl.max_scale * 1000) / 10:g}% or less")
              + (f", prime tower at {pl.tower[0]:.0f},{pl.tower[1]:.0f}" if pl.tower else "")
              + ("  ⚠ tight for the prime tower -- slice it in Snapmaker Orca and look"
                 if pl.tight and pl.fits and not sliced else ""))
    return out_path


def inspect(src: Path) -> list[str]:
    """Lines describing how a source fits the U1: used slots and each plate's fit."""
    bed = Bed.of(load_template(False))
    used, plates = analyze(src.stem, src)
    lines = []
    if len(used) > TARGET_FILAMENTS:
        lines.append(f"{U1_LABEL}: uses {len(used)} slots, {len(used) - TARGET_FILAMENTS} must be merged in the designer's slicer")
    for p in plates:
        rotated = orientation(p.size, bed)
        if rotated is None:
            fit = f"bigger than the bed (fits at <= {math.floor(fit_scale(p.size, bed)[0] * 1000) / 10:g}%)"
        else:
            fit = "fits turned 90" if rotated else "fits"
        where = f"plate {p.number}" if len(plates) > 1 else "size"
        tall = ", tall: heavier prime tower if its strip has room" if bed.tower is not None and p.size[2] > TALL_PRINT_MM else ""
        lines.append(f"    {where} on the {U1_LABEL}: {fit}{tall}")
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

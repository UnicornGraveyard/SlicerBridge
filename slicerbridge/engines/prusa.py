"""
engines/prusa.py -- a multicolor project (Bambu Studio, OrcaSlicer or a fork) -> PrusaSlicer projects for Prusa's
multi-material printers.

Wraps Panda2Prusa (https://github.com/liutang/Panda2Prusa, MIT), pinned to one
commit, and checks every output before it is kept.

Colors: only the filament slots a model actually PAINTS or ASSIGNS count, never
the palette size. Used slots that fit the printer's tools are compacted to
1..K in slot order -- no color decisions. More used slots than tools is a
refusal: merging two colors is a design decision, not a conversion.

Size: never changed, and never a reason to skip a model. Every model is
converted at the size it was designed, and check 11 proves it. Many are bigger
than the MMU3 and INDX beds: those are centered as they are, with a warning and
the scale that would fit -- scaling it is the call of whoever prints it, in
PrusaSlicer. A plate that fits is turned 90 degrees only if that is what makes
it fit, and on printers with a wipe tower (MMU3, XL) the model goes to one edge
and the tower into the space left; slicing proves it clears.

PrusaSlicer has no plates, so each plate of the Bambu project becomes its own
file. PrusaSlicer builds the printer profiles once (into SlicerBridge's cache)
and load-checks every output.
"""
from __future__ import annotations

import gc
import math
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import lxml.etree as ET

from .._vendor.panda2prusa import ns
from .._vendor.panda2prusa.convert import describe, used_filaments
from .._vendor.panda2prusa.settings import Warnings, translate
from .._vendor.panda2prusa.transform import Transform
from .._vendor.panda2prusa.writer import write_prusa_3mf

from ..errors import Refusal
from ..printers import LEGACY_STAMPS, PRUSA_PRINTERS as PRINTERS, SLICERS, STAMP, PrusaPrinter as Printer, cache_dir, out_name, run_slicer

PROFILES = cache_dir() / "prusa-profiles"
# PrusaSlicer runs against this private settings folder, never your own, so
# installing printer models for the profiles can't touch your PrusaSlicer setup.
DATADIR = cache_dir() / "prusaslicer"
# Where an installed PrusaSlicer keeps its downloaded vendor bundles.
USER_DATADIR = {"darwin": Path("~/Library/Application Support/PrusaSlicer"),
                "win32": Path(os.environ.get("APPDATA", "~")) / "PrusaSlicer"}.get(
    sys.platform, Path("~/.config/PrusaSlicer")).expanduser()

FILAMENT_TYPES = {"PLA"}  # Bambu filament_type values the filament preset stands for

# Print settings carried over from the Bambu project onto the Prusa print profile,
# on top of whatever the project marks as changed from its system preset. Everything
# goes through Panda2Prusa's own translator; what it can't translate is reported.
PRINT_KEYS = (
    "layer_height", "wall_loops", "top_shell_layers", "bottom_shell_layers",
    "sparse_infill_density", "sparse_infill_pattern", "seam_position",
    "enable_support", "support_type", "support_style", "support_threshold_angle",
    "support_on_build_plate_only",
)

# Bambu part subtypes -> PrusaSlicer volume types (the 3MF names on both sides).
VOLUME_TYPES = {
    "normal_part": "ModelPart",
    "modifier_part": "ParameterModifier",
    "negative_part": "NegativeVolume",
    "support_enforcer": "SupportEnforcer",
    "support_blocker": "SupportBlocker",
}
EPS = 1e-4  # mm of tolerance on bed edges -- no looser than PrusaSlicer's own bed check

ROOT_MODEL = "3D/3dmodel.model"
CORE_OBJECT = ns.q(ns.CORE, "object")
CORE_MESH = ns.q(ns.CORE, "mesh")
CORE_VERTICES = ns.q(ns.CORE, "vertices")
CORE_VERTEX = ns.q(ns.CORE, "vertex")
CORE_TRIANGLES = ns.q(ns.CORE, "triangles")
CORE_TRIANGLE = ns.q(ns.CORE, "triangle")
CORE_COMPONENTS = ns.q(ns.CORE, "components")
CORE_COMPONENT = ns.q(ns.CORE, "component")
CORE_ITEM = ns.q(ns.CORE, "item")
P_PATH = ns.q(ns.PRODUCTION, "path")
MMU = ns.q(ns.SLIC3RPE, "mmu_segmentation")


# ---------------------------------------------------------------- paint ----

def paint_leaves(code: str) -> Counter:
    """Count the leaf states of one TriangleSelector paint string.

    Written from the format rather than borrowed from Panda2Prusa, so the
    verification doesn't ask the engine to check its own work. The bits are
    read from the END of the hex string, LSB first in each nibble -- which is
    just int(code, 16) read from bit 0 up:

        node := split:2 ( state:2 [ext:4 if state == 3 -> state = ext + 3]
                        | side:2 node * (split + 1) )
    """
    v = int(code, 16)
    nbits = 4 * len(code)
    pos, todo, out = 0, 1, Counter()
    while todo:
        todo -= 1
        if pos + 2 > nbits:
            raise ValueError(f"truncated paint string {code[:40]}")
        split = (v >> pos) & 3
        pos += 2
        if split == 0:
            state = (v >> pos) & 3
            pos += 2
            if state == 3:
                state = ((v >> pos) & 15) + 3
                pos += 4
            out[state] += 1
        else:
            pos += 2  # special side
            todo += split + 1
    if nbits - pos >= 4 and v >> pos:
        raise ValueError(f"data after the root node in paint string {code[:40]}")
    return out


def histogram(codes: Counter) -> Counter:
    """Leaves per paint state over every painted triangle (codes: string -> triangles)."""
    hist: Counter = Counter()
    for code, n in codes.items():
        if code:
            for state, k in paint_leaves(code).items():
                hist[state] += k * n
    return hist


def remap_histogram(hist: Counter, mapping: dict) -> Counter:
    """State 0 is 'the part's own filament' and never moves."""
    out: Counter = Counter()
    for state, n in hist.items():
        out[mapping.get(state, state) if state else 0] += n
    return out


# --------------------------------------------------------------- source ----

def norm_color(c: str) -> str:
    c = c.strip().upper()
    return c[:7] if len(c) == 9 else c


@dataclass
class Plate:
    number: Optional[int]
    items: list = field(default_factory=list)      # panda2prusa BuildItems on this plate
    objects: list = field(default_factory=list)    # root object ids, first-seen order
    triangles: int = 0
    painted: int = 0
    codes: Counter = field(default_factory=Counter)
    special: Counter = field(default_factory=Counter)  # non-ModelPart volume types
    model_parts: int = 0
    lo: list = field(default_factory=lambda: [math.inf] * 3)
    hi: list = field(default_factory=lambda: [-math.inf] * 3)

    @property
    def size(self) -> tuple:
        return tuple(self.hi[i] - self.lo[i] for i in range(3))


@dataclass
class Source:
    slug: str
    display: str
    path: Path
    project: object
    palette: list          # normalized "#RRGGBB" per Bambu slot
    types: list
    used: list             # 1-based slots the model paints or assigns
    plates: list           # [Plate]
    print_settings: dict   # Bambu keys to translate onto the Prusa print profile


def _norm_path(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("3D", target))


def _objects(project) -> dict:
    out = {}
    for path, tree in [(ROOT_MODEL, project.root_tree), *project.object_trees.items()]:
        for o in tree.getroot().iter(CORE_OBJECT):
            out[(path, o.get("id"))] = o
    return out


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


def _leaf_subtypes(so, keys: list) -> list:
    """Bambu part subtype per mesh leaf: by part id, else by order (as Bambu numbers them)."""
    parts = so.parts if so else []
    by_id = {p.id: p.subtype for p in parts}
    out = []
    for i, (_path, oid) in enumerate(keys):
        if oid in by_id:
            out.append(by_id[oid])
        elif len(parts) == len(keys):
            out.append(parts[i].subtype)
        else:
            out.append("normal_part")
    return out


def load_source(slug: str, display: str, path: Path) -> Source:
    if not path.exists():
        raise Refusal(f"no such file: {path}")
    project = describe(str(path))
    if project.source != "bambu":
        raise Refusal(f"{path.name} is already a PrusaSlicer project, not a Bambu one")

    ps = project.project_settings
    palette = [norm_color(c) for c in project.filament_colors]
    types = list(project.filament_types)
    used = used_filaments(project)

    # Plates. Bambu files place every plate side by side in one coordinate space,
    # so plate 2 sits ~350 mm to the right of plate 1 -- off any Prusa bed.
    numbers = project.plate_ids or [None]
    plates = {n: Plate(n) for n in numbers}
    for bi in project.build_items:
        n = project.plate_of_object.get(bi.objectid) if project.plate_ids else None
        if n not in plates:
            raise Refusal(f"{slug}: build item for object {bi.objectid} is on no plate")
        plates[n].items.append(bi)

    objs = _objects(project)
    for plate in plates.values():
        for bi in plate.items:
            first = bi.objectid not in plate.objects
            if first:
                plate.objects.append(bi.objectid)
            so = project.settings_objects.get(bi.objectid)
            if first and so:
                for p in so.parts:
                    vt = VOLUME_TYPES.get(p.subtype, "ParameterModifier")
                    if vt == "ModelPart":
                        plate.model_parts += 1
                    else:
                        plate.special[vt] += 1
            leaves = list(_leaves(objs, (ROOT_MODEL, bi.objectid), Transform.parse(bi.transform)))
            subtypes = _leaf_subtypes(so, [k for k, _ in leaves])
            for (key, xf), subtype in zip(leaves, subtypes):
                mesh = objs[key].find(CORE_MESH)
                if first:  # one mesh per object, however many instances it has
                    tris = mesh.find(CORE_TRIANGLES)
                    for t in (tris.iterchildren(CORE_TRIANGLE) if tris is not None else ()):
                        plate.triangles += 1
                        code = t.get("paint_color")
                        if code is not None:
                            plate.painted += 1
                            plate.codes[code] += 1
                if subtype != "normal_part":
                    continue  # only printed parts sit on the bed
                m = xf.m
                lo, hi = plate.lo, plate.hi
                verts = mesh.find(CORE_VERTICES)
                for v in (verts.iterchildren(CORE_VERTEX) if verts is not None else ()):
                    x, y, z = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                    p = (x * m[0] + y * m[3] + z * m[6] + m[9],
                         x * m[1] + y * m[4] + z * m[7] + m[10],
                         x * m[2] + y * m[5] + z * m[8] + m[11])
                    for i in range(3):
                        if p[i] < lo[i]:
                            lo[i] = p[i]
                        if p[i] > hi[i]:
                            hi[i] = p[i]

    diff = ps.get("different_settings_to_system") or [""]
    keys = list(PRINT_KEYS) + [k for k in str(diff[0]).split(";") if k and k not in PRINT_KEYS]
    print_settings = {}
    for k in keys:
        v = ps.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None:
            print_settings[k] = str(v)

    return Source(slug, display, path, project, palette, types, used,
                  [plates[n] for n in numbers], print_settings)


def slot_label(src: Source, slot: int) -> str:
    t = src.types[slot - 1] if slot <= len(src.types) else "?"
    c = src.palette[slot - 1] if slot <= len(src.palette) else "?"
    return f"{slot:>2} {t:5} {c}"


# -------------------------------------------------------------- mapping ----

def parse_map(text: str) -> dict:
    mapping = {}
    for pair in text.replace("->", "=").replace(":", "=").split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            old, new = pair.split("=")
            mapping[int(old)] = int(new)
        except ValueError:
            raise Refusal(f"bad --map entry {pair!r}; expected e.g. --map 10=2,11=3")
    return mapping


def decide_mapping(src: Source, printer: Printer, explicit: Optional[dict]) -> dict:
    used = src.used
    if not used:
        raise Refusal(f"{src.slug}: no filament is used -- nothing printable found")

    if explicit is None and len(used) > printer.tools:
        lines = "\n".join(f"       {slot_label(src, s)}" for s in used)
        raise Refusal(
            f"REFUSING {src.slug}: it uses {len(used)} filament slots, the {printer.label} has "
            f"{printer.tools} tools.\n\n"
            f"{lines}\n\n"
            f"   Fitting these onto {printer.tools} tools means painting two colors with one filament.\n"
            f"   That is a design decision -- which colors merge changes the piece -- so it\n"
            f"   is never made here. Decide it, then pass --map old=new for every slot above.\n"
            f"   Note: the pinned Panda2Prusa refuses to map two filaments to one extruder,\n"
            f"   so today the merge has to be done in the designer's slicer (re-paint, save again)."
        )

    if explicit is None:
        return {old: i for i, old in enumerate(used, start=1)}

    missing = [s for s in used if s not in explicit]
    extra = [s for s in explicit if s not in used]
    over = {s: t for s, t in explicit.items() if not 1 <= t <= printer.tools}
    if missing or extra or over:
        bits = []
        if missing:
            bits.append(f"used slots not mapped: {missing}")
        if extra:
            bits.append(f"slots in --map the model doesn't use: {extra}")
        if over:
            bits.append(f"targets outside tools 1..{printer.tools}: {over}")
        raise Refusal(f"REFUSING {src.slug} --map: " + "; ".join(bits))
    return dict(explicit)


def check_filament_types(src: Source, mapping: dict) -> None:
    odd = [s for s in mapping if (src.types[s - 1] if s <= len(src.types) else "?") not in FILAMENT_TYPES]
    if odd:
        raise Refusal(
            f"REFUSING {src.slug}: slots {odd} are "
            f"{sorted({src.types[s - 1] for s in odd})}, and only {sorted(FILAMENT_TYPES)} has a\n"
            f"   Prusa filament preset mapped here (PRINTERS / FILAMENT_TYPES). Add one first."
        )


# ------------------------------------------------------------- profiles ----

@dataclass
class Profile:
    header: str
    items: list          # [(key, value)] in PrusaSlicer's order
    values: dict

    @property
    def tools(self) -> int:
        return len(self.values["nozzle_diameter"].split(","))

    @property
    def bed(self) -> tuple:
        pts = [tuple(float(c) for c in p.split("x")) for p in self.values["bed_shape"].split(",")]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys), float(self.values["max_print_height"])

    @property
    def tower_width(self) -> Optional[float]:
        """Wipe tower width if this printer prints one (MMU3, XL; not the INDX)."""
        if self.values.get("wipe_tower") != "1":
            return None
        return float(self.values.get("wipe_tower_width") or 60)


def find_prusaslicer() -> Optional[str]:
    """The PrusaSlicer binary, if installed (SLICERBRIDGE_PRUSASLICER points elsewhere)."""
    s = SLICERS["prusa"]
    return str(s.binary) if s.installed else None


def ensure_datadir() -> Path:
    """Private PrusaSlicer settings folder with the Prusa bundle and our printers installed."""
    vendor = DATADIR / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    if not (vendor / "PrusaResearch.ini").exists():
        sources = [USER_DATADIR / "vendor"]
        if find_prusaslicer():
            sources.append(SLICERS["prusa"].profiles)
        for d in sources:
            if (d / "PrusaResearch.ini").exists():
                for name in ("PrusaResearch.ini", "PrusaResearch.idx"):
                    if (d / name).exists():
                        shutil.copy2(d / name, vendor / name)
                break
        else:
            raise Refusal("no PrusaResearch.ini vendor bundle found (install PrusaSlicer)")
    models = "\n".join(f"model:{p.model} = {p.variant}" for p in PRINTERS.values())
    (DATADIR / "PrusaSlicer.ini").write_text(f"[vendor:PrusaResearch]\n{models}\n", encoding="utf-8")
    return DATADIR


def refresh_profile(key: str) -> Path:
    ps = find_prusaslicer()
    if not ps:
        raise Refusal(f"the {key} profile isn't built yet and PrusaSlicer isn't installed to build it")
    p = PRINTERS[key]
    PROFILES.mkdir(parents=True, exist_ok=True)
    dest = PROFILES / f"{key}.ini"
    r = run_slicer(
        [ps, "--datadir", ensure_datadir(), "--printer-profile", p.preset,
         "--print-profile", p.print_preset, "--material-profile", p.filament_preset,
         "--save", dest],
        timeout=300,
    )
    if r.returncode or not dest.exists():
        raise Refusal(f"PrusaSlicer could not build the {key} profile:\n{r.stdout}{r.stderr}")
    bundle = DATADIR / "vendor" / "PrusaResearch.ini"
    ver = re.search(r"^config_version = (\S+)", bundle.read_text(encoding="utf-8", errors="replace"), re.M)
    print(f"  profile {key:6} <- {p.preset} / {p.print_preset} / {p.filament_preset}"
          f"  (Prusa bundle {ver.group(1) if ver else '?'})")
    return dest


def load_profile(key: str) -> Profile:
    path = PROFILES / f"{key}.ini"
    if not path.exists():
        refresh_profile(key)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].lstrip("#").strip()
    items = []
    for line in lines[1:]:
        if not line.strip():
            continue
        k, sep, v = line.partition(" = ")
        if not sep:
            raise Refusal(f"unreadable line in {path}: {line[:80]}")
        items.append((k, v))
    prof = Profile(header, items, dict(items))
    p = PRINTERS[key]
    if prof.values.get("printer_settings_id") != p.preset or prof.tools != p.tools:
        raise Refusal(f"{path} is not the {p.preset} ({p.tools} tools) profile -- run slicerbridge templates --refresh")
    return prof


def build_config(prof: Profile, src: Source, mapping: dict, printer: Printer) -> tuple[str, list]:
    """Slic3r_PE.config text: the Prusa profile, the model's colors, and its print settings."""
    values = dict(prof.values)
    notes = []

    for k in ("filament_colour", "extruder_colour"):
        cols = values[k].split(";")
        for old, tool in mapping.items():
            cols[tool - 1] = src.palette[old - 1]
        values[k] = ";".join(cols)

    warnings = Warnings()
    nozzle = src.project.nozzle_diameter
    translated = translate(src.print_settings, "project", warnings, True, nozzle,
                           lambda f: mapping.get(f, f))
    first = src.project.project_settings.get("initial_layer_print_height")
    if first:
        translated["first_layer_height"] = str(first)
    for k, v in translated.items():
        if k not in values:
            notes.append(f"{k} = {v} is not a {printer.preset} setting, left out")
            continue
        values[k] = v
    notes += [f"not carried over: {w}" for w in warnings.lines()]

    body = "".join(f"; {k} = {values[k]}\n" for k, _ in prof.items if k != "compatible_printers")
    return f"; {prof.header}\n\n{body}", notes


# ------------------------------------------------------------ placement ----

ROT90 = Transform((0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0))


@dataclass
class Placement:
    rotated: bool
    spare: tuple   # mm left over in x, y (negative: it overhangs)
    size: tuple
    fits: bool = True
    max_scale: float = 1.0  # largest scale that would fit, when it doesn't -- for the warning only


def fit_scale(size: tuple, bed: tuple) -> tuple[float, bool]:
    """Largest uniform scale that fits, and whether it needs the 90 degree turn."""
    bw, bd, bh = bed[2] - bed[0], bed[3] - bed[1], bed[4]
    plain = min(bw / size[0], bd / size[1], bh / size[2])
    turned = min(bw / size[1], bd / size[0], bh / size[2])
    return (turned, True) if turned > plain else (plain, False)


def plan_placement(src: Source, plate: Plate, bed: tuple) -> Placement:
    if not all(math.isfinite(d) and d > 0 for d in plate.size):
        raise Refusal(f"{src.slug}: plate {plate.number} has no printed part to place")
    sx, sy, sz = plate.size
    bw, bd, bh = bed[2] - bed[0], bed[3] - bed[1], bed[4]
    for rotated, w, d in ((False, sx, sy), (True, sy, sx)):
        if w <= bw + EPS and d <= bd + EPS and sz <= bh + EPS:
            return Placement(rotated, (bw - w, bd - d), (w, d, sz))
    # Bigger than the bed: converted anyway, at full size, as Bambu had it.
    # Scaling it down is the call of whoever prints it, in PrusaSlicer.
    best, _turn = fit_scale(plate.size, bed)
    return Placement(False, (bw - sx, bd - sy), (sx, sy, sz), fits=False, max_scale=best)


TOWER_DEPTH_GUESS = 40.0  # mm; PrusaSlicer sizes the tower from the purges, so only slicing knows


TOWER_BRIM = 2.0  # PrusaSlicer's wipe_tower_brim_width default


_OUTLINES: dict = {}


def outlines(path: Path) -> dict:
    """{plate number: the source's outline cells from above}, read once per source."""
    if path not in _OUTLINES:
        from . import orca as orca_engine
        _used, plates = orca_engine.analyze(path.stem, path)
        _OUTLINES.clear()  # one source at a time: they're big
        _OUTLINES[path] = {p.number: p.cells for p in plates}
    return _OUTLINES[path]


def layout(pl: Placement, bed: tuple, tower_width: Optional[float], plate: Optional[Plate] = None,
           outline: Optional[set] = None) -> tuple:
    """Where the model's center goes, and the wipe tower's front-left corner (or None).

    Without a wipe tower the model is centered. With one, PrusaSlicer would put the
    tower at its default spot -- inside a big model, where PrusaSlicer 2.9.6 logs a
    path conflict and crashes writing the G-code. So the model moves to one edge and
    the tower goes in the middle of the strip that's left -- or, when that strip is
    too small for it, where the model's `outline` (the plate's cells from above)
    leaves room. Slicing (check 10) is what proves it clears: the tower's depth and
    its stabilizing cone grow with the print.
    """
    center, tower = _strip_layout(pl, bed, tower_width)
    if tower is None or plate is None or not outline:
        return center, tower
    bx0, by0, bx1, by1 = bed[:4]
    w, d = pl.size[:2]
    model = (center[0] - w / 2, center[1] - d / 2, center[0] + w / 2, center[1] + d / 2)
    body = (tower[0] - TOWER_BRIM, tower[1] - TOWER_BRIM,
            tower[0] + tower_width + TOWER_BRIM, tower[1] + TOWER_DEPTH_GUESS + TOWER_BRIM)
    if not (body[0] < model[2] and model[0] < body[2] and body[1] < model[3] and model[1] < body[3]):
        return center, tower  # the strip holds it
    from . import orca as orca_engine
    from ..checks import orca as check
    move = placement_move(plate, pl, center)
    cells = check.map_cells(outline, lambda x, y: move.apply_point(x, y, 0.0)[:2])
    spot = orca_engine.tower_spot(cells, orca_engine.Bed(bx0, by0, bx1, by1, bed[4], tower_width, TOWER_BRIM),
                                  tower_width, TOWER_DEPTH_GUESS, center)
    return center, spot or tower


def _strip_layout(pl: Placement, bed: tuple, tower_width: Optional[float]) -> tuple:
    """The model at one edge and the tower in the middle of the strip left, as layout() starts."""
    bx0, by0, bx1, by1 = bed[:4]
    w, d = pl.size[:2]
    mid = ((bx0 + bx1) / 2, (by0 + by1) / 2)
    if tower_width is None or not pl.fits:  # an overhanging model has no free strip to give
        return mid, None
    sx, sy = (bx1 - bx0) - w, (by1 - by0) - d
    # The tower is tower_width wide in X, so a strip at the side only works if that fits across.
    side = (by1 - by0) if sx >= tower_width + 10 else 0.0
    if side > sy:
        m = min(5.0, sx / 4)
        center = (bx0 + m + w / 2, mid[1])
        strip = (bx0 + m + w, by0, bx1, by1)
    else:
        m = min(5.0, sy / 4)
        center = (mid[0], by0 + m + d / 2)
        strip = (bx0, by0 + m + d, bx1, by1)
    tx = (strip[0] + strip[2]) / 2 - tower_width / 2
    ty = (strip[1] + strip[3]) / 2 - TOWER_DEPTH_GUESS / 2
    tx = min(max(tx, bx0), bx1 - tower_width)
    ty = min(max(ty, by0), by1 - TOWER_DEPTH_GUESS)
    return center, (tx, ty)


def placement_move(plate: Plate, pl: Placement, center: tuple) -> Transform:
    """The turn (if planned) and move that put the plate's center at `center` on the bed."""
    pre = ROT90 if pl.rotated else Transform.identity()
    corners = [pre.apply_point(x, y, z) for x in (plate.lo[0], plate.hi[0])
               for y in (plate.lo[1], plate.hi[1]) for z in (plate.lo[2], plate.hi[2])]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    dx = center[0] - (lo[0] + hi[0]) / 2
    dy = center[1] - (lo[1] + hi[1]) / 2
    dz = plate.lo[2] - lo[2]  # the bottom stays where Bambu had it: on the bed
    return pre.compose(Transform((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, dx, dy, dz)))


def apply_placement(plate: Plate, pl: Placement, bed: tuple, center: tuple) -> None:
    """Rewrite the plate's build transforms: turn if planned, then move to `center` on the bed.

    Only ever a rotation about Z and a move -- never a scale (check 11)."""
    move = placement_move(plate, pl, center)
    for bi in plate.items:
        bi.transform = Transform.parse(bi.transform).compose(move).to_string()


# --------------------------------------------------------- verification ----

@dataclass
class Check:
    n: str
    name: str
    ok: bool
    detail: str
    value: object = None
    warn: bool = False     # passed, but with something worth a look in the slicer


class _Sniff:
    """File wrapper that looks for byte strings as the data streams past."""

    def __init__(self, fh, needles: list):
        self.fh, self.needles = fh, needles
        self.found = Counter()
        self.tail = b""
        self.keep = max(len(n) for n in needles) - 1

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n)
        if data:
            buf = self.tail + data
            for nd in self.needles:
                # count only matches that end inside the new data
                self.found[nd] += buf.count(nd) - self.tail.count(nd)
            self.tail = buf[-self.keep:]
        return data


def read_3mf_config(text: str) -> tuple[dict, int, int]:
    """Parse Slic3r_PE.config exactly as PrusaSlicer's 3MF reader does.

    ConfigBase::load_from_gcode_string_legacy walks the lines from the END and
    stops at the first one that isn't '; key = value' -- so one malformed line
    silently drops every setting above it. Returns (values, lines read, key lines).
    """
    lines = text.split("\n")
    key_lines = sum(1 for ln in lines[1:] if ln.startswith("; ") and " = " in ln)
    values, read = {}, 0
    pat = re.compile(r"; ([a-z][A-Za-z0-9_]{2,}) = (.*)")
    for line in reversed(lines[1:]):  # PrusaSlicer never reads the first line
        line = line.rstrip("\r")
        if not line:
            continue
        m = pat.fullmatch(line)
        if not m or len(line) < 10:
            break
        values[m.group(1)] = m.group(2)
        read += 1
    return values, read, key_lines


def verify(out: Path, src: Source, plate: Plate, mapping: dict, printer: Printer,
           prof: Profile, ps: Optional[str], slice_it: bool = False,
           placement: Optional[Placement] = None) -> list:
    checks = []
    tools = printer.tools

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        models = [n for n in names if n.lower().endswith(".model")]
        rels = z.read("_rels/.rels") if "_rels/.rels" in names else b""
        cfg_text = z.read("Metadata/Slic3r_PE.config").decode() if "Metadata/Slic3r_PE.config" in names else ""
        mcfg = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))

        # volume ranges from the PrusaSlicer model config, per output object id
        vols, obj_ext = {}, {}
        for o in mcfg.findall("object"):
            meta = {m.get("key"): m.get("value") for m in o.findall("metadata")}
            obj_ext[o.get("id")] = meta.get("extruder")
            vols[o.get("id")] = [
                (int(v.get("firstid")), int(v.get("lastid")),
                 {m.get("key"): m.get("value") for m in v.findall("metadata")})
                for v in o.findall("volume")
            ]

        # one streaming pass over the merged model
        needles = [b"p:path", b"3D/Objects", b"paint_color", b"3dmanufacturing/production"]
        objects, items, codes = [], [], Counter()
        painted = triangles = components = 0
        cur = None
        with z.open(ROOT_MODEL) as raw:
            sniff = _Sniff(raw, needles)
            for event, el in ET.iterparse(sniff, events=("start", "end"), huge_tree=True,
                                          tag=(CORE_OBJECT, CORE_VERTEX, CORE_TRIANGLE, CORE_ITEM, CORE_COMPONENT)):
                tag = el.tag
                if event == "start":
                    if tag == CORE_OBJECT:
                        oid = el.get("id")
                        vs = vols.get(oid, [])
                        part = bytearray(max([b + 1 for _, b, _ in vs], default=0))
                        for a, b, md in vs:
                            if md.get("volume_type", "ModelPart") == "ModelPart":
                                part[a:b + 1] = b"\x01" * (b + 1 - a)
                        cur = {"id": oid, "verts": array("d"), "mask": None, "ntri": 0, "part": part}
                        objects.append(cur)
                    continue
                if tag == CORE_VERTEX:
                    cur["verts"].extend((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                elif tag == CORE_TRIANGLE:
                    a = el.attrib
                    idx = cur["ntri"]
                    cur["ntri"] += 1
                    triangles += 1
                    if cur["mask"] is None:
                        cur["mask"] = bytearray(len(cur["verts"]) // 3)
                    if idx < len(cur["part"]) and cur["part"][idx]:
                        mask = cur["mask"]
                        mask[int(a["v1"])] = mask[int(a["v2"])] = mask[int(a["v3"])] = 1
                    code = a.get(MMU)
                    if code is not None:
                        painted += 1
                        codes[code] += 1
                elif tag == CORE_ITEM:
                    items.append((el.get("objectid"), el.get("transform")))
                elif tag == CORE_COMPONENT:
                    components += 1
                else:
                    continue
                el.clear()
                while el.getprevious() is not None:
                    del el.getparent()[0]

    # 1. one flat model
    bad = {n.decode(): c for n, c in sniff.found.items() if c and n != b"paint_color"}
    one = models == [ROOT_MODEL] and not bad and components == 0 and b"/3D/3dmodel.model" in rels
    checks.append(Check("1", "single flat 3D/3dmodel.model", one,
                        f"model parts {models}, components {components}, refs {bad or 'none'}"))

    # 2. no Bambu paint left
    left = sniff.found[b"paint_color"]
    checks.append(Check("2", "paint is all mmu_segmentation", left == 0,
                        f"paint_color {left}, mmu_segmentation {painted:,}"))

    # 3. every tool number in range, object extruders mapped
    try:
        hist_out, paint_err = histogram(codes), ""
    except ValueError as e:
        hist_out, paint_err = Counter(), f"  UNREADABLE PAINT: {e}"
    states = sorted(s for s in hist_out if s)
    vol_ext = sorted({int(md["extruder"]) for vs in vols.values() for _, _, md in vs if md.get("extruder")})
    out_obj = [obj_ext.get(o["id"]) for o in objects]
    want_obj = []
    for oid in plate.objects:
        so = src.project.settings_objects.get(oid)
        if so and so.extruder:
            e = so.extruder
            want_obj.append(str(mapping.get(int(e), int(e))) if e.isdigit() else e)
        else:
            want_obj.append(None)
    tops = max([0, *states, *vol_ext, *[int(e) for e in out_obj if e]])
    checks.append(Check("3", f"tools <= {tools}", tops <= tools and out_obj == want_obj and not paint_err,
                        f"paint states {states}, volume extruders {vol_ext}, object extruders "
                        f"{out_obj} (want {want_obj}){paint_err}"))

    # 4. painted triangles in == out
    checks.append(Check("4", "painted faces in == out", painted == plate.painted and triangles == plate.triangles,
                        f"painted {plate.painted:,} -> {painted:,}, faces {plate.triangles:,} -> {triangles:,}",
                        painted))

    # 5. per-slot leaf histogram survives the mapping
    try:
        want = remap_histogram(histogram(plate.codes), mapping)
    except ValueError as e:
        want, paint_err = Counter(), paint_err + f"  UNREADABLE SOURCE PAINT: {e}"
    diff = {s: (want.get(s, 0), hist_out.get(s, 0)) for s in set(want) | set(hist_out)
            if want.get(s, 0) != hist_out.get(s, 0)}
    shown = ", ".join(f"T{s}:{hist_out[s]:,}" for s in sorted(hist_out))
    checks.append(Check("5", "paint leaves per tool in == out", not diff and not paint_err,
                        f"{shown}" + (f"  MISMATCH {diff}" if diff else "") + paint_err))

    # 6. modifiers / negative parts / support volumes
    got = Counter(md.get("volume_type", "ModelPart") for vs in vols.values() for _, _, md in vs)
    special = Counter({k: v for k, v in got.items() if k != "ModelPart"})
    checks.append(Check("6", "modifier/negative parts match", special == plate.special,
                        f"source {dict(plate.special) or 'none'}, output {dict(special) or 'none'}, "
                        f"parts {got.get('ModelPart', 0)}"))

    # 7. colors in the embedded config, read the way PrusaSlicer reads it
    values, read, key_lines = read_3mf_config(cfg_text)
    fc = values.get("filament_colour", "").split(";")
    ec = values.get("extruder_colour", "").split(";")
    wrong = []
    for old, tool in sorted(mapping.items()):
        want_c = src.palette[old - 1]
        f_c = fc[tool - 1].upper() if tool <= len(fc) else None
        e_c = ec[tool - 1].upper() if tool <= len(ec) else None
        if f_c != want_c or e_c != want_c:
            wrong.append(f"T{tool} {f_c}/{e_c} want {want_c}")
    nt = len(values.get("nozzle_diameter", "").split(",")) if values.get("nozzle_diameter") else 0
    ok7 = (bool(cfg_text) and read == key_lines and not wrong and nt == tools
           and values.get("printer_settings_id") == printer.preset)
    pal = " ".join(f"T{t}={src.palette[o - 1]}" for o, t in sorted(mapping.items(), key=lambda x: x[1]))
    checks.append(Check("7", "config colors == mapped palette", ok7,
                        f"{pal}; {read}/{key_lines} config lines readable, {nt} tools, "
                        f"{values.get('printer_settings_id')}" + (f"  WRONG {wrong}" if wrong else "")))

    # 8. on the bed, exactly: every printed vertex through its build transform
    by_id = {o["id"]: o for o in objects}
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for oid, tr in items:
        o = by_id[oid]
        m = Transform.parse(tr).m
        verts, mask = o["verts"], o["mask"] or bytearray(len(o["verts"]) // 3)
        for i in range(len(mask)):
            if mask[i]:
                x, y, z = verts[3 * i], verts[3 * i + 1], verts[3 * i + 2]
                p = (x * m[0] + y * m[3] + z * m[6] + m[9],
                     x * m[1] + y * m[4] + z * m[7] + m[10],
                     x * m[2] + y * m[5] + z * m[8] + m[11])
                for k in range(3):
                    if p[k] < lo[k]:
                        lo[k] = p[k]
                    if p[k] > hi[k]:
                        hi[k] = p[k]
    bx0, by0, bx1, by1, bh = prof.bed
    on_bed = (lo[0] >= bx0 - EPS and lo[1] >= by0 - EPS and hi[0] <= bx1 + EPS
              and hi[1] <= by1 + EPS and hi[2] <= bh + EPS and lo[2] >= -0.05)
    where = (f"x {lo[0]:.1f}..{hi[0]:.1f} y {lo[1]:.1f}..{hi[1]:.1f} z {lo[2]:.2f}..{hi[2]:.1f}"
             f" on {bx1 - bx0:g} x {by1 - by0:g} x {bh:g}")
    if placement is not None and not placement.fits:
        # Known to be bigger than the bed: placed centered at full size, and that's fine.
        pct = math.floor(placement.max_scale * 1000) / 10
        checks.append(Check("8", "on the bed", lo[2] >= -0.05,
                            f"bigger than the bed, centered at full size ({where}) -- "
                            f"fits at {pct:g}% or less, scale it in PrusaSlicer before printing", warn=True))
    else:  # it fits, so ending up off the bed would be a placement bug
        checks.append(Check("8", "on the bed", on_bed, where))

    # 11. the model is the size it was designed: printed parts' dimensions in == out
    # (width and depth swap places if it was turned 90 degrees to fit)
    got = tuple(hi[k] - lo[k] for k in range(3))
    want = tuple(plate.size)
    if placement is not None and placement.rotated:
        want = (want[1], want[0], want[2])
    same = all(abs(g - w) <= 0.01 for g, w in zip(got, want))
    size_check = Check("11", "size unchanged", same,
                       f"{got[0]:.2f} x {got[1]:.2f} x {got[2]:.2f} mm, source {want[0]:.2f} x {want[1]:.2f} x {want[2]:.2f}"
                       + (" (turned 90)" if placement is not None and placement.rotated else ""))

    # 9. PrusaSlicer itself opens it and reads the same config
    if ps:
        model_tris = sum(b - a + 1 for vs in vols.values() for a, b, md in vs
                         if md.get("volume_type", "ModelPart") == "ModelPart")
        checks.append(prusaslicer_check(out, ps, model_tris, mapping, src, tools))
    # 10. PrusaSlicer slices it and writes the G-code (only with --slice; a plate
    # bigger than the bed can't be sliced at full size, so it isn't tried)
    if ps and slice_it:
        if placement is not None and not placement.fits:
            checks.append(Check("10", "PrusaSlicer slices it", True,
                                "not sliced: bigger than the bed", warn=True))
        else:
            checks.append(slice_check(out, ps))
    checks.append(size_check)
    return checks


def prusaslicer_check(out: Path, ps: str, model_tris: int, mapping: dict, src: Source, tools: int) -> Check:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        saved = Path(tmp) / "loaded.ini"
        try:
            r = run_slicer([ps, "--datadir", ensure_datadir(), "--info", out, "--save", saved], timeout=600)
        except subprocess.TimeoutExpired:
            return Check("9", "PrusaSlicer --info", False, "timed out after 600 s")
        facets = sum(int(m) for m in re.findall(r"^number_of_facets = (\d+)", r.stdout, re.M))
        manifold = re.findall(r"^manifold = (\w+)", r.stdout, re.M)
        loaded = {}
        if saved.exists():
            for line in saved.read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition(" = ")
                if sep:
                    loaded[k] = v
    fc = loaded.get("filament_colour", "").split(";")
    colors_ok = bool(loaded) and all(
        tool <= len(fc) and fc[tool - 1].upper() == src.palette[old - 1] for old, tool in mapping.items())
    nt = len(loaded.get("nozzle_diameter", "").split(",")) if loaded.get("nozzle_diameter") else 0
    ok = r.returncode == 0 and facets == model_tris and colors_ok and nt == tools
    err = (r.stderr.strip().splitlines() or [""])[-1][:120]
    return Check("9", "PrusaSlicer --info loads it", ok,
                 f"exit {r.returncode}, facets {facets:,} (want {model_tris:,}), manifold {','.join(manifold)}, "
                 f"reloaded {nt} tools, colors {'ok' if colors_ok else 'WRONG'}" + (f"  {err}" if r.returncode else ""))


def slice_check(out: Path, ps: str) -> Check:
    """Slice the file as it stands and write the G-code to a temp dir, then throw it away.

    This is the only check that sees the wipe tower: its size comes from the purges
    and its cone grows with the print's height, and PrusaSlicer 2.9.6 logs 'gcode
    path conflicts' and then crashes writing the G-code when it hits the model.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            r = run_slicer([ps, "--datadir", ensure_datadir(), "--dont-arrange",
                            "--export-gcode", "--output", Path(tmp) / "check.bgcode", out], timeout=1800)
        except subprocess.TimeoutExpired:
            return Check("10", "PrusaSlicer slices it", False, "timed out after 30 min")
        wrote = (Path(tmp) / "check.bgcode").exists() and "Slicing result exported" in r.stdout
    log = r.stdout + r.stderr
    errors = list(dict.fromkeys(ln.split("]")[-1].strip() for ln in log.splitlines() if "[error]" in ln))
    conflicts = [e for e in errors if "conflict" in e]
    # A crash, no G-code or a wipe tower/object path conflict fails. Anything else PrusaSlicer
    # logs as an error while still writing the G-code -- Arachne giving up on a Voronoi
    # diagram for one island, say -- is passed with a warning to look at that layer.
    ok = r.returncode == 0 and wrote and not conflicts
    shown = conflicts or errors
    why = "; ".join(shown[:2]) or ("" if ok else (log.strip().splitlines() or ["no output"])[-1][:120])
    if conflicts:
        why += (" -- the wipe tower doesn't fit beside the model. Moving the tower or lowering"
                " its cone angle in PrusaSlicer is your call")
    return Check("10", "PrusaSlicer slices it", ok,
                 f"exit {r.returncode}, G-code {'written' if wrote else 'NOT written'}"
                 + (f"  {'PrusaSlicer logged: ' if ok else ''}{why}" if why else ""),
                 warn=ok and bool(errors))


# -------------------------------------------------------------- convert ----

@dataclass
class Row:
    model: str
    mapped: str
    plates: str
    painted: str
    checks: str
    output: str
    ok: bool


def mapping_text(mapping: dict) -> str:
    olds = ",".join(str(o) for o in sorted(mapping))
    news = ",".join(str(mapping[o]) for o in sorted(mapping))
    return f"[{olds}] -> [{news}]"


def convert_one(src: Source, printer_key: str, explicit: Optional[dict], slice_it: bool, out: Path,
                prefix: str = "", suffix: str = "") -> list:
    """Write `src`'s file(s) for one Prusa printer, one per plate; returns its table rows."""
    printer = PRINTERS[printer_key]
    prof = load_profile(printer_key)
    bed = prof.bed
    ps = find_prusaslicer()
    OUT = out / src.display

    multi = len(src.plates) > 1
    names = [out_name(src.display, printer_key, prefix, f" - Plate {plate.number}" if multi else "", suffix)
             for plate in src.plates]

    # Every decision first: nothing is written for a model that would be refused,
    # and an older file for it doesn't get to stand in for this run.
    try:
        mapping = decide_mapping(src, printer, explicit)
        check_filament_types(src, mapping)
        placements = [plan_placement(src, plate, bed) for plate in src.plates]
    except Refusal:
        for n in names:
            if (OUT / n).exists() and ours(OUT / n):
                (OUT / n).unlink()
        raise
    config, notes = build_config(prof, src, mapping, printer)

    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for plate, pl, name in zip(src.plates, placements, names):
        final = OUT / name
        # Not dot-named (macOS would flag it hidden), and still ending in .3mf: PrusaSlicer goes by extension.
        tmp = OUT / f"partial - {name}"
        center, tower = layout(pl, bed, prof.tower_width, plate, outlines(src.path).get(plate.number or 1))
        apply_placement(plate, pl, bed, center)
        try:
            stats = write_prusa_3mf(src.project, str(tmp),
                                    plates=[plate.number] if plate.number is not None else None,
                                    extruder_map=mapping)
        except ValueError as e:  # Panda2Prusa refusing the --map (e.g. two filaments on one tool)
            tmp.unlink(missing_ok=True)
            raise Refusal(f"REFUSING {src.slug}: Panda2Prusa rejected the mapping: {e}")
        if stats.objects_written == 0:
            tmp.unlink(missing_ok=True)
            raise Refusal(f"{src.slug}: Panda2Prusa wrote no objects for plate {plate.number}")
        with zipfile.ZipFile(tmp, "a", compression=zipfile.ZIP_DEFLATED) as z:
            z.comment = STAMP
            z.writestr("Metadata/Slic3r_PE.config", config)
            if tower:
                z.writestr("Metadata/Prusa_Slicer_wipe_tower_information.xml",
                           '<?xml version="1.0" encoding="utf-8"?>\n<wipe_tower_information bed_idx="0" '
                           f'position_x="{tower[0]:.2f}" position_y="{tower[1]:.2f}" rotation_deg="0"/>\n')

        checks = verify(tmp, src, plate, mapping, printer, prof, ps, slice_it, pl)
        ok = all(c.ok for c in checks)
        where = f"plate {plate.number}" if multi else ""
        turned = ", turned 90 deg" if pl.rotated else ""
        if pl.fits:
            tight = f", spare {pl.spare[0]:.1f} x {pl.spare[1]:.1f} mm"
        else:
            tight = f", ⚠ bigger than the bed (fits at {math.floor(pl.max_scale * 1000) / 10:g}% or less)"
        tight += f", wipe tower at {tower[0]:.0f},{tower[1]:.0f}" if tower else ""
        print(f"\n  {'ok ' if ok else 'FAIL'} {src.slug} {where} -> {name}")
        print(f"       slots {mapping_text(mapping)}{turned}{tight}")
        for c in checks:
            print(f"       {('⚠' if c.warn else '✓') if c.ok else '✗'} {c.n}. {c.name}: {c.detail}")
        for w in stats.warnings:
            print(f"       panda2prusa: {w}")
        if plate is src.plates[0]:
            for n in notes:
                print(f"       settings: {n}")

        if ok:
            if final.exists() and not ours(final):
                prev = final.parent / "_previous"
                prev.mkdir(exist_ok=True)
                dest, n = prev / final.name, 2
                while dest.exists():  # never overwrite an earlier backup either
                    dest = prev / f"{final.stem} ({n}){final.suffix}"
                    n += 1
                shutil.move(str(final), str(dest))
                print(f"       moved the existing {final.name} (not made by SlicerBridge) to _previous/{dest.name}")
            tmp.replace(final)
            unhide(final)
            mb = final.stat().st_size / 1e6
            print(f"       saved {mb:.0f} MB")
        else:
            tmp.unlink(missing_ok=True)
            if final.exists() and ours(final):
                final.unlink()
            print("       !! verification failed -- output deleted, nothing shipped for this plate")
        passed = sum(c.ok for c in checks)
        rows.append(Row(
            f"{src.slug}" + (f" p{plate.number}" if multi else ""),
            mapping_text(mapping),
            f"{plate.number or 1} of {len(src.plates)}",
            f"{plate.painted:,} / {next(c.value for c in checks if c.n == '4'):,}",
            f"{passed}/{len(checks)} {'✓' if ok else '✗ FAIL'}"
            + (f" ⚠ bed ≤{math.floor(pl.max_scale * 1000) / 10:g}%" if not pl.fits else "")
            + (" ⚠ slicer log" if any(c.warn and c.n == "10" and pl.fits for c in checks) else ""),
            name if ok else "(deleted)",
            ok,
        ))
    return rows


def unhide(path: Path) -> None:
    """Clear macOS's hidden flag on a finished file.

    iCloud's Documents sync flags any dot-named file or folder under
    ~/Documents as hidden within about 2 seconds,
    and the flag survives a rename. Temp files are no longer dot-named; this is
    the guard in case anything else sets it.
    """
    flags = getattr(os.stat(path), "st_flags", 0)
    if flags & stat.UF_HIDDEN:
        os.chflags(path, flags & ~stat.UF_HIDDEN)



def ours(path: Path) -> bool:
    """Whether SlicerBridge wrote this file (its zip comment), so it may be replaced."""
    with zipfile.ZipFile(path) as z:
        return z.comment.startswith((STAMP, *LEGACY_STAMPS))

def print_table(rows: list, refused: list) -> None:
    print("\n| model | used slots -> mapped | plates | painted faces in/out | checks | output file |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r.model} | {r.mapped} | {r.plates} | {r.painted} | {r.checks} | {r.output} |")
    for slug, why in refused:
        first = why.splitlines()[0].replace("REFUSING ", "")
        print(f"| {slug} | — | — | — | refused | {first} |")


def inspect(path: Path, keys: list[str]) -> list[str]:
    """Lines describing how a source fits each Prusa printer: tools, and each plate on its bed."""
    src = load_source(path.stem, path.stem, path)
    lines = []
    for key in keys:
        pr = PRINTERS[key]
        try:
            bed = load_profile(key).bed
        except Refusal as e:
            lines.append(f"    {pr.label}: no profile ({str(e).splitlines()[0]})")
            continue
        bits = []
        for p in src.plates:
            pl = plan_placement(src, p, bed)
            if pl.fits:
                bits.append("fits" + (" turned 90" if pl.rotated else ""))
            else:
                bits.append(f"bigger than the bed (fits <= {math.floor(pl.max_scale * 1000) / 10:g}%)")
        tools = "ok" if len(src.used) <= pr.tools else "NO"
        lines.append(f"    {pr.label}: tools {tools} ({len(src.used)}/{pr.tools}), bed {' | '.join(bits)}")
    return lines


def convert(path: Path, name: str, keys: list[str], out: Path, prefix: str = "", slice_it: bool = False,
            explicit: Optional[dict] = None, suffix: str = "") -> tuple[int, int]:
    """Write `name`'s files for each Prusa printer key into out/<name>/; returns (written, refused or failed)."""
    rows, refused = [], []
    for key in keys:
        # Read afresh for every printer: placing a plate moves the source's own build items.
        try:
            src = load_source(path.stem, name, path)
            rows += convert_one(src, key, explicit, slice_it, out, prefix, suffix)
        except Refusal as e:
            print(f"\n  SKIP {name} {PRINTERS[key].label}\n!! {e}")
            refused.append((f"{name} {PRINTERS[key].label}", str(e)))
        except Exception as e:  # a bug or a slicer gone wrong: this printer fails, the rest still get theirs
            print(f"\n  FAIL {name} {PRINTERS[key].label}\n!! {type(e).__name__}: {e}")
            refused.append((f"{name} {PRINTERS[key].label}", f"{type(e).__name__}: {e}"))
        src = None
        gc.collect()
    print_table(rows, refused)
    made = sum(1 for r in rows if r.ok)
    return made, len(rows) - made + len(refused)

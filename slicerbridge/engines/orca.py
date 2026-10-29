"""
engines/orca.py -- a multicolor project (Bambu Studio, OrcaSlicer or a fork) -> one
ready file per Bambu Lab or OrcaSlicer-family printer.

The printers (printers.PRINTERS) all run Bambu Studio or a slicer forked from
OrcaSlicer, and read the Bambu .3mf format:

- Colors. What counts is the filament slots a model PAINTS or ASSIGNS, never the
  palette size. They are compacted to filaments 1..K in slot order, and every
  paint_color is re-encoded to match (Panda2Prusa's paint.transform_states).
  More colors than the printer holds is a refusal: merging two colors is a
  design decision, made in the designer's slicer. Each filament gets that printer's
  preset for its type.
- Settings. The printer's own slicer writes its settings for one filament of
  each type (templates.py); a file's settings are those, one filament column
  per color, the bed type the app would pick, and the source's own flush volumes
  for its colors. Then the print settings the designer chose in their slicer
  (supports, infill, walls, layer height and the rest that shape the print,
  and everything the source marks as changed: carry.py) are carried over where
  the target has the same setting, and the
  slicer reads the whole result back: a value it doesn't take keeps the
  printer's, and is named in the output.
- Size: never changed. Each plate moves onto its own plate of the printer's bed
  (turned 90 degrees only if that is what makes it fit), clear of the bed's
  exclusion area, with the prime tower in the space left. A model bigger than
  the bed is centered at full size, with a warning and the scale that would fit.

Every file is audited by checks/orca.py before it is written, and a file that
fails is never written.
"""
from __future__ import annotations

import copy
import gc
import json
import math
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .._vendor.panda2prusa import ns
from .._vendor.panda2prusa.convert import describe, used_filaments
from .._vendor.panda2prusa.paint import transform_states
from .._vendor.panda2prusa.transform import Transform

from .. import tall
from .. import templates as templates_build
from ..carry import carry_print_settings
from ..checks import orca as verify
from ..errors import Refusal
from ..printers import BAMBU_TYPES, LEGACY_STAMPS, PRINTERS, SLICERS, STAMP, TEMPLATES, out_path, run_slicer

PAINT = re.compile(rb'( paint_color=")([0-9A-Fa-f]+)(")')
# Filament numbers in model_settings.config: part/object extruders, and settings
# like support_filament or wall_filament. 0 means "the default" and never moves.
FILAMENT_META = re.compile(r'(key="(?:extruder|[a-z_]+_filament)" value=")(\d+)(")')
LAYER_RANGE_EXTRUDER = re.compile(r'(<option opt_key="extruder">)(\d+)(</option>)')
ITEM = re.compile(r'<item\b[^>]*>')
# What Bambu Studio left of its own slice: plate info in Bambu's bed coordinates,
# its filament order by Bambu slot number, G-code for a Bambu printer.
BAMBU_LEFTOVERS = verify.BAMBU_LEFTOVERS

# Bambu Studio and its forks lay plates out in a grid, each plate one bed plus a
# fifth of a bed apart (PartPlate's LOGICAL_PART_PLATE_GAP).
PLATE_STRIDE = 1.2
EPS = 1e-4                 # mm of tolerance on bed edges

# Settings OrcaSlicer fills in or keeps per plate when it saves: not ours to compare.
READ_BACK_IGNORED = {"upward_compatible_machine", "wipe_tower_x", "wipe_tower_y"}
# Preset values a slicer itself can't slice: OrcaSlicer 2.4.2's seam placer loops forever
# with staggered inner seams on (the Creality Hi preset's; a 2-plate test model hung 46 min in
# SeamPlacer::place_seam, and slices in 52 s with it off). It only offsets where the
# inner walls' seams line up, so off is safe.
SLICER_WORKAROUNDS = {"staggered_inner_seams": "0"}
# Bambu Studio's names for values OrcaSlicer reads back under another: carried under OrcaSlicer's.
ORCA_NAMES = {("ironing_pattern", "zig-zag"): "rectilinear", ("support_ironing_pattern", "zig-zag"): "rectilinear"}
# Settings the slicer recomputes for its own machine on load: what it reads back is written.
SLICER_OWNED = {"flush_volumes_matrix", "flush_volumes_vector", "flush_multiplier"}
# What a brand's own slicer reads differently from OrcaSlicer (brandcheck.py slices
# each file in it), written so that both read it the same:
# - QIDI Studio 2.07 takes skeleton_infill_line_width and skin_infill_line_width in mm
#   only: OrcaSlicer's "100%" is 100 mm to it, "too large line width", and it won't
#   slice. An OrcaSlicer line width in % is of the nozzle diameter, so every one is
#   written in mm, which all of them read the same.
# - Anycubic Slicer Next 2.0 reads bed_exclude_area as polygons of 4 points each,
#   OrcaSlicer as one polygon: each takes the other's Kobra 3 frame (the bed's outline
#   with a 2 mm smaller one inside) for the whole bed, "too close to exclusion area".
#   Where the two readings differ, the area is rewritten as its rectangles, each one
#   reached from the bed's corner along zero-width edges, as OrcaSlicer's own Kobra 3
#   Max preset is written.
# - The Kobra X's color change in OrcaSlicer 2.4.2 indexes ace_t_box_vector, each
#   filament's ACE Pro slot. Anycubic Slicer Next fills it only once a printer's
#   filaments are mapped, and until then won't slice: "Indexing an empty vector
#   variable". Anycubic's own preset checks its size first, a check OrcaSlicer can't
#   parse (it has no such variable) except inside the block it skips, so it goes there.
ACE_BLOCK, ACE_GUARD = verify.ACE_BLOCK, verify.ACE_GUARD
MACRO = re.compile(r"\{([^{}]*)\}")

ROOT_MODEL = "3D/3dmodel.model"
CORE_OBJECT = ns.q(ns.CORE, "object")
CORE_MESH = ns.q(ns.CORE, "mesh")
CORE_VERTICES = ns.q(ns.CORE, "vertices")
CORE_VERTEX = ns.q(ns.CORE, "vertex")
CORE_TRIANGLES = ns.q(ns.CORE, "triangles")
CORE_TRIANGLE = ns.q(ns.CORE, "triangle")
CORE_COMPONENTS = ns.q(ns.CORE, "components")
CORE_COMPONENT = ns.q(ns.CORE, "component")
P_PATH = ns.q(ns.PRODUCTION, "path")


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
    exclusions: tuple = ()        # (x0, y0, x1, y1) rectangles nothing may touch
    edge: float = 5.0             # mm kept between the model and the bed's edge: its brim and skirt
    plate: tuple = ()             # (width, depth) of the whole printable area, where it's more than every nozzle reaches

    @classmethod
    def of(cls, settings: dict) -> "Bed":
        pts = [tuple(float(c) for c in p.split("x")) for p in settings["printable_area"]]
        plate = (max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts))
        x0, y0, x1, y1 = verify.reachable_area(settings)
        tower = float(settings.get("prime_tower_width") or 35) if settings.get("enable_prime_tower") == "1" else None
        at = (float((settings.get("wipe_tower_x") or ["0"])[0]), float((settings.get("wipe_tower_y") or ["0"])[0]))
        return cls(x0, y0, x1, y1, float(settings["printable_height"]), tower,
                   verify.tower_size(settings)[2], at,
                   tuple(verify.exclusion_rects(settings)), verify.outline_reach(settings) + 2, plate)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def depth(self) -> float:
        return self.y1 - self.y0


def plate_origin(index: int, count: int, bed: Bed) -> tuple[float, float]:
    """Where plate `index` (0-based) of `count` sits, as Bambu's PartPlateList lays them out."""
    v = math.sqrt(count)
    cols = round(v) + 1 if v > round(v) else round(v)
    row, col = divmod(index, cols)
    width, depth = bed.plate or (bed.width, bed.depth)
    return col * width * PLATE_STRIDE, -row * depth * PLATE_STRIDE


def parse_filaments(path: Path) -> list[dict]:
    """The source's palette, from project_settings.config.

    Projects are often saved unsliced, so slice_info.config may be
    an empty stub; project_settings.config always carries the palette.
    """
    with zipfile.ZipFile(path) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config").decode())
    colors, types = cfg.get("filament_colour", []), cfg.get("filament_type", [])
    return [{"id": str(i + 1), "type": types[i] if i < len(types) else "PLA", "color": c}
            for i, c in enumerate(colors)]


# ------------------------------------------------------------- geometry ----

@dataclass
class Plate:
    number: int                                   # Bambu's plater_id, 1-based
    items: list = field(default_factory=list)     # (object id, 3MF transform string)
    lo: list = field(default_factory=lambda: [math.inf] * 3)
    hi: list = field(default_factory=lambda: [-math.inf] * 3)
    cells: set = field(default_factory=set)       # its outline from above, verify.CELL mm cells

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
            mesh = objs[key].find(CORE_MESH)
            verts = mesh.find(CORE_VERTICES)
            xs, ys = [], []
            for v in (verts.iterchildren(CORE_VERTEX) if verts is not None else ()):
                x, y, z = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                p = (x * m[0] + y * m[3] + z * m[6] + m[9],
                     x * m[1] + y * m[4] + z * m[7] + m[10],
                     x * m[2] + y * m[5] + z * m[8] + m[11])
                xs.append(p[0])
                ys.append(p[1])
                for k in range(3):
                    if p[k] < lo[k]:
                        lo[k] = p[k]
                    if p[k] > hi[k]:
                        hi[k] = p[k]
            tris = mesh.find(CORE_TRIANGLES)
            if tris is not None:
                verify.add_outline(plate.cells, xs, ys,
                                   ((int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
                                    for t in tris.iterchildren(CORE_TRIANGLE)))
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
    crowded: bool = False  # no strip beside the model is big enough for the prime tower
    fits: bool = True
    max_scale: float = 1.0  # largest scale that would fit, when it doesn't -- for the warning only
    excluded: bool = False  # no spot kept the model and tower clear of the bed's exclusion area


def fit_scale(size: tuple, bed: Bed) -> float:
    """Largest uniform scale that fits, turned or not -- reported, never applied."""
    plain = min(bed.width / size[0], bed.depth / size[1], bed.height / size[2])
    turned = min(bed.width / size[1], bed.depth / size[0], bed.height / size[2])
    return max(plain, turned)


def orientation(size: tuple, bed: Bed) -> Optional[bool]:
    """False if it fits as Bambu had it, True if only turned 90 degrees, None if neither."""
    sx, sy, sz = size
    for rotated, w, d in ((False, sx, sy), (True, sy, sx)):
        if w <= bed.width + EPS and d <= bed.depth + EPS and sz <= bed.height + EPS:
            return rotated
    return None


# A strip must hold the estimated tower with this much to spare, or the tower goes
# where the outline leaves room: the estimate runs short (IronJaw's P2S tower printed
# 8 mm deeper than its A1 one, from the same estimate).
TOWER_SLACK = 12.0


def spots(w: float, d: float, bed: Bed, tw: float, td: float) -> list[tuple]:
    """Candidate (model center, tower position or None, crowded) on the bed, best first.

    With a prime tower (tw wide, about td deep) the model goes to one edge and the
    tower into the middle of the strip left -- the edge with the roomier strip
    first, as the u1 engine does, then the other two. Without one, the model is
    centered first.
    """
    spare_x, spare_y = bed.width - w, bed.depth - d
    mid = ((bed.x0 + bed.x1) / 2, (bed.y0 + bed.y1) / 2)
    # Room at the bed's edge for the brim and skirt (a QIDI preset's skirt ran off the
    # bed at 5 mm), without eating the strip the tower needs on the far side -- where
    # that side can hold a tower at all; where it can't, the model takes the room.
    b = bed.tower_brim
    share_x = 4 if bed.tower is not None and spare_x >= tw + 2 * b + TOWER_SLACK else 2
    share_y = 4 if bed.tower is not None and spare_y >= td + 2 * b + TOWER_SLACK else 2
    mx, my = min(max(5.0, bed.edge), spare_x / share_x), min(max(5.0, bed.edge), spare_y / share_y)
    edges = {
        "left":  ((bed.x0 + mx + w / 2, mid[1]), (bed.x0 + mx + w, bed.y0, bed.x1, bed.y1)),
        "front": ((mid[0], bed.y0 + my + d / 2), (bed.x0, bed.y0 + my + d, bed.x1, bed.y1)),
        "right": ((bed.x1 - mx - w / 2, mid[1]), (bed.x0, bed.y0, bed.x1 - mx - w, bed.y1)),
        "back":  ((mid[0], bed.y1 - my - d / 2), (bed.x0, bed.y0, bed.x1, bed.y1 - my - d)),
    }
    if bed.tower is None:
        return [(mid, None, False)] + [(c, None, False) for c, _strip in edges.values()]
    side = bed.depth if spare_x >= tw + 10 else 0.0
    order = ["left", "front"] if side > spare_y else ["front", "left"]
    b, out = bed.tower_brim, []
    for name in order + ["right", "back"]:
        center, strip = edges[name]
        tx = min(max((strip[0] + strip[2] - tw) / 2, bed.x0 + b), bed.x1 - tw - b)
        ty = min(max((strip[1] + strip[3] - td) / 2, bed.y0 + b), bed.y1 - td - b)
        crowded = strip[2] - strip[0] < tw + 2 * b + TOWER_SLACK or strip[3] - strip[1] < td + 2 * b + TOWER_SLACK
        out.append((center, (tx, ty), crowded))
    return out


def place(slug: str, plate: Plate, count: int, bed: Bed, tw: float = 0.0, td: float = 0.0) -> Placement:
    """Move the plate onto its own plate of this bed: a turn about Z and a move, never a scale.

    tw x td is the prime tower's likely footprint, when the printer prints one.
    """
    if not all(math.isfinite(d) and d > 0 for d in plate.size):
        raise Refusal(f"REFUSING {slug}: plate {plate.number} has no printed part to place")
    rotated = orientation(plate.size, bed)
    fits = rotated is not None
    rotated = bool(rotated)
    sx, sy, sz = plate.size
    w, d = (sy, sx) if rotated else (sx, sy)

    tower, crowded, excluded = None, False, False
    if not fits:
        # Bigger than the bed: centered at full size, as Bambu had it. There's no
        # strip to give the tower, so it keeps the profile's own spot -- pulled
        # onto the bed, since OrcaSlicer's default (15, 220) is off a 220 mm one.
        center = ((bed.x0 + bed.x1) / 2, (bed.y0 + bed.y1) / 2)
        if bed.tower is not None:
            tower = (min(max(bed.tower_at[0], bed.x0), bed.x1 - tw), min(max(bed.tower_at[1], bed.y0), bed.y1 - td))
    else:
        def clash(c: tuple, t: Optional[tuple]) -> bool:
            # The model with its brim and skirt: the QIDI Q2's brim reached its corner
            # exclusion from a model 1.6 mm clear of it ("unprintable area").
            e = bed.edge
            boxes = [(c[0] - w / 2 - e, c[1] - d / 2 - e, c[0] + w / 2 + e, c[1] + d / 2 + e)]
            if t is not None:
                b = bed.tower_brim + EXCLUSION_GAP
                boxes.append((t[0] - b, t[1] - b, t[0] + tw + b, t[1] + td + b))
            return any(verify.overlaps(box, r) for box in boxes for r in bed.exclusions)

        options = spots(w, d, bed, tw, td)
        ranked = sorted(range(len(options)), key=lambda i: (clash(options[i][0], options[i][1]), options[i][2], i))
        center, tower, crowded = options[ranked[0]]
        excluded = clash(center, tower)

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
    if fits and tower is not None and (crowded or excluded) and plate.cells:
        # No strip beside the bounding box: put the tower where the outline leaves room.
        outline = verify.map_cells(plate.cells, lambda x, y: (lambda p: (p[0] - ox, p[1] - oy))(
            move.apply_point(x, y, 0.0)))
        spot = tower_spot(outline, bed, tw, td, center)
        if spot is not None:
            tower, crowded = spot, False
            excluded = clash(center, tower)
    transforms = {oid: Transform.parse(tr).compose(move).to_string() for oid, tr in plate.items}
    return Placement(rotated, (w, d, sz), transforms, tower, crowded, fits,
                     1.0 if fits else fit_scale(plate.size, bed), excluded)


# Room kept between the prime tower's brim and the model's outline, tried widest
# first; and between the brim and the bed's edge, where Bambu Studio checks the
# tower with more conservative parameters than it slices with.
TOWER_CLEARANCES = (10.0, 6.0, 3.0)
TOWER_EDGE = 2.0
# Kept between the tower's brim and an exclusion area: OrcaSlicer refused a Kobra 3
# tower whose brim stopped short of the bed's 2 mm frame ("too close to exclusion area").
EXCLUSION_GAP = 3.0


def tower_spot(outline: set, bed: Bed, tw: float, td: float, center: tuple) -> Optional[tuple]:
    """A prime tower corner on the bed clear of the model's outline, or None.

    Of the spots clear by the widest clearance that leaves any, the one nearest the
    bed's edge, then farthest from the model's center: out of the way, like a person
    would put it.
    """
    b, step = bed.tower_brim, verify.CELL
    # The skirt goes around the tower too (the QIDI Q2's ran off the bed from 2 mm).
    e = b + max(TOWER_EDGE, bed.edge)
    xs = [bed.x0 + e + k * step for k in range(int((bed.width - tw - 2 * e) // step) + 1)]
    ys = [bed.y0 + e + k * step for k in range(int((bed.depth - td - 2 * e) // step) + 1)]
    # A summed-area table of the outline over the bed and a margin around it, so
    # each spot's check is four lookups.
    pad = int(max(TOWER_CLEARANCES) // step) + 2
    i0, j0 = math.floor(bed.x0 / step) - pad, math.floor(bed.y0 / step) - pad
    ni, nj = math.floor(bed.x1 / step) + pad - i0 + 1, math.floor(bed.y1 / step) + pad - j0 + 1
    sat = [[0] * (nj + 1) for _ in range(ni + 1)]
    for i in range(ni):
        row, prev = sat[i + 1], sat[i]
        run = 0
        for j in range(nj):
            run += (i + i0, j + j0) in outline
            row[j + 1] = prev[j + 1] + run

    def hits(r: tuple) -> bool:
        a0 = min(max(math.floor(r[0] / step) - i0, 0), ni)
        a1 = min(max(math.floor((r[2] - 1e-9) / step) - i0 + 1, 0), ni)
        c0 = min(max(math.floor(r[1] / step) - j0, 0), nj)
        c1 = min(max(math.floor((r[3] - 1e-9) / step) - j0 + 1, 0), nj)
        return sat[a1][c1] - sat[a0][c1] - sat[a1][c0] + sat[a0][c0] > 0

    for clear in TOWER_CLEARANCES:
        best = None
        for tx in xs:
            for ty in ys:
                body = (tx - b, ty - b, tx + tw + b, ty + td + b)
                near = (body[0] - EXCLUSION_GAP, body[1] - EXCLUSION_GAP, body[2] + EXCLUSION_GAP, body[3] + EXCLUSION_GAP)
                if any(verify.overlaps(near, r) for r in bed.exclusions):
                    continue
                if hits((body[0] - clear, body[1] - clear, body[2] + clear, body[3] + clear)):
                    continue
                edge = min(tx - bed.x0, ty - bed.y0, bed.x1 - tx - tw, bed.y1 - ty - td)
                away = math.hypot(tx + tw / 2 - center[0], ty + td / 2 - center[1])
                score = (round(edge, 3), -away)
                if best is None or score < best[0]:
                    best = (score, (tx, ty))
        if best is not None:
            return best[1]
    return None


# Prime tower widths tried, as multiples of the printer's own, when its own
# leaves no strip big enough: a wider tower is shallower (OrcaSlicer spreads the
# same purge over the width), so a short strip takes a wide one, a narrow strip
# a narrow one.
TOWER_WIDTHS = (1.0, 1.5, 2.0, 3.0, 4.0, 0.75, 0.5)
MIN_TOWER_WIDTH = 15.0  # mm; narrower than this, a tall tower gets wobbly


# The prime tower brim tried when the printer's is auto and its tower finds no room:
# the A1's own. Auto came to about 8 mm on Bambu Studio's H2 and P2S towers.
CROWDED_TOWER_BRIM = 3.0


def place_all(slug: str, plates: list[Plate], bed: Bed,
              cfg: dict) -> tuple[list[Placement], Optional[float], Optional[float]]:
    """Every plate placed, and the prime tower width -- and brim, if the printer's auto
    brim had to give way -- that leave each one room (one of each per file).

    `cfg` is the settings the file will have, which size the tower.
    """
    if bed.tower is None:
        return [place(slug, p, len(plates), bed) for p in plates], None, None
    widths = [bed.tower] + [bed.tower * f for f in TOWER_WIDTHS[1:] if MIN_TOWER_WIDTH <= bed.tower * f <= bed.width / 2]
    auto = verify._first(cfg.get("prime_tower_brim_width")) < 0
    first = None
    for brim in [None] + ([CROWDED_TOWER_BRIM] if auto else []):
        b = bed if brim is None else replace(bed, tower_brim=brim)
        for tw in widths:
            fw, fd, _brim = verify.tower_size(cfg, tw)
            placements = [place(slug, p, len(plates), b, fw, fd) for p in plates]
            if not any(pl.crowded for pl in placements if pl.fits):
                return placements, tw, brim
            first = first or (placements, tw, None)
    return first  # crowded at any width: the printer's own, and a warning


def tall_tower(slug: str, plates: list[Plate], cfg: dict, slicer: str) -> tuple[dict, bool]:
    """({setting: value} for a tall print's prime tower, whether its wider base had room).

    Over tall.TALL_PRINT_MM, with a tower: the heavier tower if every plate, placed with
    its wider base, finds room; else only the slower tower. Nothing for anything else.
    """
    if Bed.of(cfg).tower is None or max(p.size[2] for p in plates) <= tall.TALL_PRINT_MM:
        return {}, False
    heavy = tall.settings(cfg, tall.TOWER[slicer])
    heavy_cfg = {**cfg, **heavy}
    tried, _w, _b = place_all(slug, plates, Bed.of(heavy_cfg), heavy_cfg)
    if not any(pl.crowded or pl.excluded or not pl.fits for pl in tried):
        return heavy, True
    return tall.settings(cfg, tall.SLOWER[slicer]), False


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
    """(filaments in order, {old slot: new filament}), or exit refusing."""
    fils = parse_filaments(src)
    if not fils:
        raise Refusal(f"could not parse filaments from {src}")
    by_id = {int(f["id"]): f for f in fils}
    missing = [s for s in used if s not in by_id]
    if missing:
        raise Refusal(f"REFUSING {slug}: slots {missing} are painted or assigned but not in the palette")
    odd = sorted({by_id[s]["type"] for s in used} - set(BAMBU_TYPES))
    if odd:
        raise Refusal(f"REFUSING {slug}: filament type(s) {odd} -- printers.BAMBU_TYPES says what each Bambu "
                 f"type prints as; add it there with a preset per printer")
    return [by_id[s] for s in used], {old: new for new, old in enumerate(used, start=1)}


def orca_colour(c: str) -> str:
    """#RRGGBB, as OrcaSlicer writes it; an alpha other than FF is kept."""
    c = c.strip().upper()
    return c[:7] if len(c) == 9 and c.endswith("FF") else c


def flush_matrix(src_cfg: dict, used: list[int], base: dict) -> list[str]:
    """Flush volumes between the used colors: the source's own, Bambu's per-color values.

    Bambu Studio sizes them from the colors (black to bone white purges far more
    than the reverse); a dual-nozzle H2C/H2D keeps one matrix per nozzle, and
    the larger of the two is taken. The printer's own flush_multiplier still
    applies on top, as it would to the values OrcaSlicer computes from colors.
    Without a usable source matrix, the printer's default for every change.
    """
    n = len(src_cfg.get("filament_colour", []))
    m = [float(v) for v in src_cfg.get("flush_volumes_matrix", [])]
    k = len(used)
    if n and m and len(m) % (n * n) == 0:
        blocks = len(m) // (n * n)
        return [f"{max(m[b * n * n + (i - 1) * n + (j - 1)] for b in range(blocks)):g}" if i != j else "0"
                for i in used for j in used]
    off = next((v for v in base.get("flush_volumes_matrix", []) if v != "0"), "280")
    return [off if i != j else "0" for i in range(k) for j in range(k)]


def in_orcaslicer_terms(carried: dict, base: dict) -> dict:
    """The carried settings with each value OrcaSlicer names differently under its name (ORCA_NAMES)."""
    named = {k: ORCA_NAMES.get((k, v), v) for k, v in carried.items()}
    return {k: v for k, v in named.items() if v != base.get(k)}


def absolute_line_widths(s: dict) -> set:
    """Write every line width given in % of the nozzle in mm; returns the keys changed."""
    nozzles = {float(n) for n in s.get("nozzle_diameter") or []}
    if len(nozzles) != 1:
        return set()  # a % of which nozzle: the slicer's to work out
    nozzle = nozzles.pop()
    changed = set()
    for k, v in s.items():
        if k.endswith("line_width") and isinstance(v, str) and v.endswith("%"):
            s[k] = f"{float(v[:-1]) * nozzle / 100:g}"
            changed.add(k)
    return changed


def exclusion_area(rects: list[tuple], corner: tuple) -> list[str]:
    """bed_exclude_area for these rectangles that OrcaSlicer and Anycubic Slicer Next read alike.

    Read four points at a time, the connecting points are lines with no area;
    read as one polygon, each rectangle is a loop reached from the bed's corner
    and left by the same zero-width edges.
    """
    x0, y0 = corner
    pts = []
    for a, b, c, d in rects:
        pts += [(x0, y0), (a, y0), (a, y0), (a, y0),   # along the bed's front edge
                (a, y0), (a, b), (a, b), (a, b),       # up to the rectangle's corner
                (a, b), (c, b), (c, d), (a, d),        # the rectangle
                (a, b), (a, y0), (a, y0), (a, y0),     # and back the same way
                (a, y0), (x0, y0), (x0, y0), (x0, y0)]
    return [f"{x:g}x{y:g}" for x, y in pts]


def guard_ace(gcode: str) -> Optional[str]:
    """gcode with ACE_GUARD nested in each unguarded ACE_BLOCK, or None if there's none."""
    lines = gcode.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.strip() == ACE_BLOCK
              and not (i + 1 < len(lines) and lines[i + 1].strip() == ACE_GUARD)]
    for start in reversed(starts):
        depth = 0
        for end in range(start, len(lines)):
            for m in MACRO.finditer(lines[end]):
                body = m.group(1)
                if re.match(r"\s*if\b", body) and not re.search(r"\bthen\b", body):  # not an inline if..then..endif
                    depth += 1
                elif re.fullmatch(r"\s*endif\s*", body):
                    depth -= 1
            if depth == 0:
                break
        else:
            raise Refusal("an ACE block in change_filament_gcode has no endif")
        lines.insert(end, "{ endif }")
        lines.insert(start + 1, ACE_GUARD)
    return "\n".join(lines) if starts else None


def compose(key: str, meta: dict, tsettings: dict, src_cfg: dict, ordered: list[dict], used: list[int],
            mapping: dict, carried: dict, placements: list[Placement], bed: Bed,
            tower_width: Optional[float], tower_brim: Optional[float] = None) -> dict:
    """The file's project_settings.config for printer `key`."""
    base = tsettings["PLA"]
    types = [BAMBU_TYPES[f["type"]] for f in ordered]
    missing = sorted(set(types) - set(tsettings))
    if missing:
        raise Refusal(f"REFUSING: the {PRINTERS[key].label} has no preset here for {missing} (printers.py)")
    s = copy.deepcopy(base)

    # One column per filament, each from the template its slicer wrote for its
    # type: one value, or on Bambu Studio's dual-nozzle printers one per nozzle
    # variant -- the entries filament_self_index ties to their filament.
    for k in meta["filament_keys"]:
        if k not in base:
            continue
        col = []
        for t in types:
            v = tsettings[t].get(k)
            if not isinstance(v, list) or not v:
                raise Refusal(f"the {key} template for {t} has {v!r} for {k}, not a list -- run slicerbridge templates --refresh")
            col.extend(v)
        s[k] = col
    # A Bambu H2D/H2C project names the hotends on each extruder ("Standard#6": a rack of
    # six); Bambu Studio fills it in from the connected printer, and won't slice without
    # it ("No valid nozzle found"). The stock H2D has one per extruder, the least an
    # H2C can have: that, until the downloader's printer syncs its own.
    if base.get("extruder_nozzle_stats") == [] and isinstance(base.get("nozzle_volume_type"), list):
        s["extruder_nozzle_stats"] = [f"{t}#1" for t in base["nozzle_volume_type"]]
        s["extruder_nozzle_stats_new"] = list(s["extruder_nozzle_stats"])  # Bambu Studio saves both
    if isinstance(base.get("filament_self_index"), list):
        s["filament_self_index"] = [str(i) for i, t in enumerate(types, start=1)
                                    for _ in tsettings[t]["filament_self_index"]]
    s["filament_colour"] = [orca_colour(f["color"]) for f in ordered]
    # print preset, one per filament, printer preset
    for k in ("inherits_group", "different_settings_to_system"):
        if isinstance(base.get(k), list) and len(base[k]) == 3:
            s[k] = [base[k][0], *(tsettings[t][k][1] for t in types), base[k][2]]
    s["flush_volumes_matrix"] = flush_matrix(src_cfg, used, base)
    vector = base.get("flush_volumes_vector") or ["140"]
    s["flush_volumes_vector"] = [vector[0]] * (2 * len(types))
    s["curr_bed_type"] = templates_build.bed_type_for(key, base, src_cfg)

    # Filament numbers set project-wide (support in one color, say). 0 is the default.
    for k, v in src_cfg.items():
        if k.endswith("_filament") and k in base and isinstance(v, str) and v.isdigit() and v != "0":
            n = int(v)
            if n not in mapping:
                raise Refusal(f"REFUSING: {k} is slot {n}, which the model doesn't otherwise use")
            s[k] = str(mapping[n])

    s.update(carried)
    changed = set(carried)
    for k, v in SLICER_WORKAROUNDS.items():
        if k in s and s[k] != v:
            s[k] = v
            changed.add(k)
    changed |= absolute_line_widths(s)
    machine_changed = set()
    rects = verify.exclusion_rects(s)
    if rects != verify.exclusion_rects(s, quads=True):
        pts = [tuple(float(c) for c in p.split("x")) for p in s["printable_area"]]
        s["bed_exclude_area"] = exclusion_area(rects, (min(p[0] for p in pts), min(p[1] for p in pts)))
        machine_changed.add("bed_exclude_area")
    if isinstance(s.get("change_filament_gcode"), str):
        guarded = guard_ace(s["change_filament_gcode"])
        if guarded:
            s["change_filament_gcode"] = guarded
            machine_changed.add("change_filament_gcode")
    if machine_changed:
        marked = s["different_settings_to_system"]
        marked[-1] = ";".join(sorted({k for k in marked[-1].split(";") if k} | machine_changed))
    # One prime tower position per plate, in that plate's own coordinates, and
    # a width that leaves each plate room for it.
    if tower_width is not None and abs(tower_width - bed.tower) > 1e-6:
        s["prime_tower_width"] = f"{tower_width:g}"
        changed.add("prime_tower_width")
    if tower_brim is not None:
        s["prime_tower_brim_width"] = f"{tower_brim:g}"
        changed.add("prime_tower_brim_width")
    # The tower is placed unturned: the Creality Hi's preset turns it 90 degrees, which
    # ran its 60 mm off the back of the bed from the spot left for it.
    if bed.tower is not None and float(s.get("wipe_tower_rotation_angle") or 0) % 360:
        s["wipe_tower_rotation_angle"] = "0"
        changed.add("wipe_tower_rotation_angle")
    if changed:
        s["different_settings_to_system"][0] = ";".join(sorted(changed))
    if bed.tower is not None:
        s["wipe_tower_x"] = [f"{pl.tower[0]:.3f}" for pl in placements]
        s["wipe_tower_y"] = [f"{pl.tower[1]:.3f}" for pl in placements]
    return s


READ_BACK_LIMIT = 180  # seconds


def read_back(key: str, settings: dict) -> tuple[dict, list[str]]:
    """What OrcaSlicer makes of these settings: ({key: value it read back}, [load errors]).

    The settings go into the printer's 20 mm cube template; OrcaSlicer opens it and
    saves it again. A setting it doesn't know is gone, a value it doesn't take is
    its default, and a value out of range fails the load and is named.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        probe = tmp / "probe.3mf"
        with zipfile.ZipFile(TEMPLATES / key / "PLA.3mf") as zin, \
             zipfile.ZipFile(probe, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "Metadata/project_settings.config":
                    data = json.dumps(settings, indent=4).encode()
                zout.writestr(item, data)
        (tmp / "out").mkdir()
        slicer = SLICERS[PRINTERS[key].slicer]
        # A read-back takes seconds. Bambu Studio's command line now and then never
        # exits (Windows, 2.8.2): one that hangs gets one more try.
        for attempt in range(2):
            try:
                run = run_slicer([slicer.binary, "--outputdir", tmp / "out", "--export-3mf", "back.3mf", probe],
                                 timeout=READ_BACK_LIMIT)
                break
            except subprocess.TimeoutExpired:
                if attempt:
                    raise Refusal(f"{slicer.name} hung reading the {PRINTERS[key].label} settings back, twice "
                                  f"({READ_BACK_LIMIT // 60} min each)") from None
        back = tmp / "out" / "back.3mf"
        if run.returncode != 0 or not back.exists():
            log = (run.stdout + run.stderr).splitlines()
            errors = [ln.strip() for ln in log if re.match(r"^\s*[a-z_0-9]+: .* not in range", ln)]
            errors += [m.group(1) for ln in log for m in [re.search(r"Invalid value provided for parameter (\S+?):", ln)] if m]
            return {}, errors or [f"exit {run.returncode}: " + " | ".join(log[-3:])]
        with zipfile.ZipFile(back) as z:
            return json.loads(z.read("Metadata/project_settings.config")), []


def settle(key: str, settings: dict, carried: dict, base: dict) -> tuple[dict, dict]:
    """(settings OrcaSlicer reads back exactly, {carried key: why it was dropped}).

    A carried value OrcaSlicer won't take reverts to the printer's; anything else
    that doesn't read back is this script's mistake and a refusal.
    """
    dropped = {}
    slicer = SLICERS[PRINTERS[key].slicer].name
    for _ in range(4):
        back, errors = read_back(key, settings)
        bad = {}
        for e in errors:
            k = e.split(":")[0].strip()
            if k in carried and k not in dropped:
                bad[k] = f"OrcaSlicer won't take {carried[k]!r} ({e.split(':', 1)[-1].strip()})"
        if errors and not bad:
            raise Refusal(f"{SLICERS[PRINTERS[key].slicer].name} won't load the {PRINTERS[key].label} settings: {errors}")
        if not errors:
            for k in sorted(set(settings) | set(back)):
                if k in READ_BACK_IGNORED or settings.get(k) == back.get(k):
                    continue
                if k in carried and k not in dropped:
                    bad[k] = (f"{slicer} read {carried[k]!r} back as {back[k]!r}" if k in back
                              else f"{slicer} dropped it")
                elif k == "different_settings_to_system":
                    continue  # follows the carried keys; settled with them
                elif k in SLICER_OWNED or (isinstance(settings.get(k), list) and isinstance(back.get(k), list)
                                           and back[k][:len(settings[k])] == settings[k]) or (
                        isinstance(settings.get(k), str) and isinstance(back.get(k), str)
                        and "".join(settings[k].split()) == "".join(back[k].split())):
                    # The slicer pads a list to its own length when it loads one, and on a
                    # toolchanger keeps its own flush volumes per head (the Creator 5's come
                    # back 111 mm3 lower, in four blocks), and respaces a text value (the ZR Ultra S's
                    # thumbnails come back "48x48/PNG, 300x300/PNG"). Write what it would.
                    settings[k] = back[k]
                    continue
                else:
                    raise Refusal(f"{slicer} reads {k} back as {back.get(k)!r}, written {settings.get(k)!r} "
                             f"({PRINTERS[key].label}) -- a bug in compose()")
            if not bad:
                return settings, dropped
        for k, why in bad.items():
            dropped[k] = why
            settings[k] = base[k]
        kept = [k for k in settings["different_settings_to_system"][0].split(";") if k and k not in dropped]
        settings["different_settings_to_system"][0] = ";".join(kept)
    raise Refusal(f"settings for the {PRINTERS[key].label} never settled: {dropped}")


def remap_model_settings(text: str, mapping: dict, used: list[int]) -> str:
    def ext(m: re.Match) -> str:
        n = int(m.group(2))
        return f"{m.group(1)}{mapping.get(n, n) if n else 0}{m.group(3)}"

    text = FILAMENT_META.sub(ext, text)
    # Per-filament plate lists. filament_maps is the H2C/H2D's nozzle choice; a
    # single-nozzle printer has one nozzle, and OrcaSlicer writes a 1 per filament.
    text = re.sub(r'(key="filament_maps" value=")[^"]*(")',
                  lambda m: m.group(1) + " ".join(["1"] * len(used)) + m.group(2), text)

    def volume_maps(m: re.Match) -> str:
        vals = m.group(2).split()
        return m.group(1) + " ".join(vals[s - 1] if s <= len(vals) else "0" for s in used) + m.group(3)

    return re.sub(r'(key="filament_volume_maps" value=")([^"]*)(")', volume_maps, text)


# -------------------------------------------------------------- convert ----

def unhide(path: Path) -> None:
    """Clear macOS's hidden flag on a finished file.

    iCloud's Documents sync flags any dot-named file under ~/Documents as hidden
    within seconds, and the flag survives a rename. Temp files here aren't
    dot-named; this is the guard.
    """
    flags = getattr(os.stat(path), "st_flags", 0)
    if flags & stat.UF_HIDDEN:
        os.chflags(path, flags & ~stat.UF_HIDDEN)


def ours(path: Path) -> bool:
    """Whether SlicerBridge wrote this file (its zip comment), so it may be replaced."""
    with zipfile.ZipFile(path) as z:
        return z.comment.startswith((STAMP, *LEGACY_STAMPS))


def withdraw(path: Path, why: str) -> None:
    """Remove SlicerBridge's earlier file for a model/printer that no longer passes."""
    if path.exists() and ours(path):
        path.unlink()
        print(f"       removed the earlier {path.name}: {why}")


@dataclass
class Model:
    """What one source is, worked out once for every printer."""
    slug: str
    src: Path
    used: list
    plates: list
    ordered: list
    mapping: dict
    src_cfg: dict
    recoded: dict = field(default_factory=dict)


def load_model(src: Path, slug: str) -> Model:
    """Read a Bambu project once: `slug` is the model's display name, used in messages and file names."""
    if not src.exists():
        raise Refusal(f"no such file: {src}")
    used, plates = analyze(slug, src)
    ordered, mapping = plan(slug, src, used)
    with zipfile.ZipFile(src) as z:
        src_cfg = json.loads(z.read("Metadata/project_settings.config").decode())
    return Model(slug, src, used, plates, ordered, mapping, src_cfg)


def write(m: Model, key: str, slice_it: bool, out: Path, prefix: str = "", suffix: str = "") -> bool:
    """Convert one model for one printer; True when the file passed and landed in `out`."""
    printer = PRINTERS[key]
    final = out_path(out, m.slug, key, prefix, suffix)
    if len(m.used) > printer.tools:
        by_id = {int(f["id"]): f for f in parse_filaments(m.src)}
        lines = "\n".join(f"       {s:>2}  {by_id[s]['type']:8} {by_id[s]['color']}" for s in m.used)
        raise Refusal(
            f"REFUSING {m.slug} for the {printer.label}: it uses {len(m.used)} filament slots; the "
            f"{printer.label} holds {printer.tools}.\n\n{lines}\n\n"
            f"   Fitting these onto {printer.tools} means painting two colors with one filament.\n"
            f"   That is a design decision -- which colors merge changes the piece. Do it in\n"
            f"   your slicer: merge deliberately, re-paint, save, and run this again.\n")
    meta, tsettings = templates_build.load(key)
    base = tsettings["PLA"]
    bed = Bed.of(base)
    with zipfile.ZipFile(TEMPLATES / key / "PLA.3mf") as z:
        application = re.search(r'<metadata name="Application">([^<]*)</metadata>', z.read(ROOT_MODEL).decode())
        slice_info = z.read("Metadata/slice_info.config")
    if not application:
        raise Refusal(f"no Application stamp in the {key} template -- run slicerbridge templates --refresh")

    carried, skipped = carry_print_settings(m.src_cfg, base)
    if printer.slicer == "orca":
        carried = in_orcaslicer_terms(carried, base)
    # The prime tower's likely size, from the settings the file will have.
    likely = {**base, **carried, "filament_colour": [f["color"] for f in m.ordered],
              "flush_volumes_matrix": flush_matrix(m.src_cfg, m.used, base)}
    bed = Bed.of(likely)  # the carried brim and skirt decide the room kept at the edge
    # A tall print gets the heavier prime tower (tall.py), placed with its wider base,
    # if every plate has room for it; if not, only the slower tower. It's read back
    # with the carried settings: a value the slicer won't take keeps the printer's.
    tallest = max(p.size[2] for p in m.plates)
    tower_extra, roomy = tall_tower(m.slug, m.plates, likely, printer.slicer)
    if tower_extra:
        likely = {**likely, **tower_extra}
        bed = Bed.of(likely)
        carried = {**carried, **tower_extra}
    placements, tower_width, tower_brim = place_all(m.slug, m.plates, bed, likely)
    settings = compose(key, meta, tsettings, m.src_cfg, m.ordered, m.used, m.mapping, carried, placements, bed,
                       tower_width, tower_brim)
    settings, dropped = settle(key, settings, carried, base)

    transforms = {oid: tr for pl in placements for oid, tr in pl.transforms.items()}
    identity = all(old == new for old, new in m.mapping.items())
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"partial - {final.name}")  # not dot-named: macOS would flag it hidden

    def recode(match: re.Match) -> bytes:
        code = match.group(2)
        if code not in m.recoded:
            m.recoded[code] = transform_states(code.decode(), m.mapping).encode()
        return match.group(1) + m.recoded[code] + match.group(3)

    with zipfile.ZipFile(m.src) as zin, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.comment = STAMP
        model_settings = remap_model_settings(
            zin.read("Metadata/model_settings.config").decode(), m.mapping, m.used).encode()
        root_model = retransform_items(zin.read(ROOT_MODEL).decode(), transforms)
        root_model = re.sub(r'(<metadata name="Application">)[^<]*(</metadata>)',
                            lambda x: x.group(1) + application.group(1) + x.group(2), root_model).encode()
        for item in zin.infolist():
            safe = posixpath.normpath(item.filename).lstrip("/")
            if safe.startswith("..") or BAMBU_LEFTOVERS.match(item.filename):
                continue  # zip-slip guard; Bambu's slice leftovers
            if item.filename == "Metadata/project_settings.config":
                zout.writestr(item, json.dumps(settings, indent=4, ensure_ascii=False).encode())
            elif item.filename == "Metadata/slice_info.config":
                zout.writestr(item, slice_info)  # OrcaSlicer's unsliced stub
            elif item.filename == "Metadata/model_settings.config":
                zout.writestr(item, model_settings)
            elif item.filename == ROOT_MODEL:
                zout.writestr(item, root_model if identity else PAINT.sub(recode, root_model))
            elif item.filename == "Metadata/layer_config_ranges.xml" and not identity:
                xml = zin.read(item.filename).decode()
                zout.writestr(item, LAYER_RANGE_EXTRUDER.sub(
                    lambda x: f"{x.group(1)}{m.mapping.get(int(x.group(2)), int(x.group(2)))}{x.group(3)}", xml))
            elif item.filename.lower().endswith(".model") and not identity:
                zout.writestr(item, PAINT.sub(recode, zin.read(item.filename)))
            else:
                zout.writestr(item, zin.read(item.filename))

    # The checks, before anything lands in out/.
    info, problems, warnings = verify.audit(tmp, m.src, key)
    if slice_it and not problems:
        si, sp, sw = verify.slice_check(tmp, key)
        info, problems, warnings = info + si, problems + sp, warnings + sw
    if problems:
        tmp.unlink()
        print(f"  FAIL {m.slug:12} {printer.label}")
        for p in problems:
            print(f"       !! {p}")
        withdraw(final, "it no longer passes")
        return False

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
    print(f"  ok   {m.slug:12} {printer.label:18} -> {final.parent.name}/{final.name}  ({mb:.0f} MB)"
          + ("  sliced" if slice_it else ""))
    for p, pl in zip(m.plates, placements):
        where = f"plate {p.number}: " if len(m.plates) > 1 else ""
        notes = [f"{pl.size[0]:.1f} x {pl.size[1]:.1f} x {pl.size[2]:.1f} mm"]
        if pl.rotated:
            notes.append("turned 90 deg")
        if not pl.fits:
            notes.append(f"⚠ bigger than the bed: centered at full size, fits at "
                         f"{math.floor(pl.max_scale * 1000) / 10:g}% or less")
        elif pl.crowded:
            notes.append("⚠ no strip beside it big enough for the prime tower: move the tower or scale it "
                         "down in the slicer")
        if pl.excluded:
            notes.append("⚠ no spot clear of the bed's exclusion area")
        print(f"       {where}" + ", ".join(notes))
    if dropped:
        print("       kept the printer's own " + "; ".join(f"{k} ({why})" for k, why in dropped.items()))
    if tallest > tall.TALL_PRINT_MM and bed.tower is not None:
        got = {k: v for k, v in tower_extra.items() if k not in dropped}
        if roomy:
            print(f"       {tallest:.0f} mm tall, over {tall.TALL_PRINT_MM:g}: heavier prime tower"
                  + (f" ({tall.describe(got)})" if got else " (the printer's own already is)"))
        else:
            print(f"       ⚠ {tallest:.0f} mm tall, over {tall.TALL_PRINT_MM:g}, but there's no room for the heavier "
                  f"tower's base: slower tower only" + (f" ({tall.describe(got)})" if got else "")
                  + ". Slice it, and if the model leaves room, give the tower a bigger brim and longer ribs")
    if tower_width is not None and abs(tower_width - bed.tower) > 1e-6:
        print(f"       prime tower {tower_width:g} mm wide (the printer's is {bed.tower:g}) to fit the space left")
    if tower_brim is not None:
        print(f"       prime tower brim {tower_brim:g} mm (the printer's is auto) to fit the space left")
    worked_around = [k for k, v in SLICER_WORKAROUNDS.items() if base.get(k) not in (None, v)]
    if worked_around:
        print(f"       {', '.join(worked_around)} off: {SLICERS[printer.slicer].name} hangs on it")
    crowded = any(pl.crowded for pl in placements if pl.fits)
    for w in warnings:
        if "bigger than" not in w and not (crowded and "may touch" in w):  # said above
            print(f"       ⚠ {w}")
    return True


def inspect(src: Path, keys: list[str]) -> list[str]:
    """Lines describing a source: its palette, the slots it uses, each plate's size and fit on each bed."""
    lines = []
    by_id = {int(f["id"]): f for f in parse_filaments(src)}
    used, plates = analyze(src.stem, src)
    lines.append(f"palette {len(by_id)}, uses {len(used)}: " + ", ".join(f"{s}->{i}" for i, s in enumerate(used, start=1)))
    for s in used:
        f = by_id.get(s, {"type": "?", "color": "?"})
        lines.append(f"    slot {s:>2}  {f['type']:8} {f['color']}")
    beds = {}
    for k in keys:
        try:
            beds[k] = Bed.of(templates_build.load(k, install=False)[1]["PLA"])  # inspecting installs nothing
        except Refusal as e:
            lines.append(f"    {PRINTERS[k].label}: no template ({str(e).splitlines()[0]})")
    for p in plates:
        where = f"plate {p.number}" if len(plates) > 1 else "size"
        fits = []
        for k, bed in beds.items():
            r = orientation(p.size, bed)
            fits.append(f"{PRINTERS[k].label}: " + ("fits" if r is False else "turned 90" if r else
                        f"<= {math.floor(fit_scale(p.size, bed) * 1000) / 10:g}%"))
        lines.append(f"    {where}: {p.size[0]:.1f} x {p.size[1]:.1f} x {p.size[2]:.1f} mm"
                     + ("  -- " + " | ".join(fits) if fits else ""))
    return lines


def convert(src: Path, name: str, keys: list[str], out: Path, prefix: str = "", slice_it: bool = False,
            suffix: str = "") -> tuple[int, int]:
    """Write `name`'s file for each printer key into out/<name>/; returns (written, refused or failed)."""
    try:
        m = load_model(src, name)
    except Refusal as e:  # a refused model writes nothing, and takes back what it wrote before
        print(f"  SKIP {name}\n!! {e}")
        for k in keys:
            withdraw(out_path(out, name, k, prefix, suffix), "the model is refused")
        return 0, len(keys)
    made = failed = 0
    for k in keys:
        try:
            ok = write(m, k, slice_it, out, prefix, suffix)
        except Refusal as e:
            print(f"  SKIP {name} {PRINTERS[k].label}\n!! {e}")
            withdraw(out_path(out, name, k, prefix, suffix), "it was refused")
            ok = False
        except Exception as e:  # a bug or a slicer gone wrong: this printer fails, the rest still get theirs
            print(f"  FAIL {name} {PRINTERS[k].label}\n!! {type(e).__name__}: {e}")
            withdraw(out_path(out, name, k, prefix, suffix), "it failed")
            ok = False
        made, failed = made + ok, failed + (not ok)
    del m
    gc.collect()
    return made, failed

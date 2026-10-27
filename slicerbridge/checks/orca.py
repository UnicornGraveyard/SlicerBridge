"""
checks/orca.py -- the audit every orca-engine file passes before it's written.

    slicerbridge verify out/"My Model" --source my-model.3mf           # every file
    slicerbridge verify out/"My Model" --source my-model.3mf --slice   # and slice each one (minutes per file)

Each file is checked against the printer its name says and the Bambu source it
was made from:

  1. Not a white blob: the palette isn't all white.
  2. Printer: the printer and print presets are that printer's, from printers.PRINTERS.
  3. Colors: one filament per slot the source paints or assigns, at most the
     printer's 4, each a preset of that printer for the source's filament type.
  4. Paint present.
  5. Palette: filament k is the color of the k-th used source slot.
  6. Every paint state and extruder number is a filament the file has, and the
     part/object extruders are the source's, mapped.
  7. Painted faces in == out.
  8. Paint leaves per filament in == out after the mapping -- the proof the
     colors landed on the right filaments.
  9. On the bed: every printed vertex inside its own plate (OrcaSlicer lays
     plates out 1.2 beds apart) and under the max height, clear of the bed's
     exclusion area. A model bigger than the bed is converted at full size and
     gets a warning with the scale that would fit. A prime tower that may touch
     the model's footprint gets a warning too.
 10. Version stamp: OrcaSlicer's own, from the printer's template.
 11. Size unchanged: each plate measures as in the source, to 0.01 mm.
 12. Settings complete: every per-filament setting has one value per filament,
     the flush matrix is filaments x filaments, the bed type is the one
     OrcaSlicer's app picks for that printer, and Bambu's slice leftovers
     (plate_N.json, filament_sequence.json, G-code) are gone. And it reads the
     same in the brands' own slicers: line widths in mm, an exclusion area
     Anycubic Slicer Next reads as OrcaSlicer does, the Kobra X's ACE block guarded.

With --slice, also:

 13. OrcaSlicer slices every plate; each multicolor plate changes filament with
     tool changes the printer makes itself (no PAUSE or M600); the prime tower
     OrcaSlicer actually printed stays clear of the model's footprint.

The paint decoder is checks/u1.py's, written from the format: the conversion
engine (Panda2Prusa) never checks its own work. Standard library only.
"""
from __future__ import annotations

import functools
import json
import math
import re
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .. import templates as templates_build
from ..printers import BAMBU_TYPES, PRINTERS, SLICERS, TEMPLATES, Slicer
from . import u1


EPS = 1e-4
SLICE_LIMIT = 1200  # seconds; a 2-plate, 4-color model slices in 1-2 minutes per printer
CRASHES = {-6, -11, 134, 139}  # SIGABRT and SIGSEGV, as subprocess or as the shell reports them
VERSION_REFUSED = 232  # CLI_FILE_VERSION_NOT_SUPPORTED (-24) as an exit status
# CLI_GCODE_PATH_CONFLICTS (-101), as Windows and as a shell report it: the slicer's
# own word that the tower hit the model, with or without a line in the log saying so.
CONFLICT_EXITS = {-101, 2**32 - 101, 256 - 101}
# CLI_GCODE_PATH_IN_UNPRINTABLE_AREA (-102): a brim, skirt or tower past the bed's
# printable area, as a model too big for its bed with them would be.
UNPRINTABLE_EXITS = {-102, 2**32 - 102, 256 - 102}
AUTO_MODEL_BRIM = 12.0  # mm; see outline_reach()
# The Anycubic Kobra X's ACE Pro block in its color-change G-code, and the guard
# Anycubic Slicer Next needs nested inside it (engines/orca.py, guard_ace).
ACE_BLOCK = "{ if magic_mask_with_box == -1392 }"
ACE_GUARD = ("{ if size(filament_type) > 0 && size(filament_type) == size(ace_t_box_vector) "
             "&& size(filament_type) == size(ace_t_slot_vector) }")
WHITE = {"#FFFFFF", "#FFFFFFFF"}
BAMBU_LEFTOVERS = re.compile(r"^Metadata/(plate_\d+\.json|filament_sequence\.json|plate_\d+\.gcode(\.md5)?)$")
# Settings OrcaSlicer keeps one value per plate, or adds on its own when it saves.
PER_PLATE = {"wipe_tower_x", "wipe_tower_y"}


def rgba(c: str) -> str:
    c = c.strip().upper()
    return c + "FF" if len(c) == 7 else c


def exclusion_rects(settings: dict, quads: bool = False) -> list[tuple]:
    """The bed's exclusion area as rectangles (x0, y0, x1, y1).

    OrcaSlicer reads bed_exclude_area as one polygon, filled even-odd. It may
    trace two corner notches joined by zero-width edges along the border (the
    QIDI Plus4's), or a frame -- the bed's outline and, inside it, a 2 mm smaller
    one (the Kobra 3's). Anycubic Slicer Next reads it as separate polygons of 4
    points each (quads=True): to it that frame is the whole bed. Filling it on a
    1 mm grid and cutting the filled cells into runs, merged down the rows where
    they repeat, gives the notches and the frame's four strips, and nothing for
    zero-width edges.
    """
    pts = []
    for s in settings.get("bed_exclude_area") or []:
        for p in str(s).split(","):
            if "x" in p:
                x, y = p.split("x")
                pts.append((float(x), float(y)))
    if len(set(pts)) < 3:
        return []
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    gx0, gy0 = math.floor(min(xs)), math.floor(min(ys))
    nx, ny = math.ceil(max(xs)) - gx0, math.ceil(max(ys)) - gy0
    polygons = [pts[i:i + 4] for i in range(0, len(pts) - 3, 4)] if quads else [pts]
    edge_lists = [list(zip(p, p[1:] + p[:1])) for p in polygons]

    def inside(x: float, y: float) -> bool:
        for edges in edge_lists:
            hit = False
            for (ax, ay), (bx, by) in edges:
                if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                    hit = not hit
            if hit:
                return True
        return False

    rects, open_ = [], {}  # open_: run (x0, x1) -> first row it started on
    for j in range(ny + 1):
        runs, start = set(), None
        for i in range(nx + 1):
            filled = j < ny and i < nx and inside(gx0 + i + 0.5, gy0 + j + 0.5)
            if filled and start is None:
                start = i
            elif not filled and start is not None:
                runs.add((start, i))
                start = None
        for run in list(open_):
            if run not in runs:  # the run ends here: close its rectangle
                rects.append((gx0 + run[0], gy0 + open_.pop(run), gx0 + run[1], gy0 + j))
        for run in runs:
            open_.setdefault(run, j)
    return sorted(rects)


def overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _first(v) -> float:
    return float((v[0] if isinstance(v, list) else v) or 0)


def tower_area(cfg: dict) -> float:
    """mm^2 of bed the prime tower's busiest layer needs, as OrcaSlicer's WipeTower sizes it.

    Each filament change lays its volume down in rows the tower's width long and
    one layer high, so a change adds volume / (width x layer height) of depth,
    and a layer has at most one change per filament. With purge_in_prime_tower
    off (every printer here that prints a tower), a change primes prime_volume
    into the tower and purges the rest into the printer's chute; on, the whole
    flush goes in. A quarter on top for the rows' spacing. The measured K2 Plus
    Gatefang tower, 4 colors at 0.16 mm: about 40 x 24 mm against 40 x 35 here.

    A whole flush on every filament's change every layer is far past what a model's
    busiest layer has: IronJaw's K2 Pro tower (purging, 4 colors, 0.16 mm, 270 mm^3
    at most) printed about 45 x 60 mm against 40 x 211 that way. A flush tower counts
    a change for every three filaments, at least one, which puts it at 40 x 70.
    """
    k = len(cfg.get("filament_colour") or []) or 1
    lh = _first(cfg.get("layer_height")) or 0.2
    if str(cfg.get("purge_in_prime_tower")) == "1":
        v = max(float(x) for x in cfg.get("flush_volumes_matrix") or ["0"]) * (_first(cfg.get("flush_multiplier")) or 1)
        return 1.25 * max(1.0, k / 3) * v / lh
    v = _first(cfg.get("prime_volume")) or 45
    return 1.25 * k * v / lh


def outline_reach(cfg: dict) -> float:
    """How far past the model its brim and skirt print, in mm.

    A brim set to auto has no width until slicing, and grows with the model's
    height: OrcaSlicer gave IronJaw (127 mm tall) 9.8 to 11.7 mm on the QIDI Plus4,
    where 5 mm had been assumed and the skirt ran off the bed. Each skirt loop is
    about half a millimetre.
    """
    brim_type = str(cfg.get("brim_type") or "no_brim")
    brim = _first(cfg.get("brim_width")) if brim_type != "no_brim" else 0.0
    if brim_type == "auto_brim" and brim <= 0:
        brim = AUTO_MODEL_BRIM
    loops = int(_first(cfg.get("skirt_loops")))
    skirt = _first(cfg.get("skirt_distance")) + 0.5 * loops if loops > 0 else 0.0
    return brim + skirt


def reachable_area(cfg: dict) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of the bed every nozzle reaches.

    A two-nozzle printer (H2D, H2C) can't reach the whole bed with either nozzle,
    and filaments are mapped to nozzles at slicing ("Auto For Flush"): anything
    printed must sit where both reach, or Bambu Studio finds G-code in an
    unprintable area. A one-nozzle printer reaches its whole printable_area.
    """
    pts = [tuple(float(c) for c in p.split("x")) for p in cfg["printable_area"]]
    x0, y0, x1, y1 = min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)
    for area in cfg.get("extruder_printable_area") or []:
        reach = [tuple(float(c) for c in p.split("x")) for p in str(area).split(",") if "x" in p]
        if reach:
            x0, y0 = max(x0, min(p[0] for p in reach)), max(y0, min(p[1] for p in reach))
            x1, y1 = min(x1, max(p[0] for p in reach)), min(y1, max(p[1] for p in reach))
    return x0, y0, x1, y1


# Bambu Studio's auto prime tower brim (prime_tower_brim_width -1), and what its rib
# walls add to a tower, across and deep. Measured on IronJaw (4 colors, 0.16 mm, a
# tower_area of 1,405 mm^2, so 37.5 mm a side): an A1 tower with a 3 mm brim printed
# 42.1 x 42.8 mm inside its brim; an H2C one on auto, 45.5 x 40.9 mm inside a brim
# that started 8.2 mm before its corner.
AUTO_TOWER_BRIM = 8.0
RIB_EXTRA = (8.0, 5.5)
# A plain tower's walls and brim lines past its width and depth: IronJaw's Kobra 3
# tower (45 mm, 5 mm brim) printed 58.5 x 44.2 mm against 55 x 41.2.
WALL_EXTRA = 4.0


def tower_size(cfg: dict, width: float | None = None) -> tuple[float, float, float]:
    """(width, depth, brim) of the prime tower as the slicer prints it, `width` wide if given.

    A tower is laid in rows the width long, so its depth is tower_area / width. With
    rib walls (Bambu Studio's default) it's built about square instead, whatever its
    width: sqrt(tower_area) a side, plus the ribs. A brim of -1 is auto.
    """
    w = width or _first(cfg.get("prime_tower_width")) or 35
    b = _first(cfg.get("prime_tower_brim_width"))
    b = AUTO_TOWER_BRIM if b < 0 else b
    area = tower_area(cfg)
    if str(cfg.get("prime_tower_rib_wall")) == "1":
        side = math.sqrt(area)
        return side + RIB_EXTRA[0], side + RIB_EXTRA[1], b
    return w + WALL_EXTRA, area / w + WALL_EXTRA, b


# The model's outline seen from above, as the cells of a CELL mm grid it covers.
# A bounding box says a 245 x 198 mm relief leaves no room on a 256 mm bed for a
# prime tower; its outline leaves a corner beside the head, where the designer put
# the tower in the source.
CELL = 2.0


def _in_triangle(px: float, py: float, a: tuple, b: tuple, c: tuple) -> bool:
    d1 = (px - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (py - b[1])
    d2 = (px - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (py - c[1])
    d3 = (px - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (py - a[1])
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def add_outline(cells: set, xs: list, ys: list, tris) -> None:
    """Add the CELL grid cells triangles (vertex indices into xs, ys) cover, seen from above.

    A small triangle marks its whole bounding box of cells; a big one the cells whose
    centers it covers and those its edges cross, so a low-poly face leaves no gap.
    """
    fl, inv = math.floor, 1.0 / CELL
    ix, iy = [fl(x * inv) for x in xs], [fl(y * inv) for y in ys]
    for a, b, c in tris:
        x0, x1 = min(ix[a], ix[b], ix[c]), max(ix[a], ix[b], ix[c])
        y0, y1 = min(iy[a], iy[b], iy[c]), max(iy[a], iy[b], iy[c])
        if x1 - x0 <= 2 and y1 - y0 <= 2:
            for i in range(x0, x1 + 1):
                for j in range(y0, y1 + 1):
                    cells.add((i, j))
            continue
        pa, pb, pc = (xs[a], ys[a]), (xs[b], ys[b]), (xs[c], ys[c])
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                if _in_triangle((i + 0.5) * CELL, (j + 0.5) * CELL, pa, pb, pc):
                    cells.add((i, j))
        for p, q in ((pa, pb), (pb, pc), (pc, pa)):
            n = int(math.hypot(q[0] - p[0], q[1] - p[1]) / (CELL / 2)) + 1
            for k in range(n + 1):
                t = k / n
                cells.add((fl((p[0] + (q[0] - p[0]) * t) * inv), fl((p[1] + (q[1] - p[1]) * t) * inv)))


def map_cells(cells: set, point) -> set:
    """The cells of the grid that `cells` cover once each of their points goes through `point`.

    Each cell's four corners are mapped (pulled a hair inside), so a move that doesn't
    land on the grid still covers every cell the old one reaches.
    """
    out, fl, e = set(), math.floor, CELL * 0.49
    for i, j in cells:
        cx, cy = (i + 0.5) * CELL, (j + 0.5) * CELL
        for dx, dy in ((-e, -e), (e, -e), (-e, e), (e, e)):
            x, y = point(cx + dx, cy + dy)
            out.add((fl(x / CELL), fl(y / CELL)))
    return out


def outline_hits(cells: set, rect: tuple) -> bool:
    """Whether any cell of the outline lies in rect (x0, y0, x1, y1)."""
    fl = math.floor
    i0, i1 = fl(rect[0] / CELL), fl((rect[2] - 1e-9) / CELL)
    j0, j1 = fl(rect[1] / CELL), fl((rect[3] - 1e-9) / CELL)
    if (i1 - i0 + 1) * (j1 - j0 + 1) > len(cells):
        return any(i0 <= i <= i1 and j0 <= j <= j1 for i, j in cells)
    return any((i, j) in cells for i in range(i0, i1 + 1) for j in range(j0, j1 + 1))


@functools.lru_cache(maxsize=4)
def _plate_outlines(f: Path, stamp: float) -> dict:
    """{plate number: outline cells in absolute coordinates, plate origin} of a written file."""
    from ..engines import orca as engine  # the engine imports this module: import it when used
    with zipfile.ZipFile(f) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    _used, plates = engine.analyze(f.stem, f)
    bed = engine.Bed.of(cfg)
    return {p.number: (p.cells, engine.plate_origin(p.number - 1, len(plates), bed)) for p in plates}


def plate_outline(f: Path, n: int, center: tuple, s: float) -> set | None:
    """Plate n's outline in plate-local cells, scaled by s about `center` as the sliced copy was;
    None when there's no outline to go by, and the bounding box decides."""
    cells, (ox, oy) = _plate_outlines(f, f.stat().st_mtime).get(n, (set(), (0.0, 0.0)))
    if not cells:
        return None
    cx, cy = center
    return map_cells(cells, lambda x, y: (cx + (x - ox - cx) * s, cy + (y - oy - cy) * s))


def tower_rect(cfg: dict, at: tuple) -> tuple:
    """The prime tower's likely footprint with its brim, from its corner `at`."""
    w, d, b = tower_size(cfg)
    return at[0] - b, at[1] - b, at[0] + w + b, at[1] + d + b


@functools.lru_cache(maxsize=None)
def template(key: str) -> tuple[dict, dict, tuple]:
    """(meta, PLA template settings, version stamp) of one printer."""
    meta, settings = templates_build.load(key)
    with zipfile.ZipFile(TEMPLATES / key / "PLA.3mf") as z:
        stamp = u1.stamp(z)
    return meta, settings["PLA"], stamp


@functools.lru_cache(maxsize=None)
def source_facts(src: Path) -> dict:
    return u1.source_facts(src)


@functools.lru_cache(maxsize=None)
def source_sizes(src: Path) -> dict:
    with zipfile.ZipFile(src) as z:
        return {n: tuple(hi[k] - lo[k] for k in range(3)) for n, lo, hi in u1.plate_boxes(z)}


def on_plates(z: zipfile.ZipFile, cfg: dict, label: str) -> tuple[list, list, list, list]:
    """Check 9: (info, problems, warnings, plate boxes in plate coordinates)."""
    pts = [tuple(float(c) for c in p.split("x")) for p in cfg["printable_area"]]
    area = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    height = float(cfg["printable_height"])
    bw, bd = area[2] - area[0], area[3] - area[1]
    excl = exclusion_rects(cfg)
    tower = cfg.get("enable_prime_tower") == "1"
    txs, tys = cfg.get("wipe_tower_x") or [], cfg.get("wipe_tower_y") or []

    info, problems, warnings, local = [], [], [], []
    boxes = u1.plate_boxes(z)
    for idx, (number, a, b) in enumerate(boxes):
        bx0, by0, bx1, by1 = u1.plate_area(number - 1, len(boxes), area)
        name = f"plate {number}" if len(boxes) > 1 else "plate"
        if a[0] == float("inf"):
            problems.append(f"{name}: no printed part found to place")
            local.append(None)
            continue
        lo = (a[0] - bx0 + area[0], a[1] - by0 + area[1], a[2])
        hi = (b[0] - bx0 + area[0], b[1] - by0 + area[1], b[2])
        local.append((number, lo, hi))
        info.append(f"{name}: x {lo[0]:.1f}..{hi[0]:.1f} y {lo[1]:.1f}..{hi[1]:.1f} z {lo[2]:.1f}..{hi[2]:.1f} "
                    f"on its {bw:g} x {bd:g} x {height:g} bed")
        sx, sy, sz = (hi[k] - lo[k] for k in range(3))
        fits = sz <= height + EPS and ((sx <= bw + EPS and sy <= bd + EPS) or (sy <= bw + EPS and sx <= bd + EPS))
        if not fits:
            best = max(min(bw / sx, bd / sy), min(bw / sy, bd / sx))
            pct = math.floor(min(best, height / sz) * 1000) / 10
            warnings.append(f"{name} is bigger than the {label} bed ({sx:.1f} x {sy:.1f} x {sz:.1f} mm): "
                            f"converted at full size, fits at {pct:g}% or less -- scale it in the slicer to print")
            if not (area[0] <= (lo[0] + hi[0]) / 2 <= area[2] and area[1] <= (lo[1] + hi[1]) / 2 <= area[3]
                    and lo[2] >= -0.05):
                problems.append(f"{name} isn't on its own plate at all")
            continue
        if (lo[0] < area[0] - EPS or lo[1] < area[1] - EPS or hi[0] > area[2] + EPS or hi[1] > area[3] + EPS
                or hi[2] > height + EPS or lo[2] < -0.05):
            problems.append(f"{name} hangs off its plate (x {lo[0]:.1f}..{hi[0]:.1f}, y {lo[1]:.1f}..{hi[1]:.1f}, "
                            f"z to {hi[2]:.1f}; the bed is {bw:g} x {bd:g} x {height:g}) -- the slicer won't slice it")
        footprint = (lo[0], lo[1], hi[0], hi[1])
        reach = outline_reach(cfg)
        gap = min(lo[0] - area[0], lo[1] - area[1], area[2] - hi[0], area[3] - hi[1])
        if gap < reach:
            warnings.append(f"{name} is {gap:.1f} mm from the bed's edge; its brim and skirt reach about "
                            f"{reach:.1f} mm -- the slicer may refuse it (unprintable area)")
        for r in excl:
            if overlaps(footprint, r):
                warnings.append(f"{name}'s footprint reaches into the bed's exclusion area "
                                f"{r[0]:g}..{r[2]:g} x {r[1]:g}..{r[3]:g} -- check it in the slicer")
        if tower and idx < len(txs) and idx < len(tys):
            tx, ty = float(txs[idx]), float(tys[idx])
            t = tower_rect(cfg, (tx, ty))
            # In the bounding box is fine where the outline leaves room for it.
            on_model = overlaps(t, footprint) and (
                (outline := plate_outline(Path(z.filename), number, (0.0, 0.0), 1.0) if z.filename else None) is None
                or outline_hits(outline, t))
            if on_model or not (area[0] <= t[0] and area[1] <= t[1] and t[2] <= area[2] and t[3] <= area[3]):
                warnings.append(f"{name}: the prime tower at {tx:.0f},{ty:.0f} (about {t[2] - t[0]:.0f} x {t[3] - t[1]:.0f} mm) "
                                f"may touch the model or the bed's edge "
                                f"-- slice it (--slice) and look")
    return info, problems, warnings, local


def audit(f: Path, src: Path | None, key: str) -> tuple[list[str], list[str], list[str]]:
    """(info, problems, warnings) for one file made for printer `key`."""
    printer = PRINTERS[key]
    meta, tpl, tpl_stamp = template(key)
    with zipfile.ZipFile(f) as z:
        names = z.namelist()
        cfg = json.loads(z.read("Metadata/project_settings.config").decode())
        codes = u1.paint_codes(z)
        ms = u1.model_settings(z)
        file_stamp = u1.stamp(z)
        bed_info, bed_problems, bed_warnings, boxes = on_plates(z, cfg, printer.label)
    pal = [c.upper() for c in cfg.get("filament_colour", [])]
    k = len(pal)
    painted = sum(codes.values())

    problems: list[str] = []
    warnings: list[str] = list(bed_warnings)
    info = [f"printer   : {cfg.get('printer_settings_id')} / {cfg.get('print_settings_id')}",
            f"filaments : {cfg.get('filament_settings_id')}",
            f"palette   : {pal}",
            f"paint_color attrs: {painted:,}", *bed_info]

    # 1. white blob
    if pal and not [c for c in pal if rgba(c) not in WHITE]:
        problems.append("WHITE-BLOB: every filament is white -- the ColorStacking is gone")
    # 2. printer
    if (cfg.get("printer_settings_id"), cfg.get("print_settings_id")) != (printer.machine, printer.process):
        problems.append(f"printer/print presets are {cfg.get('printer_settings_id')!r} / "
                        f"{cfg.get('print_settings_id')!r}, not {printer.machine!r} / {printer.process!r}")
    # 3. colors and their presets
    if not 0 < k <= printer.tools:
        problems.append(f"{k} filaments -- the {printer.label} takes 1 to {printer.tools}")
    for i, (t, sid) in enumerate(zip(cfg.get("filament_type", []), cfg.get("filament_settings_id", [])), start=1):
        if printer.filaments.get(t) != sid:
            problems.append(f"filament {i} is {t} on preset {sid!r}, not the {printer.label}'s {t} preset")
    # 4. paint
    if painted == 0:
        problems.append("no paint_color data -- no color painting at all")
    if getattr(f.stat(), "st_flags", 0) & 0x8000:  # UF_HIDDEN
        problems.append("hidden in Finder (macOS hidden flag) -- chflags nohidden, or re-convert")
    problems += bed_problems

    # 10. version stamp
    info.append(f"stamp     : {file_stamp[0]!r}")
    if file_stamp[0] != tpl_stamp[0]:
        problems.append(f"version stamp {file_stamp[0]!r} isn't OrcaSlicer's {tpl_stamp[0]!r}")

    # 12. settings complete, nothing of Bambu's slice left
    short = [key_ for key_ in meta["filament_keys"] if key_ in cfg and (not k or len(cfg[key_]) % k)]
    if "filament_self_index" in cfg and sorted(set(cfg["filament_self_index"]), key=int) != [str(i) for i in range(1, k + 1)]:
        short.append("filament_self_index")
    if short:
        problems.append(f"{len(short)} per-filament settings don't have {k} values: {short[:6]}")
    n = len(cfg.get("flush_volumes_matrix", []))
    if not k or n % (k * k):
        problems.append(f"flush_volumes_matrix has {n} values, want {k} x {k} per nozzle")
    src_cfg = None
    if src is not None:
        with zipfile.ZipFile(src) as z:
            src_cfg = json.loads(z.read("Metadata/project_settings.config"))
    want_bed = templates_build.bed_type_for(key, tpl, src_cfg)
    if cfg.get("curr_bed_type") != want_bed:
        problems.append(f"bed type {cfg.get('curr_bed_type')!r}, want {want_bed!r} for this printer")
    leftovers = [n for n in names if BAMBU_LEFTOVERS.match(n)]
    if leftovers:
        problems.append(f"Bambu's slice leftovers still inside: {leftovers}")
    # 12b. read the same in the brands' own slicers (brandcheck.py)
    percent = sorted(k for k, v in cfg.items() if k.endswith("line_width") and isinstance(v, str) and v.endswith("%"))
    if percent:
        problems.append(f"line widths in % ({', '.join(percent)}): QIDI Studio reads some as mm and won't slice")
    if exclusion_rects(cfg) != exclusion_rects(cfg, quads=True):
        problems.append("bed_exclude_area is one area to OrcaSlicer (one polygon) and another to Anycubic Slicer "
                        "Next (4 points at a time)")
    gcode = cfg.get("change_filament_gcode")
    lines = [ln.strip() for ln in gcode.split("\n")] if isinstance(gcode, str) else []
    if any(ln == ACE_BLOCK and (i + 1 >= len(lines) or lines[i + 1] != ACE_GUARD) for i, ln in enumerate(lines)):
        problems.append("the color change's ACE block isn't guarded: Anycubic Slicer Next won't slice it "
                        "until the printer's filaments are mapped")

    try:
        hist = u1.histogram(codes)
    except ValueError as e:
        problems.append(f"unreadable paint: {e}")
        hist = Counter()
    # 6a. tool numbers, whatever the source
    states = sorted(s for s in hist if s)
    numbers = sorted({int(n) for n in u1.ANY_FILAMENT.findall(ms) if n != "0"})
    if [s for s in states if s > k] or [n for n in numbers if n > k]:
        problems.append(f"paint states {states} / extruder numbers {numbers} -- the file has filaments 1-{k}")

    if src is None:
        info.append("source    : not given -- checks 5-8 and 11 skipped")
        return info, problems, warnings

    # 11. size unchanged
    want_sizes = source_sizes(src)
    for box in boxes:
        if box is None:
            continue
        n, lo, hi = box
        got = tuple(hi[i] - lo[i] for i in range(3))
        want = want_sizes.get(n)
        name = f"plate {n}" if len(boxes) > 1 else "plate"
        if want is None:
            problems.append(f"{name} has no counterpart in the source")
        elif not any(all(abs(g - w) <= 0.01 for g, w in zip(got, cand)) for cand in (want, (want[1], want[0], want[2]))):
            problems.append(f"{name} is {got[0]:.2f} x {got[1]:.2f} x {got[2]:.2f} mm, the source "
                            f"{want[0]:.2f} x {want[1]:.2f} x {want[2]:.2f} -- the model was resized")
        else:
            info.append(f"size      : {name} {got[0]:.1f} x {got[1]:.1f} x {got[2]:.1f} mm, as designed")

    sf = source_facts(src)
    mapping, used = sf["mapping"], sf["used"]
    info.append(f"source    : uses slots {used} -> filaments {[mapping[s] for s in used]}")
    info.append("paint leaves: " + ", ".join(f"F{s}:{hist[s]:,}" for s in sorted(hist)))
    if len(used) > printer.tools:
        problems.append(f"the source uses {len(used)} slots {used} -- more than the {printer.label}'s {printer.tools}")
        return info, problems, warnings
    if k != len(used):
        problems.append(f"{k} filaments for the source's {len(used)} used slots")

    # 3b. each filament's type is its source slot's
    with zipfile.ZipFile(src) as z:
        src_types = json.loads(z.read("Metadata/project_settings.config")).get("filament_type", [])
    want_types = [BAMBU_TYPES.get(src_types[s - 1]) if s <= len(src_types) else None for s in used]
    if cfg.get("filament_type", [])[: len(want_types)] != want_types:
        problems.append(f"filament types {cfg.get('filament_type')}, the source's used slots are {want_types}")

    # 5. palette
    want = [sf["palette"][s - 1] for s in used]
    if [rgba(c) for c in pal[: len(want)]] != want:
        problems.append(f"palette differs from the source's used slots: want {want}, file has {pal[:len(want)]}")
    # 6b. part/object extruders
    out_exts = Counter(int(e) for e in u1.EXTRUDER.findall(ms) if e != "0")
    want_exts = Counter(mapping.get(e, e) for e in sf["exts"])
    if out_exts != want_exts:
        problems.append(f"part/object extruders {dict(out_exts)}, want the source's mapped {dict(want_exts)}")
    # 7. painted faces
    src_painted = sum(sf["codes"].values())
    if painted != src_painted:
        problems.append(f"painted faces {painted:,}, source has {src_painted:,}")
    # 8. paint leaves per filament
    want_hist: Counter = Counter()
    for s, n in sf["hist"].items():
        want_hist[mapping.get(s, s) if s else 0] += n
    if hist != want_hist:
        diff = {s: (want_hist.get(s, 0), hist.get(s, 0)) for s in set(want_hist) | set(hist)
                if want_hist.get(s, 0) != hist.get(s, 0)}
        problems.append(f"paint leaves per filament don't match the source after mapping "
                        f"(filament: want, got) {dict(sorted(diff.items()))} -- colors on the wrong filaments")
    return info, problems, warnings


AXIS = re.compile(r"\b([XYE])(-?[\d.]+)")
TOOL = re.compile(r"^T(\d+)\b")


def gcode_facts(path: Path) -> dict:
    """Tool changes, manual-change commands and the prime tower's printed extent in one plate's G-code.

    The tower's extent is where filament was laid down along a move inside
    ;TYPE:Prime tower -- not the travel to a purge chute in the same block, nor
    the de-retraction at the model that follows it before the next ;TYPE: (a
    Klipper EXCLUDE_OBJECT_START ends the tower block too).
    """
    tools, manual = Counter(), Counter()
    tower = [math.inf, math.inf, -math.inf, -math.inf]
    in_tower, relative, x, y, e = False, False, 0.0, 0.0, 0.0
    with open(path, encoding="utf-8", errors="replace") as g:
        for line in g:
            if line.startswith(";TYPE:") or line.startswith("; FEATURE:"):  # OrcaSlicer, Bambu Studio
                in_tower = line.split(":", 1)[1].strip().lower() in ("prime tower", "wipe tower")
                continue
            if line.startswith("EXCLUDE_OBJECT_START"):
                in_tower = False
            code = line.split(";")[0].strip()
            if not code:
                continue
            word = code.split()[0].upper()
            if word in ("G0", "G1", "G2", "G3"):
                axes = {a: float(v) for a, v in AXIS.findall(code)}
                laid = ("E" in axes and ("X" in axes or "Y" in axes)
                        and (axes["E"] > 0 if relative else axes["E"] > e))
                if "E" in axes and not relative:
                    e = axes["E"]
                if laid and in_tower:
                    for px, py in ((x, y), (axes.get("X", x), axes.get("Y", y))):
                        tower = [min(tower[0], px), min(tower[1], py), max(tower[2], px), max(tower[3], py)]
                x, y = axes.get("X", x), axes.get("Y", y)
            elif word == "M83":
                relative = True
            elif word == "M82":
                relative = False
            elif word == "G92":
                e = float(dict(AXIS.findall(code)).get("E", e))
            elif TOOL.match(word):
                tools[int(word[1:])] += 1
            elif word in templates_build.MANUAL_CHANGE:
                manual[word] += 1
    return {"tools": tools, "manual": manual, "tower": tower if tower[0] != math.inf else None}


def scaled_to_fit(f: Path, dest: Path, cfg: dict, crowded: bool = False) -> dict:
    """Copy f to dest with every plate bigger than the bed scaled down to fit, about its own center.

    Only for slicing: OrcaSlicer won't slice a model bigger than the bed, and the
    file keeps it at full size on purpose. This is what a downloader would do --
    scale it in the slicer -- so slicing the copy proves the rest of the file.
    The model stays centered, small enough that the prime tower fits beside it,
    and the copy's tower goes there. `crowded`: do that to every plate, because
    at full size the tower collided with the model. Returns {plate number: scale}.
    """
    x0, y0, x1, y1 = reachable_area(cfg)
    bw, bd = x1 - x0, y1 - y0
    height = float(cfg["printable_height"])
    tower = cfg.get("enable_prime_tower") == "1"
    tw, td, brim = tower_size(cfg)
    with zipfile.ZipFile(f) as z:
        boxes = u1.plate_boxes(z)
        root = ET.fromstring(u1.model_settings(z))
        settings = json.loads(z.read("Metadata/project_settings.config"))
    plate_objs = {}
    for p in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        plate_objs[int(meta.get("plater_id", 0))] = {m.get("value") for inst in p.findall("model_instance")
                                                     for m in inst.findall("metadata") if m.get("key") == "object_id"}
    scales, by_obj = {}, {}
    for idx, (n, lo, hi) in enumerate(boxes):
        sx, sy, sz = (hi[k] - lo[k] for k in range(3))
        s = min(bw / sx, bd / sy, height / sz)  # as placed: the file keeps Bambu's orientation
        if s >= 1 and not crowded:
            continue
        s = min(s, 1.0) * 0.97  # a margin inside the bed edge
        if tower:
            room = 2 * (brim + 2)
            # The width that lets the model stay biggest: a narrow tower that purges
            # into itself runs deeper than the bed (the K2 SE's 35 mm one).
            own = _first(cfg.get("prime_tower_width")) or 35
            best = None
            for w in [own] + [own * k for k in (1.5, 2.0, 3.0, 4.0) if own * k <= bw / 2]:
                tw, td, _b = tower_size(cfg, w)
                beside = (bw - 2 * (tw + room)) / sx  # tower in the strip left or right
                behind = (bd - 2 * (td + room)) / sy  # or in front
                if best is None or max(beside, behind) > best[0]:
                    best = (max(beside, behind), w, tw, td, beside, behind)
            _fit, w, tw, td, beside, behind = best
            if w != own:
                settings["prime_tower_width"] = f"{w:g}"
            s = min(s, max(beside, behind))
            gap_x, gap_y = (bw - sx * s) / 2, (bd - sy * s) / 2
            if beside >= behind:
                at = (x0 + (gap_x - tw) / 2, y0 + (bd - td) / 2)
            else:
                at = (x0 + (bw - tw) / 2, y0 + (gap_y - td) / 2)
            for key_, v in zip(("wipe_tower_x", "wipe_tower_y"), at):
                if idx < len(settings.get(key_, [])):
                    settings[key_][idx] = f"{v:.3f}"
        s = math.floor(s * 1000) / 1000
        scales[n] = s
        for oid in plate_objs.get(n, ()):
            by_obj[oid] = (s, (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2)

    def item(m: re.Match) -> str:
        tag = m.group(0)
        oid = re.search(r'\bobjectid="([^"]+)"', tag).group(1)
        if oid not in by_obj:
            return tag
        s, cx, cy = by_obj[oid]
        t = [float(v) for v in re.search(r'\btransform="([^"]*)"', tag).group(1).split()]
        t = [v * s for v in t[:9]] + [(t[9] - cx) * s + cx, (t[10] - cy) * s + cy, t[11] * s]
        return re.sub(r'\btransform="[^"]*"', 'transform="' + " ".join(f"{v:.9g}" for v in t) + '"', tag)

    with zipfile.ZipFile(f) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in zin.infolist():
            data = zin.read(it.filename)
            if it.filename == "3D/3dmodel.model" and by_obj:
                data = re.sub(r"<item\b[^>]*>", item, data.decode()).encode()
            elif it.filename == "Metadata/project_settings.config" and by_obj:
                data = json.dumps(settings, indent=4).encode()
            zout.writestr(it, data)
    return scales


def slice_check(f: Path, key: str, slicer: Slicer | None = None) -> tuple[list[str], list[str], list[str]]:
    """Check 13: (info, problems, warnings) from slicing every plate in the printer's
    slicer, or in `slicer` (brandcheck.py: the brand's own fork)."""
    printer = PRINTERS[key]
    slicer = slicer or SLICERS[printer.slicer]
    info, problems, warnings = [], [], []
    with zipfile.ZipFile(f) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config").decode())
        _i, _p, _w, boxes = on_plates(z, cfg, printer.label)
        file_stamp = u1.stamp(z)[0]
    k = len(cfg.get("filament_colour", []))
    # OrcaSlicer 2.4.2 stamps its projects BambuStudio-02.06.00.51 and then its CLI
    # refuses them as newer than itself (2.4.2): its own exports need the override.
    # Any other slicer gets the file as a downloader would, and a refusal is noted.
    newer = slicer == SLICERS["orca"]
    with tempfile.TemporaryDirectory() as tmp:
        def run_on(crowded: bool) -> tuple[subprocess.CompletedProcess, dict]:
            target = Path(tmp) / "scaled.3mf"
            scales = scaled_to_fit(f, target, cfg, crowded)
            # A slicer can loop forever (OrcaSlicer 2.4.2's seam placer did): a slice that
            # hasn't finished in SLICE_LIMIT seconds is a failure, not a wait.
            cmd = [str(slicer.binary), *slicer.cli, *(["--allow-newer-file"] if newer else []),
                   "--outputdir", tmp, "--slice", "0", str(target if scales else f)]
            try:
                run = subprocess.run(cmd, capture_output=True, cwd=tempfile.gettempdir(), text=True, timeout=SLICE_LIMIT)
            except subprocess.TimeoutExpired:
                run = subprocess.CompletedProcess(cmd, -9, "", f"still slicing after {SLICE_LIMIT // 60} min: "
                                                             f"the slicer hung")
            return run, scales

        run, scales = run_on(False)
        if run.returncode == VERSION_REFUSED and not newer:
            # The app opens it, with a note to update: in the GUI that's a notice, not a refusal.
            warnings.append(f"{slicer.name}'s command line won't take the file's version ({file_stamp}) without "
                            f"--allow-newer-file (sliced with it)")
            newer = True
            run, scales = run_on(False)
        if run.returncode in CRASHES:
            # Bambu Studio 2.8.2 sometimes aborts after slicing, in GCodeProcessor's
            # time estimates (macOS: "stack buffer overflow"; 2 in 6 runs of one H2D
            # file). A crash gets one more try; one that repeats is a failure.
            warnings.append(f"{slicer.name} crashed (exit {run.returncode}) and was run again")
            run, scales = run_on(False)
        log = (run.stdout + run.stderr).splitlines()
        if run.returncode in CONFLICT_EXITS or (run.returncode != 0 and
                                                any("conflicts found between WipeTower" in ln for ln in log)):
            # The tower collides with the model at full size: there's no room left
            # for it. Like a model bigger than the bed, that's the downloader's to
            # arrange -- so slice it as they would, smaller with the tower beside it.
            run, scales = run_on(True)
            log = (run.stdout + run.stderr).splitlines()
            if run.returncode == 0:
                warnings.append(f"at full size {slicer.name}'s prime tower collides with the model: move the tower "
                                "or scale it down in the slicer (sliced at " + ", ".join(
                                    f"{v * 100:g}%" for v in scales.values()) + " with the tower beside it)")
        elif run.returncode in UNPRINTABLE_EXITS and not scales:
            # At full size its brim, skirt or tower runs off the bed: the downloader's to
            # fix, like a model bigger than the bed -- so slice it smaller, as they would.
            run, scales = run_on(True)
            log = (run.stdout + run.stderr).splitlines()
            if run.returncode == 0:
                warnings.append(f"at full size {slicer.name}'s brim, skirt or prime tower runs past the bed's "
                                "printable area: scale it down or narrow the brim in the slicer (sliced at "
                                + ", ".join(f"{v * 100:g}%" for v in scales.values()) + ")")
        if scales:
            info.append("sliced    : a copy with " + ", ".join(f"plate {n} at {v * 100:g}%" for n, v in scales.items())
                        + ", as a downloader would scale it")
        if run.returncode != 0:
            tail = [ln for ln in log if "shader" not in ln and "gl_FragColor" not in ln and "ERROR: 0:" not in ln][-6:]
            # Bambu Studio says why in result.json and nothing on the console.
            result = Path(tmp) / "result.json"
            if result.exists():
                try:
                    reason = json.loads(result.read_text(encoding="utf-8")).get("error_string")
                except ValueError:
                    reason = None
                if reason and reason != "Success.":
                    tail.append(reason)
            problems.append(f"{slicer.name} didn't slice it (exit {run.returncode}): " + " | ".join(tail))
            return info, problems, warnings
        for box in boxes:
            if box is None:
                continue
            n, lo, hi = box
            gcode = Path(tmp) / f"plate_{n}.gcode"
            name = f"plate {n}" if len(boxes) > 1 else "plate"
            if not gcode.exists() or gcode.stat().st_size == 0:
                problems.append(f"{name}: {slicer.name} wrote no G-code")
                continue
            facts = gcode_facts(gcode)
            info.append(f"sliced    : {name} {gcode.stat().st_size / 1e6:.0f} MB G-code, tool changes "
                        f"{dict(sorted(facts['tools'].items()))}"
                        + (f", prime tower {facts['tower'][0]:.0f}..{facts['tower'][2]:.0f} x "
                           f"{facts['tower'][1]:.0f}..{facts['tower'][3]:.0f}" if facts["tower"] else ""))
            if facts["manual"]:
                problems.append(f"{name}: the G-code stops for a person ({dict(facts['manual'])}) -- "
                                f"not a print-and-go multicolor file")
            if k > 1 and len(facts["tools"]) < 2:
                problems.append(f"{name}: {k} filaments but the G-code changes tool {dict(facts['tools'])} -- "
                                f"the colors won't print")
            if facts["tower"]:
                tw = facts["tower"]
                s = scales.get(n, 1.0)
                cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
                foot = (cx - (cx - lo[0]) * s, cy - (cy - lo[1]) * s, cx + (hi[0] - cx) * s, cy + (hi[1] - cy) * s)
                # Inside the bounding box is fine where the outline leaves room (a corner
                # beside a head): only a tower on the outline itself is in the model's way.
                outline = plate_outline(f, n, (cx, cy), s) if overlaps(tuple(tw[:4]), foot) else None
                if overlaps(tuple(tw[:4]), foot) and (outline is None or outline_hits(outline, tuple(tw[:4]))):
                    msg = (f"{name}: the prime tower {slicer.name} printed ({tw[0]:.0f}..{tw[2]:.0f} x "
                           f"{tw[1]:.0f}..{tw[3]:.0f}) overlaps the model's footprint "
                           f"({foot[0]:.0f}..{foot[2]:.0f} x {foot[1]:.0f}..{foot[3]:.0f})")
                    # A model bigger than the bed has no room kept for the tower: whoever scales it
                    # moves the tower too. A warning there, a failure anywhere else.
                    (warnings if n in scales else problems).append(msg + (" -- move the tower after scaling"
                                                                          if n in scales else ""))
        errors = [ln for ln in log if "[error]" in ln and "shader" not in ln and "Voronoi" not in ln
                  and "exclude triangles" not in ln]
        if errors:
            warnings.append(f"{slicer.name} logged {len(errors)} error line(s) but sliced; first: {errors[0][-160:]}")
    return info, problems, warnings



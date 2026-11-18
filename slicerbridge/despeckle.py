"""
despeckle.py -- merge paint islands too small to print into the color around them.

A model painted from a textured OBJ carries specks: anti-aliased texture edges rounded to the
nearest filament, each a fraction of a square millimetre. A slice plane that cuts one prints a
single layer of its color, a stray line on the wall. Every same-color island smaller than the
threshold (MIN_AREA) takes the color of the neighbor it shares the longest edge with.

- Each triangle counts as the color that covers most of its area. A triangle the brush split
  between colors keeps its paint unless its color changes.
- An edge to a neighbor that is itself an island counts a hundredth (SMALL_WEIGHT), so an island
  goes to a big region where it touches one. Passes repeat until no island is left, at most
  MAX_PASSES. Two islands that would trade colors don't: the smaller takes the larger's.
- An unpainted triangle prints in its part's filament, and counts as that filament.
- A triangle whose color changes is written as one unsplit leaf of its new color. Every other
  byte of the model stays as it was, and every other entry of the 3MF is copied. A slice kept in
  the file (plate G-code) was made from the old paint, and is left out.

Areas are in mm² as placed on the plate. A part placed more than once at different scales is
measured at the largest. Paint is TriangleSelector's: paint_color in Bambu Studio and its forks,
slic3rpe:mmu_segmentation in PrusaSlicer.

Standard library only: a 1.6 million triangle model takes about 10 seconds.
"""
from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from array import array
from collections import Counter
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path

from .errors import Refusal
from .output import land, partial
from .printers import STAMP

MIN_AREA = 1.0        # mm²
MAX_PASSES = 12
SMALL_WEIGHT = 0.01   # an edge to a neighbor that is itself an island
FROM_TO_SHOWN = 12

ROOT_MODEL = "3D/3dmodel.model"
GCODE = re.compile(r"^Metadata/plate_\d+\.gcode(\.md5)?$")
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

OBJECT = re.compile(rb"<object\b([^>]*)>")
COMPONENT = re.compile(rb"<component\b([^>]*)>")
ITEM = re.compile(rb"<item\b([^>]*)>")
ATTR = re.compile(rb'([\w:.-]+)\s*=\s*"([^"]*)"')
VERTEX = re.compile(rb'<vertex\s+x="([^"]*)"\s+y="([^"]*)"\s+z="([^"]*)"')
VERTEX_ANY = re.compile(rb"<vertex\b([^>]*)>")
# The fast form is how every slicer writes a triangle; any other attribute order takes the slow one.
TRIANGLE = re.compile(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"([^>]*)>')
TRIANGLE_ANY = re.compile(rb"<triangle\b(?!s)([^>]*)>")
PAINT = re.compile(rb'\s(paint_color|slic3rpe:mmu_segmentation)\s*=\s*"([0-9A-Fa-f]*)"')


# ---------------------------------------------------------------- paint ----

class PaintError(ValueError):
    pass


# Each child's share of its parent's area, in the order the children are serialized
# (last child first). A split on one side halves the triangle; on two sides it makes two
# quarter corners and a half (the last child); on three, four quarters.
SHARES = {1: (0.5, 0.5), 2: (0.5, 0.25, 0.25), 3: (0.25, 0.25, 0.25, 0.25)}


def leaf_shares(code: bytes | str) -> dict[int, float]:
    """state -> share of the triangle's area it covers, for one TriangleSelector paint string.

    The bits are int(code, 16) read from bit 0 up (the end of the hex string, LSB first):

        node := split:2 ( state:2 [ext:4 if state == 3 -> state = ext + 3]
                        | side:2 node * (split + 1) )

    Split points are edge midpoints, so each leaf's share is exact.
    """
    if not code:
        return {0: 1.0}
    try:
        v = int(code, 16)
    except ValueError:
        raise PaintError(f"not a paint string: {code[:40]!r}") from None
    nbits = 4 * len(code)
    pos, out, stack = 0, {}, [1.0]
    while stack:
        share = stack.pop()
        if pos + 4 > nbits:
            raise PaintError(f"truncated paint string {code[:40]!r}")
        split = (v >> pos) & 3
        if split == 0:
            state = (v >> (pos + 2)) & 3
            pos += 4
            if state == 3:
                if pos + 4 > nbits:
                    raise PaintError(f"truncated paint string {code[:40]!r}")
                state = ((v >> pos) & 15) + 3
                pos += 4
            out[state] = out.get(state, 0.0) + share
        else:
            pos += 4  # split, then the special side
            stack.extend(share * s for s in reversed(SHARES[split]))
    if nbits - pos >= 4 and v >> pos:
        raise PaintError(f"data after the root node in paint string {code[:40]!r}")
    return out


def leaf(state: int) -> bytes:
    """One unsplit triangle in `state`: split 0, then the state in 2 bits, or 0b11 and state - 3
    in 4 more. States 1 and 2 fit one nibble ("4", "8"); 3 and up take two ("0C" for 3)."""
    if not 0 < state <= 18:
        raise PaintError(f"state {state} can't be written as a TriangleSelector leaf")
    if state <= 2:
        return b"%X" % (state << 2)
    return b"%02X" % (0b1100 | ((state - 3) << 4))


# ---------------------------------------------------------------- 3MF ----

def _attrs(raw: bytes) -> dict[bytes, bytes]:
    return dict(ATTR.findall(raw))


def _linear(text: bytes | None) -> tuple:
    """The 3x3 part of a 3MF transform (row-vector convention: p' = p * M)."""
    vals = [float(v) for v in (text or b"").split()]
    return tuple(vals[:9]) if len(vals) == 12 else IDENTITY


def _then(a: tuple, b: tuple) -> tuple:
    """Linear map a, then b."""
    return tuple(sum(a[3 * r + k] * b[3 * k + c] for k in range(3)) for r in range(3) for c in range(3))


def _entry(path: bytes | None, default: str) -> str:
    return path.decode().lstrip("/") if path else default


@dataclass
class _Object:
    start: int
    end: int
    mesh: bool
    components: list = field(default_factory=list)   # (entry, object id, linear part)


def _objects(buf: bytes, entry: str) -> dict[str, _Object]:
    objs = {}
    for m in OBJECT.finditer(buf):
        oid = _attrs(m.group(1)).get(b"id", b"").decode()
        end = buf.find(b"</object>", m.end())
        end = len(buf) if end < 0 else end
        mesh = buf.find(b"<mesh", m.end(), end) >= 0
        o = _Object(m.start(), end, mesh)
        for c in COMPONENT.finditer(buf, m.end(), end):
            a = _attrs(c.group(1))
            path = next((v for k, v in a.items() if k == b"path" or k.endswith(b":path")), None)
            o.components.append((_entry(path, entry), a.get(b"objectid", b"").decode(), _linear(a.get(b"transform"))))
        objs[oid] = o
    return objs


def _root(z: zipfile.ZipFile) -> str:
    try:
        rels = z.read("_rels/.rels").decode(errors="replace")
    except KeyError:
        return ROOT_MODEL
    for rel in re.findall(r"<Relationship\b[^>]*>", rels):
        if "3dmodel" in rel:
            target = re.search(r'Target="([^"]+)"', rel)
            if target:
                return target.group(1).lstrip("/")
    return ROOT_MODEL


@dataclass
class _Layout:
    root: str
    placements: dict     # (entry, object id) -> [linear part, ...]
    extruders: dict      # (entry, object id) -> [(first triangle, last triangle, filament), ...]
    palette: list        # filament n -> "#RRGGBB" at palette[n - 1]


def _layout(z: zipfile.ZipFile) -> _Layout:
    """Where each mesh sits on the plate, and which filament its unpainted triangles print in."""
    names = set(z.namelist())
    root = _root(z)
    if root not in names:
        raise Refusal(f"no {root} in the file: not a 3MF model")
    buf = z.read(root)
    objs = _objects(buf, root)
    placements: dict = {}

    def walk(oid: str, lin: tuple, depth: int) -> None:
        o = objs.get(oid)
        if o is None or depth > 16:
            return
        if o.mesh:
            placements.setdefault((root, oid), []).append(lin)
        for entry, child, clin in o.components:
            if entry == root:
                walk(child, _then(clin, lin), depth + 1)
            else:  # Bambu Studio's parts: a mesh object in 3D/Objects/*.model
                placements.setdefault((entry, child), []).append(_then(clin, lin))

    for m in ITEM.finditer(buf):
        a = _attrs(m.group(1))
        walk(a.get(b"objectid", b"").decode(), _linear(a.get(b"transform")), 0)

    extruders: dict = {}
    palette: list = []
    if "Metadata/model_settings.config" in names:  # Bambu Studio and its forks: per part
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
        for o in cfg.iter("object"):
            meta = {m.get("key"): m.get("value") for m in o.findall("metadata")}
            own = objs.get(o.get("id"))
            parts = {p.get("id"): {m.get("key"): m.get("value") for m in p.findall("metadata")}
                     for p in o.findall("part")}
            for entry, child, _ in (own.components if own else []):
                e = (parts.get(child) or {}).get("extruder") or "0"
                e = e if e != "0" else meta.get("extruder") or "0"
                if e.isdigit() and e != "0":
                    extruders[(entry, child)] = [(0, float("inf"), int(e))]
    if "Metadata/Slic3r_PE_model.config" in names:  # PrusaSlicer: per volume, a triangle range of the object
        cfg = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
        for o in cfg.iter("object"):
            meta = {m.get("key"): m.get("value") for m in o.findall("metadata")}
            ranges = []
            for v in o.findall("volume"):
                vmeta = {m.get("key"): m.get("value") for m in v.findall("metadata")}
                e = vmeta.get("extruder") or "0"
                e = e if e != "0" else meta.get("extruder") or "0"
                if e.isdigit() and e != "0" and (v.get("firstid") or "").isdigit() and (v.get("lastid") or "").isdigit():
                    ranges.append((int(v.get("firstid")), int(v.get("lastid")), int(e)))
            if ranges:
                extruders[(root, o.get("id"))] = sorted(ranges)
    if "Metadata/project_settings.config" in names:
        try:
            palette = [str(c) for c in json.loads(z.read("Metadata/project_settings.config")).get("filament_colour", [])]
        except ValueError:
            pass
    elif "Metadata/Slic3r_PE.config" in names:
        text = z.read("Metadata/Slic3r_PE.config").decode(errors="replace")
        found = {k: [c.strip().strip('"') for c in v.split(";")] for k, v in re.findall(r"^; (extruder_colour|filament_colour) = (.*)$", text, re.M)}
        ext, fil = found.get("extruder_colour", []), found.get("filament_colour", [])
        palette = [e or f for e, f in zip(ext + [""] * (len(fil) - len(ext)), fil)] if fil else ext
    return _Layout(root, placements, extruders, palette)


# ---------------------------------------------------------------- one mesh ----

@dataclass
class _Mesh:
    """What despeckling found in one mesh object, and the paint it changes."""
    entry: str
    oid: str
    span: tuple           # (start, end) of its <triangles> in the entry
    fast: bool            # TRIANGLE parses it (else TRIANGLE_ANY)
    attr: bytes           # the paint attribute it uses
    triangles: int
    islands_before: int = 0
    island_area: float = 0.0
    islands_after: int = 0
    passes: int = 0
    changed: dict = field(default_factory=dict)   # triangle index -> new state (0: unpainted)
    changed_area: float = 0.0
    split_changed: int = 0
    from_to: Counter = field(default_factory=Counter)
    before: set = field(default_factory=set)      # filaments painted before
    after: set = field(default_factory=set)       # and after


def _vertices(buf: bytes, start: int, end: int) -> tuple[array, array, array]:
    xs, ys, zs = array("d"), array("d"), array("d")
    for x, y, z in (m.groups() for m in VERTEX.finditer(buf, start, end)):
        xs.append(float(x)); ys.append(float(y)); zs.append(float(z))
    if len(xs) != buf.count(b"<vertex", start, end) - buf.count(b"<vertices", start, end):
        xs, ys, zs = array("d"), array("d"), array("d")
        for m in VERTEX_ANY.finditer(buf, start, end):
            a = _attrs(m.group(1))
            xs.append(float(a[b"x"])); ys.append(float(a[b"y"])); zs.append(float(a[b"z"]))
    return xs, ys, zs


def _triangle_matches(buf: bytes, span: tuple, fast: bool):
    """(v1, v2, v3, the attributes after them, where those start) for each triangle in order."""
    if fast:
        for m in TRIANGLE.finditer(buf, *span):
            a, b, c, rest = m.groups()
            yield int(a), int(b), int(c), rest, m.start(4)
    else:
        for m in TRIANGLE_ANY.finditer(buf, *span):
            raw = m.group(1)
            a = _attrs(raw)
            yield int(a[b"v1"]), int(a[b"v2"]), int(a[b"v3"]), raw, m.start(1)


def _read_triangles(buf: bytes, span: tuple, fast: bool, ranges: list, where: str):
    """Each triangle's vertices, and its kind: (color, split between colors, filaments), decoded once
    per paint string and part filament. An unpainted triangle is its part's filament (0 if unknown)."""
    va, vb, vc, kind = array("l"), array("l"), array("l"), array("l")
    kinds: list = []
    kind_of: dict = {}
    attr = None
    ri, (first, last, ext) = 0, ranges[0]
    for t, (a, b, c, rest, _) in enumerate(_triangle_matches(buf, span, fast)):
        va.append(a); vb.append(b); vc.append(c)
        code = b""
        if len(rest) > 2:
            pm = PAINT.search(rest)
            if pm:
                code = pm.group(2)
                attr = attr or pm.group(1)
        while t > last and ri + 1 < len(ranges):
            ri += 1
            first, last, ext = ranges[ri]
        e = ext if first <= t <= last else 0
        k = kind_of.get((code, e))
        if k is None:
            try:
                shares = leaf_shares(code)
            except PaintError as err:
                raise Refusal(f"{where}, triangle {t}: {err}") from None
            by_filament: dict = {}
            for s, share in shares.items():
                by_filament[s or e] = by_filament.get(s or e, 0.0) + share
            color = min(by_filament, key=lambda f: (-by_filament[f], f))  # the most area; a tie, the lower
            k = kind_of[(code, e)] = len(kinds)
            kinds.append((color, len(by_filament) > 1, frozenset(by_filament)))
        kind.append(k)
    return va, vb, vc, kind, kinds, attr


def _mesh(buf: bytes, entry: str, oid: str, o: _Object, layout: _Layout, min_area: float,
          max_passes: int) -> _Mesh | None:
    """Find the mesh's islands and the color each takes; None if it carries no paint."""
    ms = buf.find(b"<mesh", o.start, o.end)
    vs, ts = buf.find(b"<vertices", ms, o.end), buf.find(b"<triangles", ms, o.end)
    if vs < 0 or ts < 0:
        return None
    ve, te = buf.find(b"</vertices>", vs, o.end), buf.find(b"</triangles>", ts, o.end)
    ve, te = (o.end if ve < 0 else ve), (o.end if te < 0 else te)
    if PAINT.search(buf, ts, te) is None:
        return None
    span = (ts, te)
    n_any = buf.count(b"<triangle", ts, te) - buf.count(b"<triangles", ts, te)
    ranges = layout.extruders.get((entry, oid)) or [(0, float("inf"), 0)]
    fast = True
    va, vb, vc, kind, kinds, attr = _read_triangles(buf, span, fast, ranges, f"{entry} object {oid}")
    if len(kind) != n_any:
        fast = False
        va, vb, vc, kind, kinds, attr = _read_triangles(buf, span, fast, ranges, f"{entry} object {oid}")
    if len(kind) != n_any:
        raise Refusal(f"{entry} object {oid}: read {len(kind):,} of its {n_any:,} triangles")
    n = len(kind)
    if n == 0 or attr is None:
        return None
    color = [kinds[k][0] for k in kind]
    out = _Mesh(entry, oid, span, fast, attr, n)
    out.before = set().union(*(kinds[k][2] for k in set(kind)))

    # Areas on the plate: under each distinct placement, the largest.
    xs, ys, zs = _vertices(buf, vs, ve)
    nv = len(xs)
    if nv and max(max(va), max(vb), max(vc)) >= nv:
        raise Refusal(f"{entry} object {oid}: a triangle refers to a vertex past the mesh's {nv}")
    frames = []
    for lin in dict.fromkeys(tuple(round(x, 9) for x in lin) for lin in layout.placements.get((entry, oid), [IDENTITY])):
        if lin == IDENTITY:
            frames.append((xs, ys, zs))
        else:
            m0, m1, m2, m3, m4, m5, m6, m7, m8 = lin
            frames.append((array("d", (x * m0 + y * m3 + z * m6 for x, y, z in zip(xs, ys, zs))),
                           array("d", (x * m1 + y * m4 + z * m7 for x, y, z in zip(xs, ys, zs))),
                           array("d", (x * m2 + y * m5 + z * m8 for x, y, z in zip(xs, ys, zs)))))

    def area_of(t: int) -> float:
        a, b, c = va[t], vb[t], vc[t]
        best = 0.0
        for X, Y, Z in frames:
            ax, ay, az = X[a], Y[a], Z[a]
            ux, uy, uz = X[b] - ax, Y[b] - ay, Z[b] - az
            wx, wy, wz = X[c] - ax, Y[c] - ay, Z[c] - az
            cx, cy, cz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            best = max(best, 0.5 * sqrt(cx * cx + cy * cy + cz * cz))
        return best

    # Same-color regions: triangles joined across shared edges (union-find). An edge between
    # two colors is kept for the regions' borders.
    parent = list(range(n))
    border: list = []   # (triangle, triangle, vertex, vertex)
    open_edges: dict = {}
    for t in range(n):
        ct = color[t]
        a, b, c = va[t], vb[t], vc[t]
        for u, v in ((a, b), (b, c), (c, a)):
            key = u * nv + v if u < v else v * nv + u
            o2 = open_edges.pop(key, None)
            if o2 is None:
                open_edges[key] = t
            elif color[o2] == ct:
                x = o2
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                y = t
                while parent[y] != y:
                    parent[y] = parent[parent[y]]
                    y = parent[y]
                if x != y:
                    parent[y] = x
            else:
                border.append((o2, t, u, v))
    del open_edges
    for t in range(n):
        x = t
        while parent[x] != x:
            x = parent[x]
        parent[t] = x
    region = parent   # triangle -> its region, named by a triangle in it

    # Each region's area, counted until it reaches the threshold: past that, only "big" matters.
    area: dict = {}
    tri_area: dict = {}
    for t in range(n):
        r = region[t]
        sofar = area.get(r, 0.0)
        if sofar < min_area:
            ta = area_of(t)
            tri_area[t] = ta
            area[r] = sofar + ta
    small0 = {r for r, a in area.items() if a < min_area}

    # The regions' graph, around the small ones only: border length to each neighbor.
    adj: dict = {r: {} for r in small0}
    for t1, t2, u, v in border:
        r1, r2 = region[t1], region[t2]
        if r1 in adj or r2 in adj:
            length = sqrt((xs[u] - xs[v]) ** 2 + (ys[u] - ys[v]) ** 2 + (zs[u] - zs[v]) ** 2)
            if r1 in adj:
                adj[r1][r2] = adj[r1].get(r2, 0.0) + length
            if r2 in adj:
                adj[r2][r1] = adj[r2].get(r1, 0.0) + length
    del border

    col = {r: color[r] for r in area}   # a region's color (regions only merge with their own color)
    up: dict = {}                       # merged region -> the one it joined

    def find(x: int) -> int:
        root = x
        while root in up:
            root = up[root]
        while x != root:
            nxt = up[x]
            up[x] = root
            x = nxt
        return root

    def neighbors(r: int) -> dict:
        """Border length to each neighboring region, as the regions stand now."""
        now: dict = {}
        for nb, length in adj[r].items():
            nb = find(nb)
            if nb != r:
                now[nb] = now.get(nb, 0.0) + length
        adj[r] = now
        return now

    small = set(small0)
    out.islands_before = sum(1 for r in small if neighbors(r))
    out.island_area = sum(area[r] for r in small if adj[r])
    for _ in range(max_passes):
        choice = {}
        for r in small:
            score: dict = {}
            for nb, length in neighbors(r).items():
                score[col[nb]] = score.get(col[nb], 0.0) + length * (SMALL_WEIGHT if area[nb] < min_area else 1.0)
            if score:
                choice[r] = min(score, key=lambda f: (-score[f], f))   # the longest border; a tie, the lower
        if not choice:
            break
        out.passes += 1
        # Two islands that would trade colors: the smaller takes the larger's, which keeps it.
        keep = {nb if (area[r], -r) < (area[nb], -nb) else r
                for r, f in choice.items() for nb in adj[r]
                if nb in choice and f == col[nb] and choice[nb] == col[r]}
        for r in keep:
            del choice[r]
        for r, f in choice.items():
            col[r] = f
        joins = [(r, nb) for r in choice for nb in adj[r] if col[nb] == col[r]]
        for r, nb in joins:
            x, y = find(r), find(nb)
            if x == y:
                continue
            if (area[x], -x) < (area[y], -y):
                x, y = y, x
            up[y] = x
            area[x] += area.pop(y)
            if area[x] < min_area:
                merged = adj.pop(x, {})
                for k, length in adj.pop(y, {}).items():
                    merged[k] = merged.get(k, 0.0) + length
                adj[x] = merged
            else:
                adj.pop(x, None)
                adj.pop(y, None)
        small = {find(r) for r in small}
        small = {r for r in small if area[r] < min_area}
    out.islands_after = sum(1 for r in small if neighbors(r))

    # The triangles whose color changed.
    moved = {}
    for r in small0:
        f = col[find(r)]
        if f != color[r]:
            moved[r] = f
    kept = set()
    for t in range(n):
        f = moved.get(region[t])
        if f is None:
            kept.add(kind[t])
            continue
        out.changed[t] = f   # 0 only where the part's filament isn't known: left unpainted
        out.changed_area += tri_area[t]
        out.split_changed += kinds[kind[t]][1]
        out.from_to[(color[t], f)] += 1
    out.after = set(moved.values()).union(*(kinds[k][2] for k in kept))
    out.before.discard(0)
    out.after.discard(0)
    return out


def _rewrite(buf: bytes, mesh: _Mesh) -> bytes:
    """The entry with only the changed triangles' paint rewritten."""
    chunks, pos = [], 0
    for t, (_, _, _, rest, at) in enumerate(_triangle_matches(buf, mesh.span, mesh.fast)):
        state = mesh.changed.get(t)
        if state is None:
            continue
        pm = PAINT.search(rest)
        if state == 0:   # the part's own filament, which leaves unpainted
            new = rest[:pm.start()] + rest[pm.end():] if pm else rest
        else:
            code = leaf(state)
            if leaf_shares(code) != {state: 1.0}:
                raise PaintError(f"leaf({state}) = {code!r} doesn't decode to {state}")
            if pm:
                new = rest[:pm.start(2)] + code + rest[pm.end(2):]
            else:
                core = rest[:-1] if rest.endswith(b"/") else rest
                new = core + b" " + mesh.attr + b'="' + code + b'"' + rest[len(core):]
        chunks += [buf[pos:at], new]
        pos = at + len(rest)
    chunks.append(buf[pos:])
    return b"".join(chunks)


# ---------------------------------------------------------------- the file ----

def _triangle_count(buf: bytes) -> int:
    return buf.count(b"<triangle") - buf.count(b"<triangles")


@dataclass
class Result:
    meshes: list          # a _Mesh per painted mesh object
    palette: list         # filament n's color at palette[n - 1]
    dropped: list         # slices kept in the source, made from the old paint and left out
    min_area: float
    max_passes: int
    written: Path | None = None

    @property
    def triangles(self) -> int:
        return sum(m.triangles for m in self.meshes)

    @property
    def changed(self) -> int:
        return sum(len(m.changed) for m in self.meshes)

    def total(self, name: str):
        return sum(getattr(m, name) for m in self.meshes)

    def from_to(self) -> Counter:
        """(old filament, new filament) -> triangles recolored so."""
        out: Counter = Counter()
        for m in self.meshes:
            out.update(m.from_to)
        return out

    def gone(self) -> list[int]:
        """Filaments painted before and nowhere after: every bit of them was an island."""
        before = set().union(*(m.before for m in self.meshes))
        after = set().union(*(m.after for m in self.meshes))
        return sorted(before - after)

    def name(self, f: int) -> str:
        if f == 0:
            return "the part's filament"
        color = self.palette[f - 1] if 0 < f <= len(self.palette) else ""
        return f"{f} {color}".strip()


def despeckle(src: Path, dst: Path | None, min_area: float = MIN_AREA, max_passes: int = MAX_PASSES) -> Result:
    """Merge every same-color paint island under `min_area` mm² in `src` into its neighbor's color, and
    write the result to `dst` (None: only report). It is written as partial - <name>, read back, and
    only then put in place; `src` is never written."""
    if not min_area > 0:
        raise Refusal(f"the smallest island to keep must be a positive area in mm², not {min_area:g}")
    if dst is not None and dst.resolve() == src.resolve():
        raise Refusal(f"{src.name} would be written over: give the despeckled file another name")
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise Refusal(f"{src.name} isn't a 3MF (not a zip archive)") from None
    with z:
        layout = _layout(z)
        infos = [copy.copy(i) for i in z.infolist()]   # writestr() changes the ZipInfo it's given
        meshes, new_models = [], {}
        for entry in (i.filename for i in infos if i.filename.lower().endswith(".model")):
            buf = z.read(entry)
            if PAINT.search(buf) is None:
                continue
            found = [m for oid, o in _objects(buf, entry).items() if o.mesh
                     for m in [_mesh(buf, entry, oid, o, layout, min_area, max_passes)] if m is not None]
            meshes += found
            if dst is not None and any(m.changed for m in found):
                new = buf
                for m in sorted(found, key=lambda m: -m.span[0]):   # from the end, so the spans before stay put
                    if m.changed:
                        new = _rewrite(new, m)
                if _triangle_count(new) != _triangle_count(buf):
                    raise Refusal(f"{entry}: rewriting the paint changed how many triangles it has")
                new_models[entry] = new
            del buf
        if not meshes:
            raise Refusal(f"{src.name} has no paint (paint_color or slic3rpe:mmu_segmentation): nothing to despeckle")
        changes = any(m.changed for m in meshes)   # none: nothing to write, and a slice in the file still holds
        result = Result(meshes, layout.palette, [i.filename for i in infos if changes and GCODE.match(i.filename)],
                        min_area, max_passes)
        if dst is None or not changes:
            return result
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = partial(dst)
        try:
            with zipfile.ZipFile(tmp, "w") as out:
                for info in infos:
                    if info.filename in result.dropped:
                        continue
                    data = new_models.get(info.filename)
                    if data is None:
                        out.writestr(copy.copy(info), z.read(info.filename))
                    else:
                        out.writestr(copy.copy(info), data, compress_type=zipfile.ZIP_DEFLATED)
                out.comment = z.comment if z.comment.startswith(STAMP) else STAMP
            _check(tmp, infos, result.dropped, new_models)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    land(tmp, dst)
    result.written = dst
    return result


def _check(tmp: Path, infos: list, dropped: list, new_models: dict) -> None:
    """The written file reads back whole (every entry's CRC checked): the source's entries less the
    slices left out, the same bytes but the models rewritten, and those as they were made."""
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad:
            raise Refusal(f"the written file doesn't read back: {bad} is damaged")
        got = {i.filename: i.CRC for i in z.infolist()}
    want = {i.filename: (zlib.crc32(new_models[i.filename]) if i.filename in new_models else i.CRC)
            for i in infos if i.filename not in dropped}
    if list(got) != list(want):
        raise Refusal(f"the written file's entries aren't the source's: {sorted(set(want) ^ set(got))}")
    for name, crc in want.items():
        if got[name] != crc:
            raise Refusal(f"{name} isn't as it should be in the written file")


def report(r: Result) -> list[str]:
    """Lines saying what despeckling found and did."""
    under = f"under {r.min_area:g} mm²"
    parts = len(r.meshes)
    lines = [f"paint     : {r.triangles:,} triangles in {parts} painted part{'s' * (parts != 1)}",
             f"islands   : {r.total('islands_before'):,} {under}, {r.total('island_area'):,.1f} mm² in all"]
    if r.changed:
        passes = max(m.passes for m in r.meshes)
        lines.append(f"recolored : {r.changed:,} triangles ({100 * r.changed / r.triangles:.1f}%), "
                     f"{r.total('changed_area'):,.1f} mm², in {passes} pass{'es' * (passes != 1)}")
        if r.total("split_changed"):
            lines.append(f"{'':12}{r.total('split_changed'):,} of them were split between colors, and are one color now")
        moves = r.from_to().most_common()
        width = max(len(r.name(a)) for (a, _), _ in moves[:FROM_TO_SHOWN])
        for i, ((a, b), k) in enumerate(moves[:FROM_TO_SHOWN]):
            lines.append(f"{'from -> to: ' if i == 0 else '':12}{r.name(a):{width}} -> {r.name(b)}: {k:,}")
        if len(moves) > FROM_TO_SHOWN:
            rest = sum(k for _, k in moves[FROM_TO_SHOWN:])
            lines.append(f"{'':12}{len(moves) - FROM_TO_SHOWN} more pairs: {rest:,}")
    left = r.total("islands_after")
    lines.append(f"left      : {left:,} islands {under}" + (f" (stopped after {r.max_passes} passes)" if left else ""))
    for f in r.gone():
        lines.append(f"⚠ filament {r.name(f)} is painted nowhere now: all of it was islands {under}")
    for name in r.dropped:
        lines.append(f"left out  : {name}, sliced from the old paint (slice it again)")
    return lines

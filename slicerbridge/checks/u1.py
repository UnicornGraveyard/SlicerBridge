#!/usr/bin/env python3
"""
checks/u1.py -- the audit every Snapmaker U1 file passes before it's written, and
the 3MF readers the other checks share (paint decoder, plates, stamps).

    slicerbridge verify "out/My Model/My Model - Snapmaker U1.3mf" --source my-model.3mf

Checks a U1 file for:

  1. WHITE-BLOB BUG — the big one. bl2u1/u1convert.com read the filament palette
     from Metadata/slice_info.config. Bambu projects saved *unsliced* have an
     empty stub there, so the converter finds no filaments and silently pads the
     output with 4 dummy WHITE PLA entries. The file opens fine and prints as a
     white blob with the ColorStacking gone. Every source in this project was
     saved unsliced, so anything converted on the website is suspect.

  2. Printer identity actually retargeted to the U1.
  3. Filament count fits the U1's 4 extruders.
  4. paint_color mesh data still present (ColorStacking survived).

Given the Bambu source, against what the u1 engine should have made
of it: the slots the source actually paints or assigns, compacted to 1..K.

  5. Palette: extruder k carries the color of the k-th used source slot.
  6. Every paint state and every extruder/filament number is <= 4, and the
     part/object extruders are the source's, mapped.
  7. Painted faces in == out.
  8. Paint leaves per extruder in == out after the mapping. Every paint string
     is decoded into its leaves on both sides and counted per state -- the proof
     the colors landed on the right extruders, not just that paint exists.

And on its own, whatever the source:

  9. Every printed vertex, through its build transform, is inside its own plate
     of the U1 bed and under the max height. Snapmaker Orca lays plates out in a
     grid, each 1.2 beds apart, and won't slice a plate whose object hangs over
     the edge ("no object is fully inside the print volume"). A model bigger than
     the bed is converted at full size anyway -- it gets a ⚠ with the scale that
     would fit, for whoever prints it. A prime tower footprint that may overlap
     the model is flagged with a ⚠ too -- only slicing in Snapmaker Orca knows the
     tower's real size.
 10. The version stamp (Application, X-BBL-Client-Version) is the U1 template's
     (Bambu Studio 2.2.1, the version the project settings say too) or Snapmaker
     Orca's own, not the Bambu Studio 2.6-2.8 that saved the source -- newer than
     Snapmaker Orca 2.4.0.
 11. Size unchanged: each plate's printed parts measure the same as in the
     source, to 0.01 mm (width and depth swap if it was turned 90 degrees).

Standard library only: the paint decoder here is written from the format, not
borrowed from Panda2Prusa, so the conversion engine isn't checking itself.
"""
from __future__ import annotations

import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "u1"  # bl2u1's U1 templates (GPLv3)
APPLICATION = re.compile(r'<metadata name="Application">([^<]*)</metadata>')
CLIENT_VERSION = re.compile(r'key="X-BBL-Client-Version" value="([^"]*)"')
WHITE = {"#FFFFFF", "#FFFFFFFF"}
TOOLS = 4
PLATE_STRIDE = 1.2        # Bambu's PartPlate layout: plates one bed + a fifth of a bed apart
EPS = 1e-4
TOWER_DEPTH_GUESS = 40.0  # mm; only the slicer knows

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
P_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
VERTEX_OR_OBJECT = re.compile(rb'<object\b[^>]*?\bid="(\d+)"|<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"')

PAINT = re.compile(rb' paint_color="([0-9A-Fa-f]*)"')
EXTRUDER = re.compile(r'key="extruder" value="(\d+)"')
ANY_FILAMENT = re.compile(r'key="(?:extruder|[a-z_]+_filament)" value="(\d+)"')


def rgba(c: str) -> str:
    c = c.strip().upper()
    return c + "FF" if len(c) == 7 else c


def palette(z: zipfile.ZipFile) -> list[str]:
    cfg = json.loads(z.read("Metadata/project_settings.config").decode())
    return [c.upper() for c in cfg.get("filament_colour", [])]


def paint_codes(z: zipfile.ZipFile) -> Counter:
    """paint_color string -> number of faces carrying it, over every mesh part."""
    codes: Counter = Counter()
    for name in z.namelist():
        if not name.lower().endswith(".model"):
            continue
        with z.open(name) as f:
            tail = b""
            while True:
                chunk = f.read(8_000_000)
                if not chunk:
                    break
                buf = tail + chunk
                cut = buf.rfind(b"/>")  # only scan whole elements; the rest waits
                if cut < 0:
                    tail = buf
                    continue
                for m in PAINT.finditer(buf, 0, cut):
                    codes[m.group(1)] += 1
                tail = buf[cut:]
            for m in PAINT.finditer(tail):
                codes[m.group(1)] += 1
    return codes


def paint_leaves(code: bytes) -> Counter:
    """Count the leaf states of one TriangleSelector paint string.

    Bits are read from the END of the hex string, LSB first in each nibble --
    which is int(code, 16) read from bit 0 up:

        node := split:2 ( state:2 [ext:4 if state == 3 -> state = ext + 3]
                        | side:2 node * (split + 1) )
    """
    v = int(code, 16)
    nbits = 4 * len(code)
    pos, todo, out = 0, 1, Counter()
    while todo:
        todo -= 1
        if pos + 2 > nbits:
            raise ValueError(f"truncated paint string {code[:40]!r}")
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
        raise ValueError(f"data after the root node in paint string {code[:40]!r}")
    return out


def histogram(codes: Counter) -> Counter:
    hist: Counter = Counter()
    for code, n in codes.items():
        if code:
            for state, k in paint_leaves(code).items():
                hist[state] += k * n
    return hist


def model_settings(z: zipfile.ZipFile) -> str:
    return z.read("Metadata/model_settings.config").decode()


def _tf(text: str | None) -> tuple:
    vals = [float(v) for v in (text or "").split()]
    return tuple(vals) if len(vals) == 12 else (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def _then(a: tuple, b: tuple) -> tuple:
    """Transform a, then b (3MF row-vector convention: p * A * B)."""
    out = []
    for r in range(4):
        row = a[3 * r: 3 * r + 3]
        for c in range(3):
            v = row[0] * b[c] + row[1] * b[3 + c] + row[2] * b[6 + c]
            out.append(v + (b[9 + c] if r == 3 else 0))
    return tuple(out)


def stamp(z: zipfile.ZipFile) -> tuple[str | None, str | None]:
    """(Application, X-BBL-Client-Version) a 3MF says it was saved by."""
    app = APPLICATION.search(z.read("3D/3dmodel.model")[:20_000].decode(errors="replace"))
    client = CLIENT_VERSION.search(z.read("Metadata/slice_info.config").decode(errors="replace"))
    return (app.group(1) if app else None), (client.group(1) if client else None)


def template_stamps() -> set | None:
    """Stamps of the U1 templates (Snapmaker Orca's own U1 projects)."""
    found = set()
    for name in ("u1_template.3mf", "u1_template_supports.3mf"):
        if (DATA / name).exists():
            with zipfile.ZipFile(DATA / name) as z:
                found.add(stamp(z))
    return found or None


def plate_area(index: int, count: int, area: tuple) -> tuple:
    """Bed rectangle (x0, y0, x1, y1) of plate `index` (0-based) of `count` in global coordinates."""
    x0, y0, x1, y1 = area
    v = count ** 0.5
    cols = round(v) + 1 if v > round(v) else round(v)
    row, col = divmod(index, cols)
    ox, oy = col * (x1 - x0) * PLATE_STRIDE, -row * (y1 - y0) * PLATE_STRIDE
    return x0 + ox, y0 + oy, x1 + ox, y1 + oy


def plate_boxes(z: zipfile.ZipFile) -> list[tuple]:
    """[(plate number, lo xyz, hi xyz)] of every plate's printed parts, as placed."""
    ms = ET.fromstring(model_settings(z))
    subtypes = {o.get("id"): {p.get("id"): p.get("subtype", "normal_part") for p in o.findall("part")}
                for o in ms.findall("object")}
    plates = []
    for p in ms.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        objs = [m.get("value") for inst in p.findall("model_instance")
                for m in inst.findall("metadata") if m.get("key") == "object_id"]
        plates.append((int(meta.get("plater_id", len(plates) + 1)), objs))

    root = ET.fromstring(z.read("3D/3dmodel.model"))
    comps = {}
    for o in root.iter(CORE + "object"):
        cs = [(c.get(P_PATH, "/3D/3dmodel.model").lstrip("/"), c.get("objectid"), _tf(c.get("transform")))
              for c in o.iter(CORE + "component")]
        if o.find(CORE + "mesh") is not None:
            cs.append(("3D/3dmodel.model", o.get("id"), _tf(None)))
        comps[o.get("id")] = cs
    items = {i.get("objectid"): _tf(i.get("transform")) for i in root.iter(CORE + "item")}

    # Which mesh, under which full transform, lands on which plate. Printed parts only.
    want: dict = {}
    for idx, (number, objs) in enumerate(plates):
        for oid in objs:
            for path, mesh_id, ctf in comps.get(oid, []):
                if subtypes.get(oid, {}).get(mesh_id, "normal_part") != "normal_part":
                    continue
                want.setdefault((path, mesh_id), []).append((idx, _then(ctf, items.get(oid, _tf(None)))))

    lo = [[float("inf")] * 3 for _ in plates]
    hi = [[float("-inf")] * 3 for _ in plates]
    for path in sorted({p for p, _ in want}):
        with z.open(path) as f:
            cur, tail = None, b""
            while True:
                chunk = f.read(8_000_000)
                buf = tail + chunk
                cut = buf.rfind(b">") + 1 if chunk else len(buf)
                for m in VERTEX_OR_OBJECT.finditer(buf, 0, cut):
                    if m.group(1) is not None:
                        cur = want.get((path, m.group(1).decode()))
                    elif cur:
                        x, y, zz = float(m.group(2)), float(m.group(3)), float(m.group(4))
                        for idx, t in cur:
                            q = (x * t[0] + y * t[3] + zz * t[6] + t[9],
                                 x * t[1] + y * t[4] + zz * t[7] + t[10],
                                 x * t[2] + y * t[5] + zz * t[8] + t[11])
                            for k in range(3):
                                if q[k] < lo[idx][k]:
                                    lo[idx][k] = q[k]
                                if q[k] > hi[idx][k]:
                                    hi[idx][k] = q[k]
                tail = buf[cut:]
                if not chunk:
                    break
    return [(number, lo[idx], hi[idx]) for idx, (number, _objs) in enumerate(plates)]


def on_plates(z: zipfile.ZipFile, cfg: dict) -> tuple[list[str], list[str], list[str], list[tuple]]:
    """Check 9: (info lines, problems, warnings, plate boxes) about where every plate's print sits."""
    pts = [tuple(float(c) for c in p.split("x")) for p in cfg["printable_area"]]
    area = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    height = float(cfg["printable_height"])
    boxes = plate_boxes(z)
    bw, bd = area[2] - area[0], area[3] - area[1]

    info, problems, warnings = [], [], []
    tower = cfg.get("enable_prime_tower") == "1"
    tw, brim = float(cfg.get("prime_tower_width") or 30), float(cfg.get("prime_tower_brim_width") or 0)
    txs, tys = cfg.get("wipe_tower_x") or [], cfg.get("wipe_tower_y") or []
    for idx, (number, a, b) in enumerate(boxes):
        bx0, by0, bx1, by1 = plate_area(number - 1, len(boxes), area)
        label = f"plate {number}" if len(boxes) > 1 else "plate"
        if a[0] == float("inf"):
            problems.append(f"{label}: no printed part found to place")
            continue
        info.append(f"{label}: x {a[0] - bx0:.1f}..{b[0] - bx0:.1f} y {a[1] - by0:.1f}..{b[1] - by0:.1f} "
                    f"z {a[2]:.1f}..{b[2]:.1f} on its {bx1 - bx0:g} x {by1 - by0:g} x {height:g} bed")
        sx, sy, sz = (b[k] - a[k] for k in range(3))
        fits = sz <= height + EPS and ((sx <= bw + EPS and sy <= bd + EPS) or (sy <= bw + EPS and sx <= bd + EPS))
        if not fits:
            # Bigger than the bed: converted at full size on purpose -- scaling is
            # the call of whoever prints it. It only has to sit on its own plate.
            best = max(min(bw / sx, bd / sy), min(bw / sy, bd / sx))
            pct = math.floor(min(best, height / sz) * 1000) / 10
            warnings.append(f"{label} is bigger than the U1 bed ({sx:.1f} x {sy:.1f} x {sz:.1f} mm): "
                            f"converted at full size, fits at {pct:g}% or less -- scale it in Snapmaker Orca to print")
            if not (bx0 <= (a[0] + b[0]) / 2 <= bx1 and by0 <= (a[1] + b[1]) / 2 <= by1 and a[2] >= -0.05):
                problems.append(f"{label} isn't on its own U1 plate at all")
            continue
        if (a[0] < bx0 - EPS or a[1] < by0 - EPS or b[0] > bx1 + EPS or b[1] > by1 + EPS
                or b[2] > height + EPS or a[2] < -0.05):
            problems.append(f"{label} hangs off its U1 plate -- Snapmaker Orca won't slice it "
                            f"(x {a[0] - bx0:.1f}..{b[0] - bx0:.1f}, y {a[1] - by0:.1f}..{b[1] - by0:.1f}, "
                            f"z to {b[2]:.1f}; the bed is {bx1 - bx0:g} x {by1 - by0:g} x {height:g})")
        if tower and idx < len(txs) and idx < len(tys):
            tx, ty = float(txs[idx]) + bx0 - area[0], float(tys[idx]) + by0 - area[1]
            t = (tx - brim, ty - brim, tx + tw + brim, ty + TOWER_DEPTH_GUESS + brim)
            if t[0] < b[0] and t[2] > a[0] and t[1] < b[1] and t[3] > a[1]:
                warnings.append(f"{label}: the prime tower at {float(txs[idx]):.0f},{float(tys[idx]):.0f} may touch "
                                f"the model's footprint -- slice it in Snapmaker Orca and look")
    return info, problems, warnings, boxes


def source_facts(src: Path) -> dict:
    """What the u1 engine should have made of this source."""
    with zipfile.ZipFile(src) as z:
        pal = [rgba(c) for c in palette(z)]
        codes = paint_codes(z)
        ms = model_settings(z)
    hist = histogram(codes)
    exts = [int(e) for e in EXTRUDER.findall(ms) if e != "0"]
    used = sorted({s for s in hist if s} | set(exts))
    if not used and codes:
        used = [1]  # painted but unassigned: Bambu prints it on filament 1
    mapping = {old: new for new, old in enumerate(used, start=1)}
    return {"palette": pal, "codes": codes, "hist": hist, "exts": exts,
            "used": used, "mapping": mapping}


def audit(f: Path, src: Path | None) -> tuple[list[str], list[str]]:
    """(info lines, problems) for one U1 file, against its source if given."""
    with zipfile.ZipFile(f) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config").decode())
        pal = palette(z)
        printer = cfg.get("printer_settings_id", "")
        codes = paint_codes(z)
        ms = model_settings(z)
        si = ET.fromstring(z.read("Metadata/slice_info.config").decode())
        si_fils = si.findall(".//filament")
    painted = sum(codes.values())

    problems: list[str] = []
    info = [
        f"printer   : {printer}",
        f"palette   : {pal}",
        f"slice_info: {len(si_fils)} filament entries",
        f"paint_color attrs: {painted:,}",
    ]

    # 1. the white-blob bug
    real = [c for c in pal if c not in WHITE]
    if pal and not real:
        problems.append(
            "WHITE-BLOB: every filament is white. The ColorStacking is GONE. "
            "This is the u1convert.com unsliced-project bug. DO NOT PUBLISH."
        )

    # 2. printer identity
    if "U1" not in printer:
        problems.append(f"printer_settings_id is '{printer}' — not retargeted to the U1")

    # 3. extruder budget
    if len(pal) > TOOLS:
        problems.append(f"{len(pal)} filaments — the U1 only has {TOOLS} extruders")

    # 4. color painting survived
    if painted == 0:
        problems.append("no paint_color data — this model has no color painting at all")

    # Finder can see it: iCloud's Documents sync flags dot-named temp files as hidden,
    # and earlier dot-named temp files carried that flag onto the finished file.
    if getattr(f.stat(), "st_flags", 0) & 0x8000:  # UF_HIDDEN
        problems.append("hidden in Finder (macOS hidden flag) -- chflags nohidden, or re-convert")

    # 9. on the bed, every plate on its own U1 plate
    with zipfile.ZipFile(f) as z:
        bed_info, bed_problems, bed_warnings, boxes = on_plates(z, cfg)
        file_stamp = stamp(z)
    info += bed_info + [f"⚠ {w}" for w in bed_warnings]
    problems += bed_problems

    # 10. version stamp Snapmaker Orca will read
    wanted = template_stamps()
    info.append(f"stamp     : Application {file_stamp[0]!r}, client version {file_stamp[1]!r}")
    if wanted is None:
        info.append(f"⚠ no U1 template at {DATA} -- check 10 skipped")
    elif file_stamp not in wanted and not (file_stamp[0] or "").startswith("Snapmaker_Orca-"):
        problems.append(f"version stamp {file_stamp} isn't the U1 template's {sorted(wanted)[0]} -- "
                        f"Snapmaker Orca reads it as the file's version")

    try:
        hist = histogram(codes)
    except ValueError as e:
        problems.append(f"unreadable paint: {e}")
        hist = Counter()

    # 6a. tool numbers, whatever the source
    states = sorted(s for s in hist if s)
    numbers = sorted({int(n) for n in ANY_FILAMENT.findall(ms) if n != "0"})
    if [s for s in states if s > TOOLS] or [n for n in numbers if n > TOOLS]:
        problems.append(f"paint states {states} / extruder numbers {numbers} — the U1 has extruders 1-{TOOLS}")

    if src is None:
        info.append("source    : not given — checks 5-8 skipped")
        return info, problems

    # 11. size unchanged, plate by plate
    with zipfile.ZipFile(src) as z:
        src_boxes = {n: tuple(hi[k] - lo[k] for k in range(3)) for n, lo, hi in plate_boxes(z)}
    for n, lo, hi in boxes:
        got = tuple(hi[k] - lo[k] for k in range(3))
        want = src_boxes.get(n)
        label = f"plate {n}" if len(boxes) > 1 else "plate"
        if want is None:
            problems.append(f"{label} has no counterpart in the source")
        elif not any(all(abs(g - w) <= 0.01 for g, w in zip(got, cand))
                     for cand in (want, (want[1], want[0], want[2]))):
            problems.append(f"{label} is {got[0]:.2f} x {got[1]:.2f} x {got[2]:.2f} mm, the source is "
                            f"{want[0]:.2f} x {want[1]:.2f} x {want[2]:.2f} -- the model was resized")
        else:
            info.append(f"size      : {label} {got[0]:.1f} x {got[1]:.1f} x {got[2]:.1f} mm, as designed")

    sf = source_facts(src)
    mapping, used = sf["mapping"], sf["used"]
    info.append(f"source    : uses slots {used} -> extruders {[mapping[s] for s in used]}")
    shown = ", ".join(f"E{s}:{hist[s]:,}" for s in sorted(hist))
    info.append(f"paint leaves: {shown}")

    if len(used) > TOOLS:
        problems.append(f"the source uses {len(used)} slots {used} — more than the U1's {TOOLS} extruders")
        return info, problems

    # 5. palette: extruder k is the k-th used slot's color
    want = [sf["palette"][s - 1] for s in used]
    if [rgba(c) for c in pal[: len(want)]] != want:
        problems.append(f"palette differs from the source's used slots: want {want}, file has {pal[:len(want)]}")

    # 6b. part/object extruders are the source's, mapped
    out_exts = Counter(int(e) for e in EXTRUDER.findall(ms) if e != "0")
    want_exts = Counter(mapping.get(e, e) for e in sf["exts"])
    if out_exts != want_exts:
        problems.append(f"part/object extruders {dict(out_exts)}, want the source's mapped {dict(want_exts)}")

    # 7. painted faces in == out
    src_painted = sum(sf["codes"].values())
    if painted != src_painted:
        problems.append(f"painted faces {painted:,}, source has {src_painted:,}")

    # 8. paint leaves per extruder in == out after the mapping
    want_hist: Counter = Counter()
    for s, n in sf["hist"].items():
        want_hist[mapping.get(s, s) if s else 0] += n
    if hist != want_hist:
        diff = {s: (want_hist.get(s, 0), hist.get(s, 0)) for s in set(want_hist) | set(hist)
                if want_hist.get(s, 0) != hist.get(s, 0)}
        problems.append(f"paint leaves per extruder don't match the source after mapping "
                        f"(extruder: want, got) {dict(sorted(diff.items()))} — colors are on the wrong extruders")
    return info, problems



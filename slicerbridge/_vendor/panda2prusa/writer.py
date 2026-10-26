"""Write a PrusaSlicer-compatible .3mf from a BambuProject.

Strategy: emit one *merged mesh* per build object — exactly the shape PrusaSlicer's
own writer produces. Each Bambu part (component) becomes a contiguous triangle range
in the merged mesh, recorded as a ``<volume firstid=.. lastid=..>`` entry in
``Metadata/Slic3r_PE_model.config`` carrying the part's name and extruder. This is
the only representation in which PrusaSlicer honors per-part extruder assignments;
component-based objects collapse to a single volume on import and lose them.

Component transforms are baked into the vertex coordinates (volumes therefore need
no matrix metadata), build-item transforms are preserved verbatim, and per-triangle
paint data travels with its triangle: ``paint_color`` values are kept byte-for-byte
under the ``slic3rpe:mmu_segmentation`` name, since Bambu Studio forked PrusaSlicer's
TriangleSelector and the RLE bitstream format is shared.
"""

from __future__ import annotations

import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import lxml.etree as ET

from . import ns
from .paint import transform_states, used_states, PaintError
from .reader import BambuProject, Part, ROOT_MODEL
from .settings import FILAMENTS, Warnings, translate
from . import autobrim
from .transform import Transform

# Attributes to strip from triangles (production + bambu extensions).
_DROP_ATTRS = {
    ns.q(ns.PRODUCTION, "UUID"),
    ns.q(ns.PRODUCTION, "path"),
    "paint_seam",
    "paint_supports",
}

CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
    ' <Default Extension="png" ContentType="image/png"/>\n'
    "</Types>\n"
)


@dataclass
class WriteStats:
    objects_written: int = 0
    volumes_written: int = 0
    build_items_written: int = 0
    painted_triangles: int = 0
    plates_included: Optional[list] = None
    extruder_map: Optional[dict] = None  # {bambu filament -> prusa extruder} if remapped
    warnings: list = field(default_factory=list)  # what could not be converted, one line each


@dataclass
class _Volume:
    firstid: int
    lastid: int
    leaf_oid: str
    name: str = ""
    extruder: Optional[str] = None
    volume_type: str = "ModelPart"  # PrusaSlicer's name, see _VOLUME_TYPES
    settings: dict = field(default_factory=dict)  # the Bambu part's metadata, settings included
    leaf_idx: int = -1  # index into _MergedObject.leaves


@dataclass
class _MergedObject:
    """A build object planned for merging; its mesh is streamed out by ``_write_object``."""

    name: str
    leaves: list  # [(leaf_key, cumulative Transform)]
    volumes: list = field(default_factory=list)
    painted: int = 0
    paint_codes: set = field(default_factory=set)  # distinct non-empty paint strings


def _key(path: str, objectid: str) -> tuple[str, str]:
    return (path or ROOT_MODEL, objectid)


def _collect_objects(project: BambuProject):
    """Map (model_path, object_id) -> lxml <object> element across all files."""
    objs = {}
    src_path = {}
    trees = {ROOT_MODEL: project.root_tree}
    trees.update(project.object_trees)
    for path, tree in trees.items():
        for obj in tree.getroot().iter(ns.q(ns.CORE, "object")):
            oid = obj.get("id")
            if oid is not None:
                objs[_key(path, oid)] = obj
                src_path[_key(path, oid)] = path
    return objs, src_path


def _component_target(comp, current_path: str) -> tuple[str, str]:
    p = comp.get(ns.q(ns.PRODUCTION, "path"))
    from .reader import _normalize_path

    path = _normalize_path(p) if p else current_path
    return _key(path, comp.get("objectid"))


def _flatten_leaves(key, objs, src_path, cum=None, _depth=0):
    """DFS a component tree into mesh leaves: [(leaf_key, cumulative Transform)]."""
    if cum is None:
        cum = Transform.identity()
    obj = objs.get(key)
    if obj is None or _depth > 16:
        return []
    leaves = []
    if obj.find(ns.q(ns.CORE, "mesh")) is not None:
        leaves.append((key, cum))
    comps = obj.find(ns.q(ns.CORE, "components"))
    if comps is not None:
        for comp in comps.findall(ns.q(ns.CORE, "component")):
            t = Transform.parse(comp.get("transform"))
            leaves.extend(
                _flatten_leaves(
                    _component_target(comp, src_path[key]),
                    objs,
                    src_path,
                    t.compose(cum),
                    _depth + 1,
                )
            )
    return leaves


def _fmt(v: float) -> str:
    """Shortest round-trip decimal for a coordinate."""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


# Bambu Studio / OrcaSlicer part subtypes -> PrusaSlicer volume types.
_VOLUME_TYPES = {
    "normal_part": "ModelPart",
    "modifier_part": "ParameterModifier",
    "negative_part": "NegativeVolume",
    "support_enforcer": "SupportEnforcer",
    "support_blocker": "SupportBlocker",
}


def _volume_type(subtype: str) -> str:
    """PrusaSlicer volume type for a Bambu/Orca part subtype.

    Subtypes PrusaSlicer has no counterpart for (OrcaSlicer's precise seam volumes)
    become modifiers without settings: kept, but neither printed nor changing the print.
    """
    return _VOLUME_TYPES.get(subtype, "ParameterModifier")


def _plan_object(root_key, objs, src_path, parts: list) -> Optional[_MergedObject]:
    """Plan merging a build object's component tree into a single mesh.

    Records the triangle range, name and extruder of each volume plus the paint codes
    used, without copying any geometry; ``_write_object`` streams the merged mesh.
    """
    leaves = _flatten_leaves(root_key, objs, src_path)
    if not leaves:
        return None

    # Match Bambu parts (name/extruder) to leaves: by id when possible, else by order.
    part_by_id = {p.id: p for p in parts}
    def part_for(leaf_idx: int, leaf_oid: str) -> Optional[Part]:
        if leaf_oid in part_by_id:
            return part_by_id[leaf_oid]
        if len(parts) == len(leaves):
            return parts[leaf_idx]
        return None

    merged = _MergedObject(name=objs[root_key].get("name") or "", leaves=leaves)
    tri_count = 0
    for leaf_idx, (leaf_key, _xf) in enumerate(leaves):
        leaf = objs[leaf_key]
        tris = leaf.find(_MESH).find(_TRIANGLES)

        firstid = tri_count
        if tris is not None:
            for t in tris.iterchildren(_TRIANGLE):
                code = t.get("paint_color")
                if code is not None:
                    merged.painted += 1
                    if code:
                        merged.paint_codes.add(code)
                tri_count += 1

        if tri_count > firstid:
            part = part_for(leaf_idx, leaf_key[1])
            merged.volumes.append(
                _Volume(
                    firstid=firstid,
                    lastid=tri_count - 1,
                    leaf_oid=leaf_key[1],
                    name=(part.name if part else "") or leaf.get("name", ""),
                    extruder=part.extruder if part else None,
                    volume_type=_volume_type(part.subtype) if part else "ModelPart",
                    settings=part.settings if part else {},
                    leaf_idx=leaf_idx,
                )
            )
    return merged


# -- streaming XML output -----------------------------------------------------
# The merged meshes are written straight into the zip entry instead of being built
# as a second lxml tree: for large painted models that copy (plus its serialized
# bytes) cost more memory than the parsed source itself. The output is formatted
# exactly as lxml's pretty printer would format the equivalent tree.
_MESH = ns.q(ns.CORE, "mesh")
_VERTICES = ns.q(ns.CORE, "vertices")
_VERTEX = ns.q(ns.CORE, "vertex")
_TRIANGLES = ns.q(ns.CORE, "triangles")
_TRIANGLE = ns.q(ns.CORE, "triangle")
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_RESOURCES_MARK = "panda2prusa:resources"

_ATTR_ESCAPES = str.maketrans(
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}
)
_needs_escape = re.compile('[&<>"\n\r\t]').search


def _esc(value: str) -> str:
    """Escape an attribute value the way libxml2 serializes it."""
    return value.translate(_ATTR_ESCAPES) if _needs_escape(value) else value


class _ChunkedWriter:
    """Batch many small string writes into large UTF-8 writes on a binary stream."""

    def __init__(self, raw, limit: int = 1 << 20):
        self._raw, self._limit = raw, limit
        self._buf: list = []
        self._size = 0

    def write(self, text: str) -> None:
        self._buf.append(text)
        self._size += len(text)
        if self._size >= self._limit:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self._raw.write("".join(self._buf).encode("utf-8"))
            self._buf.clear()
            self._size = 0


def _extra_triangle_attrs(t, recode) -> tuple[str, str]:
    """Serialize a triangle's attributes beyond v1..v3 as (xmlns declarations, attributes)."""
    out: dict = {}
    for attr, val in t.attrib.items():
        if attr in ("v1", "v2", "v3") or attr in _DROP_ATTRS:
            continue
        if attr.startswith("{" + ns.BAMBU + "}"):
            continue
        if attr == "paint_color":
            out[_paint_attr()] = val
        else:
            out[attr] = val
    if recode is not None and out.get(_paint_attr()):
        out[_paint_attr()] = recode(out[_paint_attr()])

    decls, parts = [], []
    declared: dict = {}
    for attr, val in out.items():
        if attr.startswith("{"):
            uri, local = attr[1:].split("}", 1)
            if uri == ns.SLIC3RPE:
                prefix = "slic3rpe"
            elif uri == _XML_NS:
                prefix = "xml"
            else:
                prefix = declared.get(uri)
                if prefix is None:
                    prefix = declared[uri] = f"ns{len(declared)}"
                    decls.append(f' xmlns:{prefix}="{_esc(uri)}"')
            attr = f"{prefix}:{local}"
        parts.append(f' {attr}="{_esc(val)}"')
    return "".join(decls), "".join(parts)


def _write_object(w, mo: _MergedObject, oid: str, objs, recode, placement=None) -> Optional[float]:
    """Stream one merged <object>: all leaves' vertices, then all their triangles.

    Returns the height above the bed of the object's printed parts as placed by
    ``placement`` (the Transform of its first build item), which is what a layer height
    profile spans. A part sunk into the bed is cut off at the bed, as in both slicers.
    """
    name = f' name="{_esc(mo.name)}"' if mo.name else ""
    w(f'    <object type="model"{name} id="{oid}">\n      <mesh>\n')

    # Only printed parts count for the height, not modifiers, negative volumes or blockers.
    printed = {vol.leaf_idx for vol in mo.volumes if vol.volume_type == "ModelPart"}
    pm = placement.m if placement is not None else None
    z_min, z_max = float("inf"), float("-inf")

    vbases = []
    vbase = 0
    opened = False
    for leaf_idx, (leaf_key, xf) in enumerate(mo.leaves):
        vbases.append(vbase)
        verts = objs[leaf_key].find(_MESH).find(_VERTICES)
        if verts is None:
            continue
        identity = xf.is_identity(tol=1e-9)
        measure = pm is not None and leaf_idx in printed
        for v in verts.iterchildren(_VERTEX):
            if not opened:
                w("        <vertices>\n")
                opened = True
            if identity:
                x, y, z = _esc(v.get("x", "0")), _esc(v.get("y", "0")), _esc(v.get("z", "0"))
                if measure:
                    px, py, pz = float(v.get("x", "0")), float(v.get("y", "0")), float(v.get("z", "0"))
            else:
                px, py, pz = xf.apply_point(
                    float(v.get("x", "0")), float(v.get("y", "0")), float(v.get("z", "0"))
                )
                x, y, z = _fmt(px), _fmt(py), _fmt(pz)
            if measure:
                placed_z = px * pm[2] + py * pm[5] + pz * pm[8] + pm[11]
                z_min, z_max = min(z_min, placed_z), max(z_max, placed_z)
            w(f'          <vertex x="{x}" y="{y}" z="{z}"/>\n')
            vbase += 1
    w("        </vertices>\n" if opened else "        <vertices/>\n")

    opened = False
    for (leaf_key, xf), base in zip(mo.leaves, vbases):
        tris = objs[leaf_key].find(_MESH).find(_TRIANGLES)
        if tris is None:
            continue
        flip = xf.det3() < 0  # reflection: reverse winding to keep outward normals
        for t in tris.iterchildren(_TRIANGLE):
            if not opened:
                w("        <triangles>\n")
                opened = True
            v1, v2, v3 = t.get("v1"), t.get("v2"), t.get("v3")
            if flip:
                v1, v2 = v2, v1
            idx = f'v1="{int(v1) + base}" v2="{int(v2) + base}" v3="{int(v3) + base}"'
            if len(t.attrib) > 3:
                decls, extra = _extra_triangle_attrs(t, recode)
                w(f"          <triangle{decls} {idx}{extra}/>\n")
            else:
                w(f"          <triangle {idx}/>\n")
    w("        </triangles>\n" if opened else "        <triangles/>\n")
    w("      </mesh>\n    </object>\n")
    return z_max - max(z_min, 0.0) if z_max >= z_min else None


def _paint_attr():
    return ns.q(ns.SLIC3RPE, "mmu_segmentation")


def _used_filaments(project: BambuProject, merged: dict) -> set[int]:
    """Filament slots the planned objects use: object/volume extruders and paint states."""
    used: set[int] = set()
    paint_strings: set[str] = set()
    for key, mo in merged.items():
        so = project.settings_objects.get(key[1])
        # Only parts and modifiers print with a filament.
        printing = [vol for vol in mo.volumes if vol.volume_type in ("ModelPart", "ParameterModifier")]
        extruders = [so.extruder] if so else []
        extruders += [vol.extruder for vol in printing]
        # Filaments of the supports, set for the object or a part.
        for cfg in ([so.settings] if so else []) + [vol.settings for vol in printing]:
            extruders += [cfg.get(k) for k in FILAMENTS]
        for ext in extruders:
            if ext:
                try:
                    used.add(int(ext))
                except ValueError:
                    pass
        paint_strings |= mo.paint_codes
    for code in paint_strings:
        try:
            used |= used_states(code) - {0}
        except PaintError:
            pass
    used.discard(0)
    return used


def _compute_extruder_map(used: set[int]) -> Optional[dict]:
    """Map the filaments actually used to sequential extruders 1..K.

    Bambu AMS slots are sparse (a two-color print may use filaments 1 and 3); Prusa
    profiles only have extruders 1..N, so anything past the physical tool count is
    silently unprintable. Compacting {1, 3} -> {1: 1, 3: 2} makes a K-color model
    land on the first K tools. Returns None when already sequential.
    """
    if not used:
        return None
    mapping = {old: i for i, old in enumerate(sorted(used), start=1)}
    if all(old == new for old, new in mapping.items()):
        return None
    return mapping


def extruder_conflicts(used, mapping: dict) -> list[str]:
    """Describe extruders that more than one used filament would be mapped to.

    Filaments missing from ``mapping`` keep their own number, so mapping 2 -> 4 also
    conflicts with an unmapped filament 4.
    """
    by_extruder: dict[int, list[int]] = {}
    for f in sorted(used):
        by_extruder.setdefault(int(mapping.get(f, f)), []).append(f)
    problems = []
    for ext, fils in sorted(by_extruder.items()):
        if len(fils) > 1:
            names = ", ".join(map(str, fils[:-1])) + f" and {fils[-1]}"
            both = "both" if len(fils) == 2 else "all"
            problems.append(f"Filaments {names} are {both} mapped to extruder {ext}.")
    return problems


def _apply_extruder_map(merged: dict, mapping: dict):
    """Rewrite volume extruders; return a cached recoder for paint bitstreams."""
    for mo in merged.values():
        for vol in mo.volumes:
            if vol.extruder:
                try:
                    vol.extruder = str(mapping.get(int(vol.extruder), int(vol.extruder)))
                except ValueError:
                    pass

    recoded: dict[str, str] = {}

    def recode(code: str) -> str:
        if code not in recoded:
            try:
                recoded[code] = transform_states(code, mapping)
            except PaintError:
                recoded[code] = code  # leave unreadable paint untouched
        return recoded[code]

    return recode


def write_prusa_3mf(
    project: BambuProject,
    out_path: str,
    plates: Optional[list] = None,
    extruder_map: Optional[dict] = None,
    compact_extruders: bool = True,
) -> WriteStats:
    objs, src_path = _collect_objects(project)

    # Which build items (and thus root objects) to include, filtered by plate.
    selected_items = []
    for bi in project.build_items:
        plate = project.plate_of_object.get(bi.objectid)
        if plates is None or plate is None or plate in plates:
            selected_items.append(bi)

    # Plan each distinct build object once, preserving first-seen order.
    merged: dict[tuple[str, str], _MergedObject] = {}
    id_map: dict[tuple[str, str], str] = {}
    for bi in selected_items:
        key = _key(ROOT_MODEL, bi.objectid)
        if key in merged or key not in objs:
            continue
        so = project.settings_objects.get(bi.objectid)
        mo = _plan_object(key, objs, src_path, so.parts if so else [])
        if mo is not None:
            merged[key] = mo
            id_map[key] = str(len(id_map) + 1)

    # Renumber filament slots: an explicit map wins; otherwise compact sparse
    # slots so a K-color model lands on extruders 1..K.
    if extruder_map is not None:
        extruder_map = {int(k): int(v) for k, v in extruder_map.items()}
        problems = extruder_conflicts(_used_filaments(project, merged), extruder_map)
        if problems:
            raise ValueError(" ".join(problems))
        extruder_map = {k: v for k, v in extruder_map.items() if k != v} or None
    elif compact_extruders:
        extruder_map = _compute_extruder_map(_used_filaments(project, merged))
    recode = _apply_extruder_map(merged, extruder_map) if extruder_map else None

    # Build the small parts of the model as a tree, with a marker where the
    # streamed objects go.
    nsmap = {None: ns.CORE, "slic3rpe": ns.SLIC3RPE}
    model = ET.Element(ns.q(ns.CORE, "model"), nsmap=nsmap)
    model.set("unit", "millimeter")
    model.set("{http://www.w3.org/XML/1998/namespace}lang", "en-US")

    _add_metadata(model, project)

    resources = ET.SubElement(model, ns.q(ns.CORE, "resources"))
    if merged:
        resources.append(ET.Comment(_RESOURCES_MARK))

    build = ET.SubElement(model, ns.q(ns.CORE, "build"))
    items_written = 0
    for bi in selected_items:
        key = _key(ROOT_MODEL, bi.objectid)
        if key not in id_map:
            continue
        item = ET.SubElement(build, ns.q(ns.CORE, "item"))
        item.set("objectid", id_map[key])
        if bi.transform.strip():
            item.set("transform", bi.transform)
        item.set("printable", "1" if bi.printable else "0")
        items_written += 1

    skeleton = ET.tostring(
        model, xml_declaration=True, encoding="UTF-8", pretty_print=True
    ).decode("utf-8")
    head, mark, tail = skeleton.partition(f"    <!--{_RESOURCES_MARK}-->\n")

    # Where each object stands on the plate: its first build item.
    placements: dict = {}
    for bi in selected_items:
        key = _key(ROOT_MODEL, bi.objectid)
        if key in merged and key not in placements:
            try:
                placements[key] = Transform.parse(bi.transform)
            except ValueError:
                pass  # unreadable transform: no height, so no layer height profile or auto brim
    heights: dict = {}

    warnings = Warnings()
    model_config = _build_model_config(project, merged, id_map, extruder_map, warnings, objs, placements)
    layer_ranges = _build_layer_ranges(project, merged, id_map, extruder_map, warnings)

    # Assemble the package.
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", _rels_xml())
        # The model's size isn't known up front, so allow it to exceed 2 GiB.
        with z.open(ROOT_MODEL, "w", force_zip64=True) as raw:
            out = _ChunkedWriter(raw)
            out.write(head)
            if mark:
                for key, mo in merged.items():
                    heights[key] = _write_object(out.write, mo, id_map[key], objs, recode, placements.get(key))
            out.write(tail)
            out.flush()
        if model_config:
            z.writestr("Metadata/Slic3r_PE_model.config", model_config)
        profiles = _build_layer_height_profiles(project, merged, id_map, heights, warnings)
        if profiles:
            z.writestr("Metadata/Slic3r_PE_layer_heights_profile.txt", profiles)
        if layer_ranges:
            z.writestr("Metadata/Prusa_Slicer_layer_config_ranges.xml", layer_ranges)
        # Pass through a thumbnail if we have a plausible one.
        thumb = _pick_thumbnail(project)
        if thumb:
            z.writestr("Metadata/thumbnail.png", thumb)

    return WriteStats(
        objects_written=len(merged),
        volumes_written=sum(len(mo.volumes) for mo in merged.values()),
        build_items_written=items_written,
        painted_triangles=sum(mo.painted for mo in merged.values()),
        plates_included=plates,
        extruder_map=extruder_map,
        warnings=warnings.lines(),
    )


def _add_metadata(model, project: BambuProject) -> None:
    def meta(name, text):
        m = ET.SubElement(model, ns.q(ns.CORE, "metadata"))
        m.set("name", name)
        m.text = text

    meta("slic3rpe:Version3mf", "1")
    meta("slic3rpe:MmPaintingVersion", "1")
    # Carry over a title if present.
    title = ""
    for md in project.root_tree.getroot().findall(ns.q(ns.CORE, "metadata")):
        if md.get("name") == "Title" and md.text:
            title = md.text
    if title:
        meta("Title", title)
    meta("Application", "panda2prusa")


def _cfg_meta(parent, mtype: str, mkey: str, value: str) -> None:
    m = ET.SubElement(parent, "metadata")
    m.set("type", mtype)
    m.set("key", mkey)
    m.set("value", value)


def _build_model_config(
    project: BambuProject,
    merged: dict,
    id_map: dict,
    extruder_map: Optional[dict] = None,
    warnings: Optional[Warnings] = None,
    objs: Optional[dict] = None,
    placements: Optional[dict] = None,
) -> Optional[bytes]:
    """Emit PrusaSlicer object/volume config: names, types, extruders and settings.

    Volumes are identified by firstid/lastid triangle ranges over the merged mesh,
    matching PrusaSlicer's native writer, so extruder assignments survive import.
    Settings PrusaSlicer has no equivalent for are left out and added to ``warnings``.
    """
    if warnings is None:
        warnings = Warnings()

    def remap(f: int) -> int:
        return extruder_map.get(f, f) if extruder_map else f

    root = ET.Element("config")
    for key, mo in merged.items():
        old_id = key[1]
        so = project.settings_objects.get(old_id)
        obj_el = ET.SubElement(root, "object")
        obj_el.set("id", id_map[key])
        name = (so.name if so else "") or mo.name
        if name:
            _cfg_meta(obj_el, "object", "name", name)
        if so and so.extruder:
            ext = so.extruder
            if extruder_map:
                try:
                    ext = str(extruder_map.get(int(ext), int(ext)))
                except ValueError:
                    pass
            _cfg_meta(obj_el, "object", "extruder", ext)
        if so:
            obj_settings = translate(so.settings, name, warnings, True, project.nozzle_diameter, remap)
            if so.settings.get("brim_type") == "auto_brim":
                obj_settings.update(_auto_brim(project, so, mo, objs, (placements or {}).get(key), name, warnings))
            for k, v in obj_settings.items():
                _cfg_meta(obj_el, "object", k, v)
        for vol in mo.volumes:
            vol_el = ET.SubElement(obj_el, "volume")
            vol_el.set("firstid", str(vol.firstid))
            vol_el.set("lastid", str(vol.lastid))
            _cfg_meta(vol_el, "volume", "name", vol.name or name or f"part {vol.leaf_oid}")
            _cfg_meta(vol_el, "volume", "volume_type", vol.volume_type)
            if vol.extruder:
                _cfg_meta(vol_el, "volume", "extruder", vol.extruder)
            # Negative volumes and support blockers or enforcers have no print settings.
            if vol.volume_type in ("ModelPart", "ParameterModifier"):
                part_settings = translate(vol.settings, name, warnings, False, project.nozzle_diameter, remap)
                for k, v in part_settings.items():
                    _cfg_meta(vol_el, "volume", k, v)
    if len(root) == 0:
        return None
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _project_number(project: BambuProject, key: str, default: Optional[float] = None) -> Optional[float]:
    """A number from the project settings; per-extruder lists give their first value."""
    value = project.project_settings.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(str(value).split(",")[0])
    except (TypeError, ValueError):
        return default


def _max_speed(project: BambuProject, settings: dict) -> float:
    """The fastest print speed of an object, as Orca's Model::findMaxSpeed works it out."""
    base = {k: _project_number(project, k, 0.0) for k in autobrim.SPEED_KEYS}
    speeds = dict(base)

    def override(key):
        try:
            return float(str(settings[key]).split(",")[0])
        except (KeyError, ValueError):
            return None

    if (inner := override("inner_wall_speed")) is not None:
        speeds["inner_wall_speed"] = inner
        if base["inner_wall_speed"]:
            speeds["outer_wall_speed"] = base["outer_wall_speed"] / base["inner_wall_speed"] * inner
    for k in autobrim.SPEED_KEYS:
        if k != "inner_wall_speed" and (v := override(k)) is not None:
            speeds[k] = v
    fastest = max(speeds.values())
    small = str(settings.get("small_perimeter_speed", "")).split(",")[0]
    try:
        fastest = max(fastest, speeds["outer_wall_speed"] * float(small[:-1]) / 100 if small.endswith("%") else float(small))
    except ValueError:
        pass
    return fastest if fastest > 0 else 250.0


def _has_support(project: BambuProject, so, mo: _MergedObject, objs) -> bool:
    """Whether Orca prints support (or a raft) for an object: PrintObject::has_support_material.

    With manual supports only, support is only made in support enforcers and painted
    support areas, so an object without either has none.
    """
    def setting(key):
        value = so.settings.get(key, project.project_settings.get(key))
        if isinstance(value, list):
            value = value[0] if value else None
        return str(value if value is not None else "").split(",")[0].strip()

    def number(key):
        try:
            return float(setting(key))
        except ValueError:
            return 0.0

    if number("raft_layers") > 0 or number("enforce_support_layers") > 0:
        return True
    if setting("enable_support") not in ("1", "true"):
        return False
    if "manual" not in setting("support_type"):
        return True
    if any(vol.volume_type == "SupportEnforcer" for vol in mo.volumes):
        return True
    for leaf_key, _xf in mo.leaves:
        tris = objs[leaf_key].find(_MESH).find(_TRIANGLES)
        if tris is not None and any(t.get("paint_supports") for t in tris.iterchildren(_TRIANGLE)):
            return True
    return False


def _auto_brim(project: BambuProject, so, mo: _MergedObject, objs, placement, name: str, warnings: Warnings) -> dict:
    """PrusaSlicer brim settings for an object with Bambu's automatic brim."""
    fallback = "Brim type auto_brim could not be worked out for this object, the brim is left to the print preset"
    printed = [vol for vol in mo.volumes if vol.volume_type == "ModelPart"]
    if placement is None or objs is None or not printed:
        warnings.add(fallback, name)
        return {}
    flip_placement = placement.det3() < 0

    # The printed parts' vertices as placed on the plate.
    points, z_lows = {}, {}
    for vol in printed:
        leaf_key, xf = mo.leaves[vol.leaf_idx]
        verts = objs[leaf_key].find(_MESH).find(_VERTICES)
        pts = [] if verts is None else [
            placement.apply_point(*xf.apply_point(float(v.get("x", "0")), float(v.get("y", "0")), float(v.get("z", "0"))))
            for v in verts.iterchildren(_VERTEX)
        ]
        points[vol.leaf_idx] = pts
        z_lows[vol.leaf_idx] = min((p[2] for p in pts), default=math.inf)
    # A part sunk into the bed is printed from the bed up, as in both slicers.
    z_min = max(min(z_lows.values()), 0.0)
    z_max = max((p[2] for pts in points.values() for p in pts), default=-math.inf)
    first_layer = project.first_layer_height or 0.2

    # The footprint: a cut through the first layer.
    cut = autobrim.FootprintBuilder(z_min + first_layer / 2)
    for vol in printed:
        leaf_key, xf = mo.leaves[vol.leaf_idx]
        tris = objs[leaf_key].find(_MESH).find(_TRIANGLES)
        pts = points[vol.leaf_idx]
        if tris is None or z_lows[vol.leaf_idx] > cut.z:
            continue
        flip = (xf.det3() < 0) != flip_placement
        for t in tris.iterchildren(_TRIANGLE):
            try:
                a, b, c = pts[int(t.get("v1"))], pts[int(t.get("v2"))], pts[int(t.get("v3"))]
            except (TypeError, ValueError, IndexError):
                continue
            cut.add_triangle(a, c, b) if flip else cut.add_triangle(a, b, c)
    footprint = cut.result()
    if footprint is None:
        warnings.add(fallback, name)
        return {}

    # Material of the parts on the bed: Orca takes the adhesion of the last one and the
    # shortest thermal length of all of them.
    adhesion, thermal_ref = 1.0, 1250.0
    for vol in printed:
        if z_lows[vol.leaf_idx] > z_min + first_layer:
            continue
        try:
            filament = int(vol.settings.get("extruder") or so.extruder or 1) or 1
        except ValueError:
            filament = 1
        types = project.filament_types
        adhesion, thermal = autobrim.material(types[filament - 1] if 0 < filament <= len(types) else None)
        thermal_ref = min(thermal_ref, thermal)

    # Brim lines are first layer lines; Orca rounds the width to whole pairs of them.
    nozzle = project.nozzle_diameter or 0.4
    line = str(project.project_settings.get("initial_layer_line_width") or "").split(",")[0]
    try:
        line_width = nozzle * float(line[:-1]) / 100 if line.endswith("%") else float(line)
    except ValueError:
        line_width = 0.0
    if line_width <= 0:
        line_width = nozzle * 1.25
    spacing = line_width - first_layer * (1 - math.pi / 4)

    width = autobrim.brim_width(footprint, z_max - z_min, _max_speed(project, so.settings), adhesion, thermal_ref, spacing)
    if width > 0 and _has_support(project, so, mo, objs):
        # Orca clears the brim wherever support covers the first layer; PrusaSlicer would
        # trim the support instead. Support matters more than the brim, so do as Orca does.
        warnings.add(
            f"Brim type auto_brim worked out to a {_mm(width)} mm outer brim, left out because the object has "
            "supports: OrcaSlicer leaves out the brim where support covers the first layer",
            name,
        )
        return {"brim_type": "no_brim"}
    if width <= 0:
        warnings.add("Brim type auto_brim worked out to no brim, as in OrcaSlicer", name)
        return {"brim_type": "no_brim"}
    warnings.add(f"Brim type auto_brim worked out to a {_mm(width)} mm outer brim, as in OrcaSlicer (approximated)", name)
    return {"brim_type": "outer_only", "brim_width": _mm(width)}


def _object_name(project: BambuProject, key, mo: _MergedObject) -> str:
    so = project.settings_objects.get(key[1])
    return (so.name if so else "") or mo.name


def _mm(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _build_layer_height_profiles(
    project: BambuProject, merged: dict, id_map: dict, heights: dict, warnings: Warnings
) -> str:
    """PrusaSlicer's Slic3r_PE_layer_heights_profile.txt for the written objects.

    The format is Bambu's: ``object_id=N|z;h;z;h;...``, N counting the objects in the
    order they are written. Like both slicers, a profile is only used if it ends at the
    top of the object (within 0.001 mm); one that doesn't (the object was changed after
    the profile was made) is reported instead of written.
    """
    lines = []
    for key, mo in merged.items():
        profile = project.layer_height_profiles.get(key[1])
        if not profile:
            continue
        name = _object_name(project, key, mo)
        height = heights.get(key)
        if height is None or abs(profile[-2] - height) > 1e-3:
            shown = "" if height is None else f", the object is {_mm(height)} mm"
            warnings.add(
                f"Variable layer height was made for an object {_mm(profile[-2])} mm tall{shown}, not converted",
                name,
            )
            continue
        # PrusaSlicer prints the first layer at its own first layer height, then follows the profile.
        # Bambu ignores a profile made for a different first layer, PrusaSlicer uses it anyway.
        first = profile[1]
        if project.first_layer_height and abs(first - project.first_layer_height) > 1e-6:
            warnings.add(
                f"Variable layer height was made for a first layer height of {_mm(first)} mm "
                f"(the project uses {_mm(project.first_layer_height)} mm): "
                f"set the first layer height to {_mm(first)} mm in PrusaSlicer",
                name,
            )
        lines.append(f"object_id={id_map[key]}|" + ";".join(f"{v:f}" for v in profile))
    return "".join(line + "\n" for line in lines)


def _build_layer_ranges(
    project: BambuProject,
    merged: dict,
    id_map: dict,
    extruder_map: Optional[dict],
    warnings: Warnings,
) -> Optional[bytes]:
    """PrusaSlicer's Prusa_Slicer_layer_config_ranges.xml: height ranges with their settings.

    The layout is Bambu's, but the settings are translated: PrusaSlicer refuses to load a
    project with a setting it doesn't know.
    """
    root = ET.Element("objects")
    for key, mo in merged.items():
        ranges = project.layer_ranges.get(key[1])
        if not ranges:
            continue
        name = _object_name(project, key, mo)
        obj_el = ET.SubElement(root, "object")
        obj_el.set("id", id_map[key])
        for min_z, max_z, options in ranges:
            rng = ET.SubElement(obj_el, "range")
            rng.set("min_z", _fmt(min_z))
            rng.set("max_z", _fmt(max_z))
            settings = {}
            if "extruder" in options:
                try:
                    ext = int(options["extruder"])
                    settings["extruder"] = str(extruder_map.get(ext, ext) if extruder_map and ext > 0 else ext)
                except ValueError:
                    pass
            settings.update(translate(options, name, warnings, True, project.nozzle_diameter,
                                      lambda f: extruder_map.get(f, f) if extruder_map else f))
            for k, v in settings.items():
                opt = ET.SubElement(rng, "option")
                opt.set("opt_key", k)
                opt.text = v
    if len(root) == 0:
        return None
    return ET.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def _rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Relationships xmlns="{ns.RELS}">\n'
        f' <Relationship Target="/{ROOT_MODEL}" Id="rel-1" Type="{ns.REL_TYPE_STARTPART}"/>\n'
        "</Relationships>\n"
    )


def _pick_thumbnail(project: BambuProject) -> Optional[bytes]:
    # Prefer a plate thumbnail, then any thumbnail.
    for name, data in project.raw_entries.items():
        low = name.lower()
        if "plate_1.png" in low and "small" not in low and "no_light" not in low:
            return data
    for name, data in project.raw_entries.items():
        if "thumbnail" in name.lower():
            return data
    return None

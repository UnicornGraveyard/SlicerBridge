"""Synthetic Bambu Studio projects for the tests: no real model, no slicer needed.

The layout is Bambu Studio 2.x's: a root model whose objects are components
pointing into 3D/Objects/*.model, plates in model_settings.config, the project's
settings in project_settings.config, and paint as paint_color on triangles.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

# paint_color for a whole triangle in one state (TriangleSelector's encoding, hex read
# from the end): states 1 and 2 fit in the leaf's two bits, 3 and up take "C" plus a
# nibble holding state - 3.
LEAF = {1: "4", 2: "8"}


def leaf(state: int) -> str:
    return LEAF.get(state) or f"{state - 3:X}C"


def _cube(size: float) -> tuple[list, list]:
    v = [(x, y, z) for x in (0, size) for y in (0, size) for z in (0, size)]
    # Counter-clockwise seen from outside, as 3MF requires and Bambu Studio writes: a cut through the
    # cube then has the area of its footprint (the automatic brim needs one), not minus it.
    faces = [(0, 3, 2), (0, 1, 3), (4, 7, 5), (4, 6, 7), (0, 5, 1), (0, 4, 5),
             (2, 7, 6), (2, 3, 7), (0, 6, 4), (0, 2, 6), (1, 7, 3), (1, 5, 7)]
    return v, faces


def bambu_project(path: Path, *, palette=("#161616", "#CBC6B8", "#DE4343", "#5B6579", "#2E8B57"),
                  types=None, plates: int = 2, size: float = 40.0, extruder: int = 1,
                  paint: dict | None = None, bed: float = 256.0, extra_settings: dict | None = None,
                  application: str = "BambuStudio-02.08.02.61", client_version: str | None = "02.08.02.61",
                  parts: list | None = None) -> Path:
    """Write a project: one cube per plate, on filament `extruder`, its faces painted per `paint`.

    `parts`: instead, each plate's object is a row of cubes, one part each, [(filament, {setting: value})]
    -- a TPU band as its own part, say -- unpainted unless `paint` says otherwise (the first part's faces).

    `application` and `client_version` are what saved it: QIDI Studio stamps "QIDIStudio-..." and,
    like Anycubic Slicer Next, writes no X-BBL-Client-Version (None).

    `paint` maps face index (0-11) -> filament slot; the default paints slots 3 and 5,
    so with extruder 1 the model uses slots 1, 3 and 5 of a 5-color palette.
    """
    types = list(types or ["PLA"] * len(palette))
    paint = ({0: 3, 1: 3, 2: 5, 3: 5} if parts is None else {}) if paint is None else paint
    parts = list(parts or [(extruder, {})])
    k = len(parts)
    verts, faces = _cube(size)

    # 3D/Objects/object_1.model: one mesh object per part of each plate's object
    meshes = []
    for i in range(plates * k):
        vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"' + (f' paint_color="{leaf(paint[n])}"'
                                                                   if n in paint and i % k == 0 else "")
                       + "/>" for n, (a, b, c) in enumerate(faces))
        meshes.append(f'<object id="{i + 1}" type="model"><mesh><vertices>{vx}</vertices>'
                      f'<triangles>{tris}</triangles></mesh></object>')
    objects_model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE}" '
                     f'xmlns:p="{PROD}" requiredextensions="p"><resources>{"".join(meshes)}</resources>'
                     f'<build/></model>')

    # 3D/3dmodel.model: an object per plate whose component is that mesh, placed on its plate.
    # Bambu Studio lays plates out 1.2 beds apart.
    objs, items = [], []
    for i in range(plates):
        oid = plates * k + i + 1
        comps = "".join(f'<component p:path="/3D/Objects/object_1.model" objectid="{i * k + j + 1}" '
                        f'transform="1 0 0 0 1 0 0 0 1 {(j - k / 2) * size} {-size / 2} 0"/>' for j in range(k))
        objs.append(f'<object id="{oid}" type="model"><components>{comps}</components></object>')
        x = i * bed * 1.2 + bed / 2
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {bed / 2} 0" printable="1"/>')
    root_model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                  f'xmlns="{CORE}" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
                  f'xmlns:p="{PROD}" requiredextensions="p">'
                  f'<metadata name="Application">{application}</metadata>'
                  f'<metadata name="BambuStudio:3mfVersion">1</metadata>'
                  f'<resources>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')

    model_settings = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>']
    for i in range(plates):
        oid = plates * k + i + 1
        part_xml = "".join(
            f'<part id="{i * k + j + 1}" subtype="normal_part"><metadata key="name" value="cube{i + 1}-{j + 1}"/>'
            f'<metadata key="extruder" value="{e}"/>'
            + "".join(f'<metadata key="{key}" value="{value}"/>' for key, value in extra.items()) + "</part>"
            for j, (e, extra) in enumerate(parts))
        model_settings.append(
            f'<object id="{oid}"><metadata key="name" value="cube{i + 1}"/>'
            f'<metadata key="extruder" value="{extruder}"/>{part_xml}</object>')
    for i in range(plates):
        model_settings.append(
            f'<plate><metadata key="plater_id" value="{i + 1}"/><metadata key="locked" value="false"/>'
            f'<model_instance><metadata key="object_id" value="{plates * k + i + 1}"/>'
            f'<metadata key="instance_id" value="0"/></model_instance></plate>')
    model_settings.append("</config>")

    settings = {
        "filament_colour": list(palette), "filament_type": types,
        "filament_settings_id": [f"Generic {t}" for t in types],
        "printer_model": "Bambu Lab P1S", "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
        "print_settings_id": "0.16mm Optimal @BBL X1C",
        "printable_area": ["0x0", f"{bed:g}x0", f"{bed:g}x{bed:g}", f"0x{bed:g}"], "printable_height": "250",
        "nozzle_diameter": ["0.4"], "layer_height": "0.16", "initial_layer_print_height": "0.2",
        "curr_bed_type": "Textured PEI Plate",
        "flush_volumes_matrix": ["0" if a == b else "140" for a in range(len(palette)) for b in range(len(palette))],
        "flush_volumes_vector": ["140"] * (2 * len(palette)),
        "different_settings_to_system": ["", *([""] * len(palette)), ""],
        **(extra_settings or {}),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/'
                   '2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.'
                   '3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel-1" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/_rels/3dmodel.model.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Target="/3D/Objects/object_1.model" Id="rel-1" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", root_model)
        z.writestr("3D/Objects/object_1.model", objects_model)
        z.writestr("Metadata/model_settings.config", "".join(model_settings))
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=4))
        version = f'<header_item key="X-BBL-Client-Version" value="{client_version}"/>' if client_version else ""
        z.writestr("Metadata/slice_info.config",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<config><header><header_item key="X-BBL-Client-Type" '
                   f'value="slicer"/>{version}</header></config>')
    return path


def _grid(art: str, cell: float, codes: dict | None, attr: str) -> tuple[str, str]:
    """(vertices, triangles) XML for a flat grid of square cells, two triangles each, painted per `art`."""
    rows = [r for r in (line.strip() for line in art.strip().splitlines()) if r]
    h, w = len(rows), len(rows[0])
    assert all(len(r) == w for r in rows), "every row of the art is as long"
    vx = "".join(f'<vertex x="{i * cell:g}" y="{(h - j) * cell:g}" z="0"/>' for j in range(h + 1) for i in range(w + 1))
    tris = []
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            a, b = r * (w + 1) + c, r * (w + 1) + c + 1
            d, e = a + w + 1, b + w + 1
            for half, (p, q, s) in enumerate(((a, d, e), (a, e, b))):
                code = (codes or {}).get((r, c, half)) or ("" if ch == "." else leaf(int(ch)))
                tris.append(f'<triangle v1="{p}" v2="{q}" v3="{s}"' + (f' {attr}="{code}"' if code else "") + "/>")
    return vx, "".join(tris)


def _rezip(path: Path, entries: dict) -> None:
    with zipfile.ZipFile(path) as z:
        old = {i.filename: z.read(i.filename) for i in z.infolist()}
    old.update(entries)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in old.items():
            z.writestr(name, data)


def painted_grid(path: Path, art: str, *, cell: float = 0.5, codes: dict | None = None, scale: float = 1.0,
                 extruder: int = 1, palette=("#161616", "#CBC6B8", "#DE4343", "#5B6579", "#2E8B57", "#FFFFFF")) -> Path:
    """A Bambu Studio project whose one part is a flat grid of square cells (`cell` mm a side), each two
    triangles painted per `art`: a line of characters per row, a digit the filament, "." unpainted (the
    part prints in `extruder`). `codes` gives one triangle its own paint string: {(row, column, 0 or 1): code}.
    The object is placed at `scale`."""
    bambu_project(path, plates=1, paint={}, extruder=extruder, palette=palette)
    vx, tris = _grid(art, cell, codes, "paint_color")
    objects_model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE}" '
                     f'xmlns:p="{PROD}" requiredextensions="p"><resources><object id="1" type="model"><mesh>'
                     f'<vertices>{vx}</vertices><triangles>{tris}</triangles></mesh></object></resources>'
                     f'<build/></model>')
    with zipfile.ZipFile(path) as z:
        root = z.read("3D/3dmodel.model").decode()
    root = root.replace('<item objectid="2" transform="1 0 0 0 1 0 0 0 1 ',
                        f'<item objectid="2" transform="{scale:g} 0 0 0 {scale:g} 0 0 0 {scale:g} ')
    _rezip(path, {"3D/Objects/object_1.model": objects_model, "3D/3dmodel.model": root})
    return path


def prusa_grid(path: Path, art: str, *, cell: float = 0.5, extruder: int = 1,
               colors=("#161616", "#CBC6B8", "#DE4343", "#5B6579")) -> Path:
    """The same grid as a PrusaSlicer project: the mesh in the root model, paint as
    slic3rpe:mmu_segmentation, the volume's extruder in Slic3r_PE_model.config."""
    vx, tris = _grid(art, cell, None, "slic3rpe:mmu_segmentation")
    n = tris.count("<triangle ")
    root = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" xmlns="{CORE}" '
            f'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"><metadata name="slic3rpe:Version3mf">1</metadata>'
            f'<resources><object id="1" type="model"><mesh><vertices>{vx}</vertices><triangles>{tris}</triangles>'
            f'</mesh></object></resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 100 0" '
            f'printable="1"/></build></model>')
    model_config = ('<?xml version="1.0" encoding="UTF-8"?>\n<config><object id="1" instances_count="1">'
                    '<metadata type="object" key="name" value="grid"/>'
                    f'<metadata type="object" key="extruder" value="{extruder}"/>'
                    f'<volume firstid="0" lastid="{n - 1}"><metadata type="volume" key="name" value="grid"/>'
                    '<metadata type="volume" key="extruder" value="0"/></volume></object></config>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="model" ContentType="'
                   'application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" '
                   'Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", root)
        z.writestr("Metadata/Slic3r_PE.config", f"; generated by PrusaSlicer 2.9.6\n\n; extruder_colour = {';'.join(colors)}\n"
                                                f"; filament_colour = {';'.join(colors)}\n")
        z.writestr("Metadata/Slic3r_PE_model.config", model_config)
    return path

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
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return v, faces


def bambu_project(path: Path, *, palette=("#161616", "#CBC6B8", "#DE4343", "#5B6579", "#2E8B57"),
                  types=None, plates: int = 2, size: float = 40.0, extruder: int = 1,
                  paint: dict | None = None, bed: float = 256.0, extra_settings: dict | None = None) -> Path:
    """Write a project: one cube per plate, on filament `extruder`, its faces painted per `paint`.

    `paint` maps face index (0-11) -> filament slot; the default paints slots 3 and 5,
    so with extruder 1 the model uses slots 1, 3 and 5 of a 5-color palette.
    """
    types = list(types or ["PLA"] * len(palette))
    paint = {0: 3, 1: 3, 2: 5, 3: 5} if paint is None else paint
    verts, faces = _cube(size)

    # 3D/Objects/object_1.model: one mesh object per plate
    meshes = []
    for i in range(plates):
        vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"' + (f' paint_color="{leaf(paint[n])}"' if n in paint else "")
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
        oid = plates + i + 1
        objs.append(f'<object id="{oid}" type="model"><components>'
                    f'<component p:path="/3D/Objects/object_1.model" objectid="{i + 1}" '
                    f'transform="1 0 0 0 1 0 0 0 1 {-size / 2} {-size / 2} 0"/></components></object>')
        x = i * bed * 1.2 + bed / 2
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {bed / 2} 0" printable="1"/>')
    root_model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
                  f'xmlns="{CORE}" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
                  f'xmlns:p="{PROD}" requiredextensions="p">'
                  f'<metadata name="Application">BambuStudio-02.08.02.61</metadata>'
                  f'<metadata name="BambuStudio:3mfVersion">1</metadata>'
                  f'<resources>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')

    model_settings = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>']
    for i in range(plates):
        oid = plates + i + 1
        model_settings.append(
            f'<object id="{oid}"><metadata key="name" value="cube{i + 1}"/>'
            f'<metadata key="extruder" value="{extruder}"/>'
            f'<part id="{i + 1}" subtype="normal_part"><metadata key="name" value="cube{i + 1}"/>'
            f'<metadata key="extruder" value="{extruder}"/></part></object>')
    for i in range(plates):
        model_settings.append(
            f'<plate><metadata key="plater_id" value="{i + 1}"/><metadata key="locked" value="false"/>'
            f'<model_instance><metadata key="object_id" value="{plates + i + 1}"/>'
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
        z.writestr("Metadata/slice_info.config",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<config><header><header_item key="X-BBL-Client-Type" '
                   'value="slicer"/><header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header></config>')
    return path

"""Translate Bambu Studio / OrcaSlicer object and part settings to PrusaSlicer.

Bambu stores the settings changed for an object or a part (a modifier, for example)
as ``<metadata key=.. value=..>`` in ``Metadata/model_settings.config``. PrusaSlicer
reads the same idea from ``Metadata/Slic3r_PE_model.config``, but most keys were
renamed when Bambu Studio forked PrusaSlicer, and some enum values differ.

Only settings with the same meaning in both slicers are carried over. Everything
else is dropped with a warning, so a conversion never silently loses a setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Metadata that describes the object or part rather than how it prints.
NOT_SETTINGS = {
    "name", "matrix", "extruder", "face_count", "thumbnail_file",
    "source_file", "source_object_id", "source_volume_id",
    "source_offset_x", "source_offset_y", "source_offset_z",
    "source_in_inches", "source_in_meters",
}

# Settings of Bambu's Locked Zag infill only, which PrusaSlicer doesn't have. Bambu Studio
# copies the infill density into the two densities whenever it changes, so they show up
# with any pattern; a project that does use Locked Zag gets a warning for the pattern.
LOCKED_ZAG_ONLY = {
    "skin_infill_density", "skeleton_infill_density", "infill_lock_depth", "skin_infill_depth",
    "skin_infill_line_width", "skeleton_infill_line_width",
}

# Same meaning and value format, only the key differs (or not even that).
RENAMED = {
    # Walls, shells, infill
    "wall_loops": "perimeters",
    "top_shell_layers": "top_solid_layers",
    "bottom_shell_layers": "bottom_solid_layers",
    "top_shell_thickness": "top_solid_min_thickness",
    "bottom_shell_thickness": "bottom_solid_min_thickness",
    "sparse_infill_density": "fill_density",
    "infill_direction": "fill_angle",
    "infill_anchor": "infill_anchor",
    "infill_anchor_max": "infill_anchor_max",
    "infill_combination": "automatic_infill_combination",
    "infill_combination_max_layer_height": "automatic_infill_combination_max_layer_height",
    "minimum_sparse_infill_area": "solid_infill_below_area",
    "interface_shells": "interface_shells",
    "detect_thin_wall": "thin_walls",
    "detect_overhang_wall": "overhangs",
    "extra_perimeters_on_overhangs": "extra_perimeters_on_overhangs",
    "only_one_wall_first_layer": "only_one_perimeter_first_layer",
    "min_bead_width": "min_bead_width",
    "min_feature_size": "min_feature_size",
    "wall_transition_angle": "wall_transition_angle",
    "wall_transition_filter_deviation": "wall_transition_filter_deviation",
    "wall_transition_length": "wall_transition_length",
    "wall_distribution_count": "wall_distribution_count",
    # Bridges
    "bridge_flow": "bridge_flow_ratio",
    "bridge_angle": "bridge_angle",
    "thick_bridges": "thick_bridges",
    "bridge_no_support": "dont_support_bridges",
    # Speeds (mm/s)
    "outer_wall_speed": "external_perimeter_speed",
    "inner_wall_speed": "perimeter_speed",
    "sparse_infill_speed": "infill_speed",
    "internal_solid_infill_speed": "solid_infill_speed",
    "top_surface_speed": "top_solid_infill_speed",
    "gap_infill_speed": "gap_fill_speed",
    "bridge_speed": "bridge_speed",
    "support_speed": "support_material_speed",
    "support_interface_speed": "support_material_interface_speed",
    # Ironing
    "ironing_flow": "ironing_flowrate",
    "ironing_spacing": "ironing_spacing",
    "ironing_speed": "ironing_speed",
    # Seam
    "staggered_inner_seams": "staggered_inner_seams",
    "seam_slope_conditional": "scarf_seam_only_on_smooth",
    "seam_slope_entire_loop": "scarf_seam_entire_loop",
    "seam_slope_min_length": "scarf_seam_length",
    "seam_slope_inner_walls": "scarf_seam_on_inner_perimeters",
    # Fuzzy skin
    "fuzzy_skin_thickness": "fuzzy_skin_thickness",
    "fuzzy_skin_point_distance": "fuzzy_skin_point_dist",
    # Layers and slicing
    "layer_height": "layer_height",
    "slicing_mode": "slicing_mode",
    "slice_closing_radius": "slice_closing_radius",
    "elefant_foot_compensation": "elefant_foot_compensation",
    # Supports
    "enable_support": "support_material",
    "support_threshold_angle": "support_material_threshold",
    "support_on_build_plate_only": "support_material_buildplate_only",
    "support_angle": "support_material_angle",
    "enforce_support_layers": "support_material_enforce_layers",
    "support_top_z_distance": "support_material_contact_distance",
    "support_interface_top_layers": "support_material_interface_layers",
    "support_interface_bottom_layers": "support_material_bottom_interface_layers",
    "support_base_pattern_spacing": "support_material_spacing",
    "support_interface_spacing": "support_material_interface_spacing",
    "support_object_xy_distance": "support_material_xy_spacing",
    "support_interface_loop_pattern": "support_material_interface_contact_loops",
    "tree_support_branch_angle_organic": "support_tree_angle",
    "tree_support_angle_slow": "support_tree_angle_slow",
    "tree_support_branch_diameter_organic": "support_tree_branch_diameter",
    "tree_support_branch_diameter_angle": "support_tree_branch_diameter_angle",
    "tree_support_branch_distance_organic": "support_tree_branch_distance",
    "tree_support_tip_diameter": "support_tree_tip_diameter",
    "tree_support_top_rate": "support_tree_top_rate",
    # Raft
    "raft_layers": "raft_layers",
    "raft_contact_distance": "raft_contact_distance",
    "raft_expansion": "raft_expansion",
    "raft_first_layer_density": "raft_first_layer_density",
    "raft_first_layer_expansion": "raft_first_layer_expansion",
    # Brim (brim_type has its own translation)
    "brim_width": "brim_width",
    "brim_object_gap": "brim_separation",
    # Multi-material
    "flush_into_infill": "wipe_into_infill",
    "flush_into_objects": "wipe_into_objects",
    "mmu_segmented_region_max_width": "mmu_segmented_region_max_width",
    "mmu_segmented_region_interlocking_depth": "mmu_segmented_region_interlocking_depth",
    "interlocking_beam": "interlocking_beam",
    "interlocking_beam_layer_count": "interlocking_beam_layer_count",
    "interlocking_beam_width": "interlocking_beam_width",
    "interlocking_boundary_avoidance": "interlocking_boundary_avoidance",
    "interlocking_depth": "interlocking_depth",
    "interlocking_orientation": "interlocking_orientation",
}

# Line widths: the same in mm, but a percentage is of the nozzle diameter in Bambu
# and of the layer height in PrusaSlicer, so percentages are converted to mm.
LINE_WIDTHS = {
    "line_width": "extrusion_width",
    "outer_wall_line_width": "external_perimeter_extrusion_width",
    "inner_wall_line_width": "perimeter_extrusion_width",
    "sparse_infill_line_width": "infill_extrusion_width",
    "internal_solid_infill_line_width": "solid_infill_extrusion_width",
    "top_surface_line_width": "top_infill_extrusion_width",
    "support_line_width": "support_material_extrusion_width",
}

# Filament numbers, renumbered like the parts' filaments. 0 means the default in both.
FILAMENTS = {
    "support_filament": "support_material_extruder",
    "support_interface_filament": "support_material_interface_extruder",
}

# Infill patterns PrusaSlicer accepts for each key, and Bambu names that differ.
_SPARSE_PATTERNS = {
    "rectilinear", "alignedrectilinear", "grid", "triangles", "stars", "cubic", "line",
    "concentric", "honeycomb", "3dhoneycomb", "gyroid", "hilbertcurve", "archimedeanchords",
    "octagramspiral", "adaptivecubic", "supportcubic", "lightning", "zigzag",
}
_SURFACE_PATTERNS = {
    "rectilinear", "monotonic", "monotoniclines", "alignedrectilinear", "concentric",
    "hilbertcurve", "archimedeanchords", "octagramspiral",
}
_PATTERN_NAMES = {"zig-zag": "rectilinear", "tri-hexagon": "stars", "monotonicline": "monotoniclines"}
PATTERNS = {
    "sparse_infill_pattern": ("fill_pattern", _SPARSE_PATTERNS),
    "top_surface_pattern": ("top_fill_pattern", _SURFACE_PATTERNS),
    "bottom_surface_pattern": ("bottom_fill_pattern", _SURFACE_PATTERNS),
}

# Enum settings: Bambu value -> PrusaSlicer value. Values missing here have no equivalent.
ENUMS = {
    "seam_position": ("seam_position", {"nearest": "nearest", "aligned": "aligned", "back": "rear", "random": "random"}),
    "seam_slope_type": ("scarf_seam_placement", {"none": "nowhere", "external": "contours", "all": "everywhere"}),
    # Orca's "Contour" fuzzes the outline without the holes; PrusaSlicer's "External"
    # fuzzes all outer walls, holes included. Its "Contour and hole" is PrusaSlicer's "External".
    "fuzzy_skin": ("fuzzy_skin", {"none": "none", "disabled_fuzzy": "none", "external": "external",
                                  "all": "external", "allwalls": "all"}),
    "wall_sequence": ("external_perimeters_first", {"inner wall/outer wall": "0", "outer wall/inner wall": "1"}),
    "ensure_vertical_shell_thickness": ("ensure_vertical_shell_thickness", {"none": "disabled", "ensure_all": "enabled"}),
    "only_one_wall_top": ("top_one_perimeter_type", {"0": "none", "1": "top"}),
    "gap_fill_target": ("gap_fill_enabled", {"everywhere": "1", "nowhere": "0"}),
    "support_base_pattern": ("support_material_pattern", {v: v for v in ("rectilinear", "rectilinear-grid", "honeycomb")}),
    "support_interface_pattern": ("support_material_interface_pattern", {v: v for v in ("auto", "rectilinear", "concentric")}),
    "wall_generator": ("perimeter_generator", {"classic": "classic", "arachne": "arachne"}),
}

# PrusaSlicer keys that only apply to a whole object, never to a part or modifier.
PRUSA_OBJECT_KEYS = {
    "layer_height", "slicing_mode", "slice_closing_radius", "elefant_foot_compensation",
    "extrusion_width", "interface_shells", "seam_position", "staggered_inner_seams",
    "perimeter_generator", "min_bead_width", "min_feature_size", "wall_distribution_count",
    "wall_transition_angle", "wall_transition_filter_deviation", "wall_transition_length",
    "brim_type", "brim_width", "brim_separation",
    "raft_layers", "raft_contact_distance", "raft_expansion", "raft_first_layer_density",
    "raft_first_layer_expansion", "dont_support_bridges", "thick_bridges", "wipe_into_objects",
    "interlocking_beam", "interlocking_beam_layer_count", "interlocking_beam_width",
    "interlocking_boundary_avoidance", "interlocking_depth", "interlocking_orientation",
    "mmu_segmented_region_max_width", "mmu_segmented_region_interlocking_depth",
    "support_material", "support_material_auto", "support_material_style",
    "support_material_threshold", "support_material_buildplate_only", "support_material_angle",
    "support_material_enforce_layers", "support_material_contact_distance",
    "support_material_bottom_contact_distance", "support_material_interface_layers",
    "support_material_bottom_interface_layers", "support_material_spacing",
    "support_material_interface_spacing", "support_material_xy_spacing",
    "support_material_interface_contact_loops", "support_material_speed",
    "support_material_interface_speed", "support_material_pattern",
    "support_material_interface_pattern", "support_material_extrusion_width",
    "support_material_extruder", "support_material_interface_extruder",
    "support_tree_angle", "support_tree_angle_slow", "support_tree_branch_diameter",
    "support_tree_branch_diameter_angle", "support_tree_branch_distance",
    "support_tree_tip_diameter", "support_tree_top_rate",
}

# Bambu support styles; every tree style becomes PrusaSlicer's only tree, Organic.
_SUPPORT_STYLES = {"grid": "grid", "snug": "snug", "organic": "organic",
                   "tree_slim": "organic", "tree_strong": "organic", "tree_hybrid": "organic"}


@dataclass
class Warnings:
    """Conversion warnings, each with the objects it applies to."""

    _objects: dict = field(default_factory=dict)  # message -> list of object names

    def add(self, message: str, obj: str = "") -> None:
        """Record ``message`` for the object named ``obj``; no name for the whole project."""
        names = self._objects.setdefault(message, [])
        if obj and obj not in names:
            names.append(obj)

    def lines(self, max_names: int = 3) -> list[str]:
        out = []
        for message, names in self._objects.items():
            shown = ", ".join(f'"{n}"' for n in names[:max_names])
            if len(names) > max_names:
                shown += f" and {len(names) - max_names} more"
            out.append(f"{message} ({shown})" if shown else message)
        return out


def _first_value(value: str) -> str:
    """Bambu stores per-nozzle values for dual-nozzle printers, e.g. "150,nil"; keep the first."""
    if "," not in value:
        return value
    for v in value.split(","):
        if v.strip() and v.strip() != "nil":
            return v.strip()
    return value.split(",")[0].strip()


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def translate(
    settings: dict,
    obj: str,
    warnings: Warnings,
    whole_object: bool,
    nozzle_diameter: Optional[float] = None,
    remap_filament: Callable[[int], int] = lambda f: f,
) -> dict:
    """PrusaSlicer settings for the Bambu settings of an object (``whole_object``) or a part.

    ``obj`` names the object in warnings. ``remap_filament`` renumbers filament settings
    the same way as the parts' filaments.
    """
    out: dict = {}

    def drop(key: str, value: str, why: str = "has no PrusaSlicer equivalent") -> None:
        warnings.add(f"Setting {key} = {value} {why}, not converted", obj)

    for key, raw in settings.items():
        if key in NOT_SETTINGS or key in LOCKED_ZAG_ONLY or key in ("support_type", "support_style"):
            continue
        value = _first_value(raw)
        if key in RENAMED:
            out[RENAMED[key]] = value
        elif key in LINE_WIDTHS:
            if value.endswith("%"):
                if not nozzle_diameter:
                    drop(key, value, "is a percentage of an unknown nozzle diameter")
                    continue
                value = _fmt(float(value[:-1]) * nozzle_diameter / 100.0)
            out[LINE_WIDTHS[key]] = value
        elif key in FILAMENTS:
            try:
                f = int(value)
            except ValueError:
                drop(key, value)
                continue
            out[FILAMENTS[key]] = str(remap_filament(f) if f > 0 else 0)
        elif key in PATTERNS:
            prusa_key, allowed = PATTERNS[key]
            pattern = _PATTERN_NAMES.get(value, value)
            if pattern in allowed:
                out[prusa_key] = pattern
            else:
                drop(key, value)
        elif key in ENUMS:
            prusa_key, values = ENUMS[key]
            if value in values:
                out[prusa_key] = values[value]
            else:
                drop(key, value)
        elif key == "ironing_type":
            if value == "no ironing":
                out["ironing"] = "0"
            elif value in ("top", "topmost", "solid"):
                out["ironing"] = "1"
                out["ironing_type"] = value
            else:
                drop(key, value)
        elif key == "brim_type":
            if value in ("no_brim", "outer_only", "inner_only", "outer_and_inner"):
                out["brim_type"] = value
            elif value in ("brim_ears", "painted", "leading_edge_only"):
                out["brim_type"] = "no_brim"
                warnings.add(f"Brim type {value} has no PrusaSlicer equivalent, the brim was removed", obj)
            elif value == "auto_brim":
                pass  # computed from the object's geometry by the writer, see autobrim.py
            else:
                drop(key, value)
        elif key == "small_perimeter_speed":
            # A percentage is of the outer wall speed in Bambu, of the perimeter speed in PrusaSlicer.
            if value.endswith("%"):
                drop(key, value, "is a percentage of a different speed in PrusaSlicer")
            else:
                out["small_perimeter_speed"] = value
        elif key == "support_bottom_z_distance":
            # PrusaSlicer reads zero as "the same as the top distance".
            if float(value) == 0:
                drop(key, value, "means the top distance in PrusaSlicer")
            else:
                out["support_material_bottom_contact_distance"] = value
        elif key == "seam_slope_start_height":
            # PrusaSlicer only takes a percentage of the layer height.
            if value.endswith("%"):
                out["scarf_seam_start_height"] = value
            else:
                drop(key, value, "is in mm, PrusaSlicer only takes a percentage")
        else:
            drop(key, value)

    # Supports: Bambu's type is the family (normal or tree) plus auto or manual (enforcers only).
    support_type, support_style = settings.get("support_type"), settings.get("support_style")
    if support_type:
        family, _, mode = support_type.partition("(")
        if mode.rstrip(")") in ("auto", "manual"):
            out["support_material_auto"] = "1" if mode.startswith("auto") else "0"
        if not support_style or support_style == "default":
            style = {"normal": "grid", "tree": "organic"}.get(family)
            if style:
                out["support_material_style"] = style
    if support_style and support_style != "default":
        if support_style in _SUPPORT_STYLES:
            out["support_material_style"] = _SUPPORT_STYLES[support_style]
        else:
            drop("support_style", support_style)

    if not whole_object:
        for key in [k for k in out if k in PRUSA_OBJECT_KEYS]:
            warnings.add(f"Setting {key} applies to whole objects in PrusaSlicer, not converted for a part", obj)
            del out[key]
    return out

"""Which of the source project's print settings every engine carries into its files.

Bambu Studio marks a project's print settings changed from the preset's system parent
(`different_settings_to_system`). A process preset with no system parent -- detached,
imported, or downloaded with a MakerWorld profile -- has nothing to compare against,
so the project marks nothing as changed, however far the designer took it from any
printer's default. The settings that shape the print are therefore carried whatever
the project marks; the rest only when it marks them.
"""
from __future__ import annotations

import re

# What the print is: layers, walls and shells, infill, surface patterns, seams,
# supports and rafts, brim and skirt, ironing, fuzzy skin, vase mode, hole sizing.
SHAPES_THE_PRINT = re.compile(
    r"^(layer_height|initial_layer_print_height"
    r"|wall_loops|wall_generator|wall_sequence|wall_infill_order|wall_direction|only_one_wall_\w+|detect_thin_wall"
    r"|(top|bottom)_shell_(layers|thickness)|(top|bottom)_surface_pattern|internal_solid_infill_pattern"
    r"|sparse_infill_\w+|infill_\w+|\w+_infill_density|minimum_sparse_infill_area"
    r"|seam_\w+|enable_support|support_\w+|tree_support_\w+|raft_\w+"
    r"|brim_\w+|skirt_\w+|ironing_\w+|fuzzy_skin\w*|spiral_mode|xy_(hole|contour)_compensation)$")
# Tuned to the source's printer (its motion system, its nozzle), or a filament number
# the engines renumber themselves: carried only when the project marks it as changed.
PRINTER_TUNED = re.compile(r"(_speed|_acceleration|_jerk|_line_width|_filament)$")
# Never carried: the printer's own business (its prime tower, purging, the
# timelapse moves a Bambu camera wants), and filament numbers, handled apart.
NEVER_CARRY = re.compile(r"^(prime_tower_|wipe_tower_|flush_volumes_|flush_multiplier$|timelapse_|"
                         r"enable_prime_tower$|.*_filament$)")


def shapes_the_print(key: str) -> bool:
    return bool(SHAPES_THE_PRINT.match(key)) and not PRINTER_TUNED.search(key)


def marked_changed(src_cfg: dict) -> set:
    """The print settings the source project marks as changed from its preset."""
    diff = src_cfg.get("different_settings_to_system") or [""]
    return {k for k in str(diff[0]).split(";") if k}


def print_keys(src_cfg: dict) -> list:
    """The source's print settings to carry: all that shape the print, and all it marks as changed."""
    return sorted({k for k in src_cfg if shapes_the_print(k)} | marked_changed(src_cfg))


def carry_print_settings(src_cfg: dict, base: dict) -> tuple[dict, dict]:
    """({key: the source's value} to carry onto `base`, {key: why not}) for the print settings the designer chose.

    `base` is the target's own settings, in the same Bambu project format: a value it
    already has isn't carried, and one it keeps another way is skipped, with why.
    """
    carried, skipped = {}, {}
    for k in print_keys(src_cfg):
        if NEVER_CARRY.match(k) or k not in src_cfg:
            continue
        v = src_cfg[k]
        if k not in base:
            skipped[k] = "not a setting of the printer's slicer"
        elif type(v) is not type(base[k]) or (isinstance(v, list) and len(v) != len(base[k])):
            skipped[k] = f"Bambu Studio keeps it per nozzle ({v})"
        elif v != base[k]:
            carried[k] = v
    return carried, skipped

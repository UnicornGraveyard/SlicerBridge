"""The designer's print settings reach every printer's file, whatever the project marks as changed."""
import json
import zipfile

from builders import bambu_project
from slicerbridge.carry import carry_print_settings, print_keys, shapes_the_print
from slicerbridge.engines import prusa
from slicerbridge.engines import u1 as engine

# A detached process preset: Bambu Studio has no system parent to diff it against,
# so different_settings_to_system marks nothing, however far it is from the default.
DESIGNER = {"sparse_infill_density": "35%", "sparse_infill_pattern": "gyroid", "enable_support": "1",
            "support_type": "tree(auto)", "support_top_z_distance": "0.18", "brim_type": "outer_only",
            "brim_width": "6", "ironing_type": "top", "fuzzy_skin": "external", "wall_loops": "4"}


def test_what_shapes_the_print_is_carried_and_what_the_printer_tunes_is_not():
    for k in ("sparse_infill_density", "support_top_z_distance", "tree_support_branch_angle", "brim_width",
              "ironing_type", "fuzzy_skin", "top_surface_pattern", "seam_position", "raft_layers"):
        assert shapes_the_print(k), k
    for k in ("sparse_infill_speed", "support_interface_speed", "default_acceleration", "outer_wall_line_width",
              "support_filament", "nozzle_temperature", "printable_area"):
        assert not shapes_the_print(k), k


def test_nothing_marked_still_carries_the_designers_settings():
    src = {**DESIGNER, "outer_wall_speed": "300", "different_settings_to_system": ["", "", ""]}
    base = {k: "x" for k in src}
    carried, skipped = carry_print_settings(src, base)
    assert carried == DESIGNER and skipped == {}


def test_a_marked_speed_is_carried_and_an_unmarked_one_is_not():
    src = {"outer_wall_speed": "120", "inner_wall_speed": "200",
           "different_settings_to_system": ["outer_wall_speed", ""]}
    assert print_keys(src) == ["outer_wall_speed"]
    carried, _ = carry_print_settings(src, {"outer_wall_speed": "200", "inner_wall_speed": "300"})
    assert carried == {"outer_wall_speed": "120"}


def test_the_printers_own_settings_never_carry():
    src = {"prime_tower_width": "50", "support_filament": "2", "enable_prime_tower": "0",
           "different_settings_to_system": ["prime_tower_width;support_filament;enable_prime_tower", ""]}
    carried, _ = carry_print_settings(src, {k: "x" for k in src})
    assert carried == {}


def test_a_value_the_printer_already_has_or_keeps_per_nozzle_isnt_carried():
    carried, skipped = carry_print_settings({"wall_loops": "3", "brim_width": ["5", "5"]},
                                            {"wall_loops": "3", "brim_width": "5"})
    assert carried == {} and set(skipped) == {"brim_width"}


def test_prusa_takes_what_it_can_translate_and_reports_only_what_was_marked():
    src = {**DESIGNER, "wall_direction": "auto", "seam_slope_conditional": "1",
           "different_settings_to_system": ["seam_slope_conditional", ""]}
    keys = [k for k in print_keys(src) if k in prusa.marked_changed(src) or prusa.translatable(k)]
    assert {"sparse_infill_density", "support_top_z_distance", "brim_type", "ironing_type"} <= set(keys)
    assert "wall_direction" not in keys  # no PrusaSlicer setting, and the designer didn't mark it
    assert "seam_slope_conditional" in keys  # marked: carried, or reported if it can't be


def _u1_settings(tmp_path, name, extra):
    src = bambu_project(tmp_path / f"{name}.3mf", extra_settings=extra)
    assert engine.convert(src, name, ["u1"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / name / f"{name} - Snapmaker U1.3mf"
    with zipfile.ZipFile(f) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def test_the_u1_file_prints_with_the_designers_settings(tmp_path):
    s = _u1_settings(tmp_path, "Relic", DESIGNER)
    for k, v in DESIGNER.items():
        assert s[k] == v, k
    assert {"sparse_infill_density", "brim_width"} <= set(s["different_settings_to_system"][0].split(";"))


def test_the_u1_prints_supports_when_they_are_on_but_not_marked(tmp_path):
    s = _u1_settings(tmp_path, "Propped", {"enable_support": "1"})
    assert s["enable_support"] == "1"
    assert "enable_support" in s["different_settings_to_system"][0].split(";")

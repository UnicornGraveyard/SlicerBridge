"""What the brands' own slicers read differently from OrcaSlicer, and the fixes (docs/brand-slicers.md)."""
from slicerbridge.checks import orca as check
from slicerbridge.engines import orca as engine

KOBRA3_FRAME = ["0x0", "255x0", "255x255", "0x255", "0x0", "2x2", "2x253", "253x253", "253x2", "2x2"]
KOBRA3MAX_STRIPS = ["0x0", "3x0", "3x420", "0x420", "0x0", "423x0", "423x0", "423x0", "423x0", "426x0", "426x420",
                    "423x420", "423x0", "0x0", "0x0", "0x0"]


def test_a_frame_is_the_whole_bed_to_anycubic():
    s = {"bed_exclude_area": KOBRA3_FRAME}
    assert check.exclusion_rects(s) == [(0, 0, 255, 2), (0, 2, 2, 253), (0, 253, 255, 255), (253, 2, 255, 253)]
    assert check.exclusion_rects(s, quads=True) == [(0, 0, 255, 255)]


def test_orcaslicers_kobra3max_strips_read_the_same_both_ways():
    s = {"bed_exclude_area": KOBRA3MAX_STRIPS}
    assert check.exclusion_rects(s) == check.exclusion_rects(s, quads=True) == [(0, 0, 3, 420), (423, 0, 426, 420)]


def test_rewritten_exclusion_reads_the_same_both_ways():
    rects = check.exclusion_rects({"bed_exclude_area": KOBRA3_FRAME})
    s = {"bed_exclude_area": engine.exclusion_area(rects, (0, 0))}
    assert len(s["bed_exclude_area"]) % 4 == 0
    assert check.exclusion_rects(s) == check.exclusion_rects(s, quads=True) == rects


def test_line_widths_in_percent_become_mm():
    s = {"nozzle_diameter": ["0.4"], "skin_infill_line_width": "100%", "bridge_line_width": "112.5%",
         "line_width": "0.42"}
    assert engine.absolute_line_widths(s) == {"skin_infill_line_width", "bridge_line_width"}
    assert (s["skin_infill_line_width"], s["bridge_line_width"], s["line_width"]) == ("0.4", "0.45", "0.42")


def test_line_widths_stay_put_with_mixed_nozzles():
    s = {"nozzle_diameter": ["0.4", "0.6"], "skin_infill_line_width": "100%"}
    assert engine.absolute_line_widths(s) == set() and s["skin_infill_line_width"] == "100%"


ACE_GCODE = "\n".join([
    "{ local curr_t = current_extruder; local next_t = next_extruder;}",
    "{ if (0 <= curr_t && curr_t < 3) then local a=1 else local a=3 endif}",  # inline if: no depth
    check.ACE_BLOCK,
    "; curr_t box={ace_t_box_vector[curr_t]}",
    "    { if ace_t_box_vector[curr_t] < 0 }",
    ";;; nothing",
    "    { else }",
    ";;; something",
    "    { endif }",
    "{ endif }",
    "T{next_extruder}",
])


def test_the_ace_block_gets_anycubics_guard_nested_inside():
    out = engine.guard_ace(ACE_GCODE).split("\n")
    i = out.index(check.ACE_BLOCK)
    assert out[i + 1] == check.ACE_GUARD
    assert out[-3:] == ["{ endif }", "{ endif }", "T{next_extruder}"]  # one more endif closes the guard
    assert engine.guard_ace("\n".join(out)) is None  # and again changes nothing


def test_gcode_without_an_ace_block_is_left_alone():
    assert engine.guard_ace("T{next_extruder}\nG1 X10") is None

"""The prusa engine's write path without PrusaSlicer, so CI runs it: convert_one(), its checks, and
landing the files.

PrusaSlicer builds each printer's profile from Prusa's own bundle, which SlicerBridge never ships. The
profiles here are made up: only the settings the engine reads, with each printer's preset name and
tool count. Without PrusaSlicer, checks 9 and 10 (PrusaSlicer loading and slicing the file) skip
themselves, as they do on a machine without it.
"""
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from builders import bambu_project
from slicerbridge import output, printers
from slicerbridge._vendor.panda2prusa import autobrim
from slicerbridge.cli import main
from slicerbridge.engines import prusa
from slicerbridge.errors import Refusal

# key: (bed width, depth, height, wipe tower)
BEDS = {"mmu3": (250, 220, 270, True), "xl": (360, 360, 360, True),
        "indx4": (250, 220, 270, False), "indx8": (250, 220, 270, False)}


# Made-up filament values per type: (temperature, first layer, bed, first layer bed). Not Prusa's.
TEMPS = {"PLA": (215, 220, 60, 60), "PETG": (240, 245, 85, 85), "ABS": (255, 255, 100, 100),
         "ASA": (260, 260, 105, 105), "PC": (275, 275, 110, 110), "PETG-CF": (265, 260, 85, 85),
         "PA-CF": (285, 285, 100, 100), "HIPS": (230, 230, 100, 100), "PVA": (210, 215, 60, 60),
         "TPU": (225, 225, 50, 50)}


def made_up_profile(key: str, ftype: str = "PLA") -> str:
    """A profile the engine can load for `key` with `ftype`'s preset on every tool, as PrusaSlicer saves one:
    none of Prusa's own settings, only what the engine reads, a filament's per tool."""
    p = prusa.PRINTERS[key]
    w, d, h, tower = BEDS[key]
    tools = p.tools
    kind, abrasive = prusa.PRUSA_KINDS[ftype]
    temp, first, bed, first_bed = TEMPS[ftype]

    def each(value, sep=","):
        return sep.join([str(value)] * tools)
    lines = {
        "printer_settings_id": p.preset,
        "print_settings_id": p.print_preset,
        "filament_settings_id": each(f'"{p.filaments[ftype]}"', ";"),
        "filament_type": each(kind, ";"),
        "filament_abrasive": each(int(abrasive)),
        "temperature": each(temp),
        "first_layer_temperature": each(first),
        "bed_temperature": each(bed),
        "first_layer_bed_temperature": each(first_bed),
        # a G-code string with a ";" in it, and the print preset's condition before the tools'
        "start_filament_gcode": each(f'"M900 K{temp / 10000:g} ; {ftype}\\n"', ";"),
        "compatible_printers_condition_cummulative": '"printer_model==\\"X\\""' + ";" + each(f'"{ftype}"', ";"),
        "nozzle_diameter": each("0.4"),
        "bed_shape": f"0x0,{w}x0,{w}x{d},0x{d}",
        "max_print_height": str(h),
        "filament_colour": each("#FFFFFF", ";"),
        "extruder_colour": each("#FFFFFF", ";"),
        "layer_height": "0.15",
        "first_layer_height": "0.2",
        "perimeters": "2",
        "fill_density": "15%",
        "support_material": "0",
        "single_extruder_multi_material": "1" if key == "mmu3" else "0",
        "bed_temperature_extruder": "0",
        "wipe_tower": "1" if tower else "0",
        "wipe_tower_width": "60",
        "wipe_tower_brim_width": "2",
        "wipe_tower_extra_spacing": "100%",
        "brim_type": "outer_only",
        "brim_width": "0",
    }
    return "# made up for SlicerBridge's tests\n" + "".join(f"{k} = {v}\n" for k, v in lines.items())


def write_profiles(folder, key, missing=None):
    """Printer `key`'s made-up profiles in `folder`, as refresh_profile() leaves them; `missing`: {type: why}."""
    missing = missing or {}
    (folder / key).mkdir(parents=True, exist_ok=True)
    for t in prusa.PRINTERS[key].filaments:
        if t not in missing:
            (folder / key / f"{t}.ini").write_text(made_up_profile(key, t), encoding="utf-8")
    (folder / key / "meta.json").write_text(json.dumps({"format": prusa.FORMAT, "bundle": "test",
                                                        "presets": prusa.PRINTERS[key].filaments,
                                                        "missing": missing}), encoding="utf-8")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Made-up profiles for every Prusa printer and type, and no PrusaSlicer, even where one is installed."""
    folder = tmp_path / "prusa-profiles"
    for key in prusa.PRINTERS:
        write_profiles(folder, key)
    monkeypatch.setattr(prusa, "PROFILES", folder)
    monkeypatch.setattr(prusa, "find_prusaslicer", lambda: None)
    return folder


@pytest.mark.parametrize("key", list(prusa.PRINTERS))
def test_each_prusa_printer_gets_a_checked_file_per_plate(project, tmp_path, profiles, key, capsys):
    out = tmp_path / "out"
    assert prusa.convert(project, "Cube", [key], out) == (2, 0)  # the project has two plates
    files = prusa.printer_files(out / "Cube", "Cube", key)
    assert [f.name for f in files] == [f"Cube - {prusa.PRINTERS[key].label} - Plate {n}.3mf" for n in (1, 2)]
    for f in files:
        with zipfile.ZipFile(f) as z:
            assert z.comment.startswith(b"SlicerBridge")
            assert "Metadata/Slic3r_PE.config" in z.namelist()
    printed = capsys.readouterr().out
    assert "✗" not in printed and "FAIL" not in printed
    for n in ("1", "2", "3", "4", "5", "6", "7", "8", "11", "12"):  # 9 and 10 need PrusaSlicer
        assert printed.count(f"✓ {n}. ") == 2, n


def test_a_file_whose_colors_come_out_wrong_fails_check_7_and_is_not_written(project, tmp_path, profiles,
                                                                            monkeypatch, capsys):
    real = prusa.build_config

    def wrong_colors(*args, **kwargs):  # a bug that loses the model's red
        text, notes, carried = real(*args, **kwargs)
        return text.replace("#DE4343", "#000000"), notes, carried

    monkeypatch.setattr(prusa, "build_config", wrong_colors)
    assert prusa.convert(project, "Cube", ["xl"], tmp_path / "out") == (0, 2)
    assert not prusa.printer_files(tmp_path / "out" / "Cube", "Cube", "xl")
    assert "✗ 7. config colors == mapped palette" in capsys.readouterr().out


def test_a_wipe_tower_with_no_room_beside_the_model_is_warned_about(tmp_path, profiles, capsys):
    """Gatefang on the MMU3: PrusaSlicer stops slicing it, the tower runs into the model."""
    src = bambu_project(tmp_path / "wide.3mf", plates=1, size=215.0)  # fits the 250 x 220 bed, leaves no strip
    assert prusa.convert(src, "Wide", ["mmu3"], tmp_path / "out") == (1, 0)
    printed = capsys.readouterr().out
    assert "⚠ no spot for the wipe tower clear of the model" in printed and "⚠ tower" in printed
    small = bambu_project(tmp_path / "small.3mf", plates=1)
    assert prusa.convert(small, "Small", ["mmu3", "indx4"], tmp_path / "out") == (2, 0)  # roomy; no tower at all
    assert "no spot for the wipe tower" not in capsys.readouterr().out


def test_a_single_plate_file_has_no_plate_in_its_name(tmp_path, profiles):
    src = bambu_project(tmp_path / "one.3mf", plates=1)
    assert prusa.convert(src, "One", ["xl"], tmp_path / "out") == (1, 0)
    assert (tmp_path / "out" / "One" / "One - Prusa XL.3mf").exists()


def test_a_model_with_more_colors_than_tools_is_refused_and_writes_nothing(tmp_path, profiles):
    src = bambu_project(tmp_path / "five.3mf", paint={0: 2, 1: 3, 2: 4, 3: 5})  # five slots
    assert prusa.convert(src, "Five", ["indx4"], tmp_path / "out") == (0, 1)
    assert not prusa.printer_files(tmp_path / "out" / "Five", "Five", "indx4")


def _brims(f):
    """(the print profile's brim type and width, each object's own brim settings) in a written file."""
    with zipfile.ZipFile(f) as z:
        values, _read, _lines = prusa.read_3mf_config(z.read("Metadata/Slic3r_PE.config").decode())
        objects = ElementTree.fromstring(z.read("Metadata/Slic3r_PE_model.config")).findall("object")
    return ((values.get("brim_type"), values.get("brim_width")),
            [{m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key").startswith("brim")}
             for o in objects])


def test_a_project_wide_auto_brim_is_worked_out_per_object_not_printed_at_its_width(tmp_path, profiles, capsys):
    """Shamus: Bambu Studio works an automatic brim's width out per object and ignores brim_width. 0.4.0
    carried the width, so PrusaSlicer printed a fixed 5 mm brim on every object, one that gets none too."""
    src = bambu_project(tmp_path / "auto.3mf", plates=1,
                        extra_settings={"brim_type": "auto_brim", "brim_width": "5"})
    assert prusa.convert(src, "Auto", ["xl"], tmp_path / "out") == (1, 0)
    profile, objects = _brims(tmp_path / "out" / "Auto" / "Auto - Prusa XL.3mf")
    assert profile == ("outer_only", "0")  # the profile's, not the project's 5 mm
    assert objects == [{"brim_type": "no_brim"}]  # a 40 mm cube is too squat for an automatic brim
    assert "auto_brim worked out to no brim" in capsys.readouterr().out


def test_an_auto_brim_that_works_out_to_a_width_is_that_objects_outer_brim(tmp_path, profiles, monkeypatch):
    monkeypatch.setattr(autobrim, "brim_width", lambda *args, **kwargs: 6.5)  # as for a tall, narrow object
    src = bambu_project(tmp_path / "auto.3mf", plates=1,
                        extra_settings={"brim_type": "auto_brim", "brim_width": "5"})
    assert prusa.convert(src, "Auto", ["xl"], tmp_path / "out") == (1, 0)
    assert _brims(tmp_path / "out" / "Auto" / "Auto - Prusa XL.3mf") == (
        ("outer_only", "0"), [{"brim_type": "outer_only", "brim_width": "6.5"}])


def test_a_brim_of_a_set_width_is_still_carried_at_that_width(tmp_path, profiles):
    src = bambu_project(tmp_path / "set.3mf", plates=1,
                        extra_settings={"brim_type": "outer_only", "brim_width": "6"})
    assert prusa.convert(src, "Set", ["xl"], tmp_path / "out") == (1, 0)
    assert _brims(tmp_path / "out" / "Set" / "Set - Prusa XL.3mf") == (("outer_only", "6"), [{}])


def test_a_tall_print_gets_the_heavier_wipe_tower(tmp_path, profiles, capsys):
    src = bambu_project(tmp_path / "tall.3mf", plates=1, size=120.0)
    assert prusa.convert(src, "Tall", ["xl"], tmp_path / "out") == (1, 0)
    with zipfile.ZipFile(tmp_path / "out" / "Tall" / "Tall - Prusa XL.3mf") as z:
        values, _read, _lines = prusa.read_3mf_config(z.read("Metadata/Slic3r_PE.config").decode())
    assert values["wipe_tower_brim_width"] == "10" and values["wipe_tower_extra_spacing"] == "150%"
    assert "heavier wipe tower" in capsys.readouterr().out


def test_each_file_records_its_plate_mapping_and_placement(project, tmp_path, profiles):
    """What verify needs to re-check a file, and only the conversion knew."""
    out = tmp_path / "out"
    assert prusa.convert(project, "Cube", ["mmu3"], out) == (2, 0)
    for n, f in enumerate(prusa.printer_files(out / "Cube", "Cube", "mmu3"), start=1):
        assert prusa.read_record(f) == {"plate": n, "map": {1: 1, 3: 2, 5: 3}, "fits": True, "turned": False}
        assert output.ours(f)  # still SlicerBridge's: the record follows the stamp


def test_an_explicit_map_and_a_model_bigger_than_the_bed_are_recorded(tmp_path, profiles):
    src = bambu_project(tmp_path / "big.3mf", plates=1, size=300.0)  # bigger than the MMU3's 250 x 220 bed
    assert prusa.convert(src, "Big", ["mmu3"], tmp_path / "out", explicit={1: 2, 3: 1, 5: 3}) == (1, 0)
    made = prusa.read_record(tmp_path / "out" / "Big" / "Big - Prusa MMU3.3mf")
    assert made == {"plate": 1, "map": {1: 2, 3: 1, 5: 3}, "fits": False, "turned": False}


@pytest.mark.parametrize("comment", [b"SlicerBridge", b"convert-prusa.py", b"SlicerBridge {not json", b""])
def test_a_file_that_doesnt_say_how_it_was_made_has_no_record(tmp_path, comment):
    f = tmp_path / "old.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", "")
        z.comment = comment
    assert prusa.read_record(f) is None


# ---- re-checking a written file: verify (ADR-0001)

def _rewrite(f, name=None, change=None, comment=None):
    """Rewrite one entry of a written file (`change` maps its text to new text), or its zip comment."""
    with zipfile.ZipFile(f) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
        old_comment = z.comment
    with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
        for i, data in entries:
            z.writestr(i, change(data.decode()).encode() if i.filename == name else data)
        z.comment = old_comment if comment is None else comment


def _converted(project, tmp_path, key="xl", **kwargs):
    assert prusa.convert(project, "Cube", [key], tmp_path / "out", **kwargs)[1] == 0
    return prusa.printer_files(tmp_path / "out" / "Cube", "Cube", key)


def _numbers(lines):
    return sorted(int(line.split(".")[0]) for line in lines if line[:1].isdigit())


def test_a_written_file_passes_its_checks_again(project, tmp_path, profiles):
    for f in _converted(project, tmp_path):
        info, problems, warnings = prusa.audit(f, project, "xl")
        assert problems == []
        assert _numbers(info) == [1, 2, 3, 4, 5, 6, 7, 8, 11, 12]
        assert warnings == ["PrusaSlicer isn't installed: it doesn't load the file (check 9)"]


def test_without_the_source_the_file_alone_is_checked(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[0]
    info, problems, _w = prusa.audit(f, None, "xl")
    assert problems == [] and _numbers(info) == [1, 2, 3, 7, 8, 12]  # no paint, colors, modifiers or size
    assert "no --source" in info[0]


def test_a_wrong_color_fails_against_the_source(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[0]
    _rewrite(f, "Metadata/Slic3r_PE.config", lambda t: t.replace("#DE4343", "#000000"))
    problems = prusa.audit(f, project, "xl")[1]
    assert len(problems) == 1 and problems[0].startswith("7. config colors == mapped palette")
    assert prusa.audit(f, None, "xl")[1] == []  # nothing to compare the colors with


def test_a_model_moved_off_the_bed_fails_even_without_the_source(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[0]

    def off_the_bed(xml):
        return re.sub(r'(transform="(?:\S+ ){9})(\S+)', lambda m: f"{m.group(1)}{float(m.group(2)) + 1000:g}", xml)

    _rewrite(f, "3D/3dmodel.model", off_the_bed)
    assert [p.split(":")[0] for p in prusa.audit(f, None, "xl")[1]] == ["8. on the bed"]


def test_a_file_without_a_record_is_checked_with_the_usual_mapping_and_says_so(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[0]
    _rewrite(f, comment=b"SlicerBridge")  # as 0.4.2 and earlier wrote it
    _info, problems, warnings = prusa.audit(f, project, "xl")
    assert problems == []
    assert any("doesn't record its slot-to-tool mapping" in w and "[1,3,5] -> [1,2,3]" in w for w in warnings)


def test_an_old_file_of_a_model_bigger_than_the_bed_isnt_failed_without_the_source(tmp_path, profiles):
    src = bambu_project(tmp_path / "big.3mf", plates=1, size=300.0)  # bigger than the MMU3's bed
    assert prusa.convert(src, "Big", ["mmu3"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / "Big" / "Big - Prusa MMU3.3mf"
    assert prusa.audit(f, None, "mmu3")[1] == []  # its record says it's bigger than the bed: a warning
    _rewrite(f, comment=b"SlicerBridge")
    _info, problems, warnings = prusa.audit(f, None, "mmu3")
    assert problems == [] and any(w.startswith("8. on the bed") and "--source" in w for w in warnings)
    assert prusa.audit(f, src, "mmu3")[1] == []  # with the source, it's known


def test_a_file_converted_with_a_map_is_checked_with_that_map(project, tmp_path, profiles):
    custom = {1: 3, 3: 1, 5: 2}
    f = _converted(project, tmp_path, explicit=custom)[0]
    assert prusa.audit(f, project, "xl")[1] == []  # the record says which map
    _rewrite(f, comment=b"SlicerBridge")
    assert prusa.audit(f, project, "xl")[1]  # without it, the usual mapping is wrong for this file
    assert prusa.audit(f, project, "xl", explicit=custom)[1] == []  # verify --map puts it right


def test_the_wrong_source_is_named_as_such(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[1]  # plate 2
    one = bambu_project(tmp_path / "one.3mf", plates=1)
    problems = prusa.audit(f, one, "xl")[1]
    assert problems and "the source has no plate 2" in problems[0]


def test_verify_re_checks_prusa_files_from_the_command_line(project, tmp_path, profiles, capsys):
    folder = _converted(project, tmp_path)[0].parent
    assert main(["verify", str(folder), "--source", str(project)]) == 0
    assert main(["verify", str(folder)]) == 0
    assert "2/2 passed" in capsys.readouterr().out
    _rewrite(prusa.printer_files(folder, "Cube", "xl")[0], "Metadata/Slic3r_PE.config",
             lambda t: t.replace("#DE4343", "#000000"))
    assert main(["verify", str(folder), "--source", str(project)]) == 1
    printed = capsys.readouterr().out
    assert "1/2 passed" in printed and "convert again to re-check" not in printed


# ---- every main filament type: each tool its own type's preset

def _config(f):
    with zipfile.ZipFile(f) as z:
        return prusa.read_3mf_config(z.read("Metadata/Slic3r_PE.config").decode())[0]


def _typed(tmp_path, types, name):
    """One 20 mm part per filament, unpainted: filament n is part n, of types[n - 1]."""
    palette = ("#FFFFFF", "#3FBF6F", "#DE4343", "#5B6579", "#161616", "#CBC6B8", "#2E8B57", "#A0522D")[:len(types)]
    return bambu_project(tmp_path / f"{name}.3mf", palette=palette, types=list(types), plates=1, size=20.0,
                         parts=[(n, {}) for n in range(1, len(types) + 1)])


def _groups(key):
    """PLA beside each other type the printer has, as many as its tools hold at once."""
    others = [t for t in prusa.PRINTERS[key].filaments if t != "PLA"]
    room = prusa.PRINTERS[key].tools - 1
    return [["PLA", *others[i:i + room]] for i in range(0, len(others), room)]


@pytest.mark.parametrize("key", list(prusa.PRINTERS))
def test_each_tool_gets_its_own_types_preset(tmp_path, profiles, key, capsys):
    p = prusa.PRINTERS[key]
    for n, types in enumerate(_groups(key)):
        name = f"Types{n}"
        assert prusa.convert(_typed(tmp_path, types, name), name, [key], tmp_path / "out") == (1, 0), types
        values = _config(tmp_path / "out" / name / f"{name} - {p.label}.3mf")
        for tool, t in enumerate(types, start=1):
            temp, first, bed, first_bed = TEMPS[t]
            got = {k: prusa.tool_entry(values, k, tool, p.tools) for k in
                   ("filament_settings_id", "filament_type", "temperature", "first_layer_temperature",
                    "bed_temperature", "start_filament_gcode", "compatible_printers_condition_cummulative")}
            assert got == {"filament_settings_id": p.filaments[t], "filament_type": prusa.PRUSA_KINDS[t][0],
                           "temperature": str(temp), "first_layer_temperature": str(first),
                           "bed_temperature": str(bed), "start_filament_gcode": f"M900 K{temp / 10000:g} ; {t}\\n",
                           "compatible_printers_condition_cummulative": t}, (key, tool)
        # the tools the model doesn't use keep PLA's
        for tool in range(len(types) + 1, p.tools + 1):
            assert prusa.tool_entry(values, "filament_settings_id", tool, p.tools) == p.filaments["PLA"]
        assert values["compatible_printers_condition_cummulative"].startswith('"printer_model==\\"X\\"";')
        printed = capsys.readouterr().out
        assert "✓ 12. each tool's filament is its type's preset" in printed and "✗" not in printed
        assert f"filaments: T1 PLA ({p.filaments['PLA']}, 215 C), T2 {types[1]} ({p.filaments[types[1]]}" in printed


def test_every_type_a_prusa_printer_takes_is_a_main_type_with_pla_first():
    for key, p in prusa.PRINTERS.items():
        assert next(iter(p.filaments)) == "PLA", key
        assert set(p.filaments) <= set(prusa.PRUSA_KINDS) < set(printers.MAIN_TYPES), key
        assert all(name.startswith(("Prusament ", "Prusa ", "Generic ")) for name in p.filaments.values()), key
    assert "TPU-AMS" not in prusa.PRUSA_KINDS  # Prusa's flex presets are 95A TPUs


@pytest.mark.parametrize("key, types, why", [
    ("xl", ["PLA", "PCTG"], "slot 2 (T2) is PCTG, and PrusaSlicer has no Prusament or Generic PCTG preset for the Prusa XL"),
    ("mmu3", ["PLA", "TPU"], "slot 2 (T2) is TPU, and PrusaSlicer has no flexible filament preset for the Prusa MMU3 at all"),
    ("indx4", ["PLA", "TPU-AMS"], "slot 2 (T2) is Bambu's TPU for AMS (TPU-AMS, about 68D), and PrusaSlicer has no preset"),
    ("xl", ["PLA", "PET-CF"], "slot 2 (T2) is PET-CF, a type SlicerBridge doesn't convert"),
    ("mmu3", ["PLA", "HIPS", "PA-CF"], "slot 2 (T2) is HIPS, and PrusaSlicer has no Prusament or Generic HIPS preset "
                                       "for the Prusa MMU3; slot 3 (T3) is PA-CF"),
])
def test_a_type_a_prusa_printer_has_no_preset_for_is_refused_by_filament_with_why(tmp_path, profiles, capsys,
                                                                                   key, types, why):
    assert prusa.convert(_typed(tmp_path, types, "Odd"), "Odd", [key], tmp_path / "out") == (0, 1)
    assert not prusa.printer_files(tmp_path / "out" / "Odd", "Odd", key)
    printed = capsys.readouterr().out
    assert f"REFUSING Odd for the {prusa.PRINTERS[key].label}: {why}" in printed
    assert "PLA prints as" not in printed


def test_a_type_whose_profile_couldnt_be_built_is_refused_with_why_and_the_rest_convert(tmp_path, profiles, capsys):
    write_profiles(profiles, "xl", missing={"PC": "'Prusament PC Blend @XL' is typed PET in this Prusa bundle, not PC"})
    assert prusa.convert(_typed(tmp_path, ["PLA", "PC"], "Pc"), "Pc", ["xl"], tmp_path / "out") == (0, 1)
    assert "its PC profile couldn't be built: 'Prusament PC Blend @XL' is typed PET" in capsys.readouterr().out
    assert prusa.convert(_typed(tmp_path, ["PLA", "PETG"], "Petg"), "Petg", ["xl"], tmp_path / "out") == (1, 0)


def test_the_mmu3_says_its_filaments_share_one_nozzle_and_every_printer_one_bed(tmp_path, profiles, capsys):
    src = _typed(tmp_path, ["PLA", "PC"], "Hot")
    assert prusa.convert(src, "Hot", ["mmu3", "xl"], tmp_path / "out") == (2, 0)
    printed = capsys.readouterr().out
    assert printed.count("⚠ one nozzle for T1 PLA at 215 C, T2 PC at 275 C: PrusaSlicer slices them together") == 1
    assert printed.count("⚠ one bed for T1 PLA at 60 C (first layer 60), T2 PC at 110 C (first layer 110): "
                         "PrusaSlicer heats it for the first filament each layer prints") == 2
    assert "Bed temperature by extruder" in printed
    # PLA and PVA: one bed temperature, so nothing to say; the INDX's heads are apart
    assert prusa.convert(_typed(tmp_path, ["PLA", "PVA"], "Cool"), "Cool", ["indx4"], tmp_path / "out") == (1, 0)
    printed = capsys.readouterr().out
    assert "one bed" not in printed and "one nozzle" not in printed


def test_check_12_fails_a_tool_that_prints_another_type(tmp_path, profiles):
    src = _typed(tmp_path, ["PLA", "PETG"], "Two")
    assert prusa.convert(src, "Two", ["xl"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / "Two" / "Two - Prusa XL.3mf"
    assert prusa.audit(f, src, "xl")[1] == [] and prusa.audit(f, None, "xl")[1] == []
    as_pla = f.with_name("as pla.3mf")
    shutil.copy(f, as_pla)
    _rewrite(as_pla, "Metadata/Slic3r_PE.config", lambda t: t.replace('"Prusament PETG @XL"', '"Prusament PLA @XLIS"'))
    problems = prusa.audit(as_pla, src, "xl")[1]
    assert len(problems) == 1 and problems[0].startswith("12. each tool's filament is its type's preset")
    assert "T2 is PETG, and its preset is 'Prusament PLA @XLIS', not 'Prusament PETG @XL'" in problems[0]
    # Without the source, the file alone: a preset typed against its own kind still fails.
    retyped = f.with_name("retyped.3mf")
    shutil.copy(f, retyped)
    _rewrite(retyped, "Metadata/Slic3r_PE.config",
             lambda t: re.sub(r"(; filament_type = )\w+;\w+", r"\1PLA;PLA", t))
    problems = prusa.audit(retyped, None, "xl")[1]
    assert len(problems) == 1 and "T2 is PETG, and its filament is typed PLA" in problems[0]


def test_a_tool_at_another_temperature_than_its_preset_is_a_warning(tmp_path, profiles):
    """A file converted with an older Prusa bundle: its preset has moved on, its type hasn't."""
    src = _typed(tmp_path, ["PLA", "PETG"], "Two")
    assert prusa.convert(src, "Two", ["xl"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / "Two" / "Two - Prusa XL.3mf"
    _rewrite(f, "Metadata/Slic3r_PE.config", lambda t: t.replace("; temperature = 215,240,", "; temperature = 215,250,"))
    _info, problems, warnings = prusa.audit(f, src, "xl")
    assert problems == []
    assert any(w.startswith("12.") and "T2 prints at 250/245 C, its preset here at 240/245 C" in w for w in warnings)


def test_composing_keeps_each_entry_as_prusaslicer_writes_it():
    base = prusa.Profile("x", [("nozzle_diameter", "0.4,0.4,0.4"), ("temperature", "215,215,215"),
                              ("gcode", '"a;b";"a;b";"a;b"'), ("cond", "c"),
                              ("compatible_printers_condition_cummulative", '"p";"x";"x";"x"')], {})
    base.values.update(base.items)
    other = prusa.Profile("y", [], {"nozzle_diameter": "0.4,0.4,0.4", "temperature": "250,250,250",
                                    "gcode": '"say \\"hi\\";";"say \\"hi\\";";"say \\"hi\\";"', "cond": "c",
                                    "compatible_printers_condition_cummulative": '"p";"y";"y";"y"'})
    got = prusa.compose(base, {"PLA": base, "PETG": other}, ["PETG", "PLA", "PETG"]).values
    assert got == {"nozzle_diameter": "0.4,0.4,0.4", "temperature": "250,215,250",
                   "gcode": '"say \\"hi\\";";"a;b";"say \\"hi\\";"', "cond": "c",
                   "compatible_printers_condition_cummulative": '"p";"y";"x";"y"'}
    # A filament setting PrusaSlicer holds once (filament_vendor) is the first tool's, as it takes it; one
    # it records only where some preset has it is empty for the others, and left out where all are.
    other.values.update(cond="d", compatible_prints_condition_cummulative='"q";"q";"q"')
    got = prusa.compose(base, {"PLA": base, "PETG": other}, ["PLA", "PETG", "PLA"]).values
    assert got["cond"] == "c" and got["compatible_prints_condition_cummulative"] == ';"q";'
    assert prusa.compose(base, {"PLA": base, "PETG": other}, ["PETG", "PLA", "PLA"]).values["cond"] == "d"
    assert "compatible_prints_condition_cummulative" not in prusa.compose(base, {"PLA": base}, ["PLA"] * 3).values
    other.values["temperature"] = "250"  # per tool in one profile and not in another: two bundles
    with pytest.raises(Refusal, match="differ in temperature, held per tool in some and not others"):
        prusa.compose(base, {"PLA": base, "PETG": other}, ["PETG", "PLA", "PLA"])


def test_building_a_types_profile_checks_its_preset_is_that_type(tmp_path, monkeypatch, capsys):
    """PrusaSlicer's installed 2.4.14 bundle types BVOH as PVA, say: a type whose preset says otherwise
    is left out, why kept, and every other type built."""
    monkeypatch.setattr(prusa, "PROFILES", tmp_path / "prusa-profiles")
    monkeypatch.setattr(prusa, "find_prusaslicer", lambda: "prusa-slicer")
    monkeypatch.setattr(prusa, "prusaslicer_version", lambda: "2.9.7")
    monkeypatch.setattr(prusa, "ensure_datadir", lambda: tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "PrusaResearch.ini").write_text("config_version = 9.9.9\n", encoding="utf-8")
    by_preset = {v: k for k, v in prusa.PRINTERS["xl"].filaments.items()}

    def save(cmd, timeout):
        preset = cmd[cmd.index("--material-profile") + 1]
        t = by_preset[preset]
        if t == "HIPS":
            return subprocess.CompletedProcess(cmd, 1, "", f"Material profile '{preset}' is not compatible")
        text = made_up_profile("xl", t)
        if t == "PC":
            text = text.replace("filament_type = PC;PC;PC;PC;PC", "filament_type = PET;PET;PET;PET;PET")
        if t == "PA-CF":
            text = text.replace("filament_abrasive = 1,1,1,1,1", "filament_abrasive = 0,0,0,0,0")
        Path(cmd[cmd.index("--save") + 1]).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(prusa, "run_slicer", save)
    missing = prusa.refresh_profile("xl")
    assert set(missing) == {"HIPS", "PC", "PA-CF"}
    assert "is not compatible" in missing["HIPS"]
    assert missing["PC"] == "'Prusament PC Blend @XL' is typed PET in this Prusa bundle, not PC"
    assert "is not marked abrasive, and PA-CF is a fibre-filled filament" in missing["PA-CF"]
    assert prusa.profiles_built("xl") and not (tmp_path / "prusa-profiles" / "xl" / "PC.ini").exists()
    assert prusa.load_profile("xl", "PETG").values["temperature"].startswith("240,")
    with pytest.raises(Refusal, match="its HIPS profile couldn't be built"):
        prusa.load_profile("xl", "HIPS")
    printed = capsys.readouterr().out
    assert "!  no PC for it" in printed and "(Prusa bundle 9.9.9)" in printed


def test_profiles_of_the_old_format_or_another_table_are_rebuilt(tmp_path, profiles):
    assert prusa.profiles_built("xl")
    meta = json.loads((profiles / "xl" / "meta.json").read_text())
    (profiles / "xl" / "meta.json").write_text(json.dumps({**meta, "format": 1}))
    assert not prusa.profiles_built("xl")
    (profiles / "xl" / "meta.json").write_text(json.dumps({**meta, "presets": {"PLA": "Prusament PLA @XLIS"}}))
    assert not prusa.profiles_built("xl")


def test_profiles_another_prusaslicer_saved_are_rebuilt(tmp_path, profiles, monkeypatch):
    """PrusaSlicer 2.9.7 added used_filament_compensation, which profiles 2.9.6 saved lack."""
    meta = json.loads((profiles / "xl" / "meta.json").read_text())
    (profiles / "xl" / "meta.json").write_text(json.dumps({**meta, "prusaslicer": "2.9.6"}))
    assert prusa.profiles_built("xl")  # no PrusaSlicer to rebuild them with: the ones there are used
    monkeypatch.setattr(prusa, "prusaslicer_version", lambda: "2.9.6")
    assert prusa.profiles_built("xl")
    monkeypatch.setattr(prusa, "prusaslicer_version", lambda: "2.9.7")
    assert not prusa.profiles_built("xl")
    (profiles / "xl" / "meta.json").write_text(json.dumps(meta))  # saved before profiles said which
    assert not prusa.profiles_built("xl")


def test_building_profiles_records_the_prusaslicer_that_saved_them(tmp_path, monkeypatch):
    monkeypatch.setattr(prusa, "PROFILES", tmp_path / "prusa-profiles")
    monkeypatch.setattr(prusa, "find_prusaslicer", lambda: "prusa-slicer")
    monkeypatch.setattr(prusa, "prusaslicer_version", lambda: "2.9.7")
    monkeypatch.setattr(prusa, "ensure_datadir", lambda: tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "PrusaResearch.ini").write_text("config_version = 2.5.13\n", encoding="utf-8")

    def save(cmd, timeout):
        preset = cmd[cmd.index("--material-profile") + 1]
        t = {v: k for k, v in prusa.PRINTERS["xl"].filaments.items()}[preset]
        Path(cmd[cmd.index("--save") + 1]).write_text(made_up_profile("xl", t), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(prusa, "run_slicer", save)
    prusa.refresh_profile("xl")
    assert json.loads((tmp_path / "prusa-profiles" / "xl" / "meta.json").read_text())["prusaslicer"] == "2.9.7"
    assert prusa.profiles_built("xl")


def test_a_painted_models_tools_take_their_slots_types(tmp_path, profiles):
    """Paint in slots 3 and 5 on a slot 1 cube: tools 1, 2 and 3 are slots 1, 3 and 5, and their types."""
    src = bambu_project(tmp_path / "painted.3mf", types=["PLA", "PLA", "PETG", "PLA", "ASA"], plates=1)
    assert prusa.convert(src, "Painted", ["mmu3"], tmp_path / "out") == (1, 0)
    values = _config(tmp_path / "out" / "Painted" / "Painted - Prusa MMU3.3mf")
    presets = [prusa.tool_entry(values, "filament_settings_id", t, 5) for t in range(1, 6)]
    assert presets == ["Prusament PLA @COREONE", "Prusament PETG @COREONE", "Prusament ASA @COREONE",
                       "Prusament PLA @COREONE", "Prusament PLA @COREONE"]

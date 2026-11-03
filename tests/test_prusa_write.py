"""The prusa engine's write path without PrusaSlicer, so CI runs it: convert_one(), its checks, and
landing the files.

PrusaSlicer builds each printer's profile from Prusa's own bundle, which SlicerBridge never ships. The
profiles here are made up: only the settings the engine reads, with each printer's preset name and
tool count. Without PrusaSlicer, checks 9 and 10 (PrusaSlicer loading and slicing the file) skip
themselves, as they do on a machine without it.
"""
import re
import zipfile

import pytest

from builders import bambu_project
from slicerbridge import output
from slicerbridge.cli import main
from slicerbridge.engines import prusa

# key: (bed width, depth, height, wipe tower)
BEDS = {"mmu3": (250, 220, 270, True), "xl": (360, 360, 360, True),
        "indx4": (250, 220, 270, False), "indx8": (250, 220, 270, False)}


def made_up_profile(key: str) -> str:
    """A profile the engine can load for `key`: none of Prusa's own settings, only what it reads."""
    p = prusa.PRINTERS[key]
    w, d, h, tower = BEDS[key]
    tools = p.tools
    lines = {
        "printer_settings_id": p.preset,
        "print_settings_id": p.print_preset,
        "filament_settings_id": ";".join([p.filament_preset] * tools),
        "nozzle_diameter": ",".join(["0.4"] * tools),
        "bed_shape": f"0x0,{w}x0,{w}x{d},0x{d}",
        "max_print_height": str(h),
        "filament_colour": ";".join(["#FFFFFF"] * tools),
        "extruder_colour": ";".join(["#FFFFFF"] * tools),
        "layer_height": "0.15",
        "first_layer_height": "0.2",
        "perimeters": "2",
        "fill_density": "15%",
        "support_material": "0",
        "wipe_tower": "1" if tower else "0",
        "wipe_tower_width": "60",
        "wipe_tower_brim_width": "2",
        "wipe_tower_extra_spacing": "100%",
    }
    return "# made up for SlicerBridge's tests\n" + "".join(f"{k} = {v}\n" for k, v in lines.items())


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Made-up profiles for every Prusa printer, and no PrusaSlicer, even where one is installed."""
    folder = tmp_path / "prusa-profiles"
    folder.mkdir()
    for key in prusa.PRINTERS:
        (folder / f"{key}.ini").write_text(made_up_profile(key), encoding="utf-8")
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
    for n in ("1", "2", "3", "4", "5", "6", "7", "8", "11"):  # 9 and 10 need PrusaSlicer
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


def test_a_single_plate_file_has_no_plate_in_its_name(tmp_path, profiles):
    src = bambu_project(tmp_path / "one.3mf", plates=1)
    assert prusa.convert(src, "One", ["xl"], tmp_path / "out") == (1, 0)
    assert (tmp_path / "out" / "One" / "One - Prusa XL.3mf").exists()


def test_a_model_with_more_colors_than_tools_is_refused_and_writes_nothing(tmp_path, profiles):
    src = bambu_project(tmp_path / "five.3mf", paint={0: 2, 1: 3, 2: 4, 3: 5})  # five slots
    assert prusa.convert(src, "Five", ["indx4"], tmp_path / "out") == (0, 1)
    assert not prusa.printer_files(tmp_path / "out" / "Five", "Five", "indx4")


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
        assert _numbers(info) == [1, 2, 3, 4, 5, 6, 7, 8, 11]
        assert warnings == ["PrusaSlicer isn't installed: it doesn't load the file (check 9)"]


def test_without_the_source_the_file_alone_is_checked(project, tmp_path, profiles):
    f = _converted(project, tmp_path)[0]
    info, problems, _w = prusa.audit(f, None, "xl")
    assert problems == [] and _numbers(info) == [1, 2, 3, 7, 8]  # no paint, colors, modifiers or size
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

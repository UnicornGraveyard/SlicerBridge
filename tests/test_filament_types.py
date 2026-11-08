"""Every main filament type: each printer's own preset for it, never another type's, and what a
printer's slicer says about two of them together. No slicer: stand-ins where one would run."""
import json
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from builders import bambu_project
from slicerbridge import printers, templates
from slicerbridge.checks import orca as check, u1 as u1check
from slicerbridge.engines import orca, u1
from test_tpu import BAND, KEY, _settings, _u1
from test_tpu import slicer as stand_in  # noqa: F401  (a fixture: the orca engine stand-in)

MAIN = printers.MAIN_TYPES
BAMBU = [k for k, p in printers.PRINTERS.items() if p.slicer == "bambu"]


def _project(tmp_path, types, name="Band"):
    palette = ("#FFFFFF", "#3FBF6F", "#DE4343", "#5B6579")[:len(types)]
    parts = [(i + 1, {}) for i in range(len(types))] if len(types) > 2 else BAND
    return bambu_project(tmp_path / f"{name}.3mf", palette=palette, types=types, plates=1, parts=parts)


# ------------------------------------------------------------------ printers ----

def test_every_main_type_prints_as_itself():
    assert all(printers.BAMBU_TYPES[t] == t for t in MAIN)
    assert printers.BAMBU_TYPES["PETG-HF"] == "PETG"  # Snapmaker Orca's, typed PETG in its own preset
    assert set(printers.BAMBU_TYPES) == {*MAIN, "PETG-HF"}


def test_every_printer_takes_pla_first_and_only_main_types():
    for key, p in printers.PRINTERS.items():
        assert next(iter(p.filaments)) == "PLA", key  # the base every file is composed on
        assert set(p.filaments) <= set(MAIN), key


@pytest.mark.parametrize("key", BAMBU)
def test_every_bambu_printer_has_bambu_studios_generic_preset_for_each_type(key):
    f = printers.PRINTERS[key].filaments
    assert all(name.startswith("Generic ") for name in f.values())
    # The open A1 mini: Bambu Studio has no ABS, ASA, PC or nylon for it.
    lacking = {"ABS", "ASA", "PC", "PA", "PA-CF"} if key == "a1mini" else set()
    assert set(f) == set(MAIN) - lacking


def test_presets_that_print_at_another_types_temperature_are_left_out():
    assert {"ABS", "ASA", "PVA"}.isdisjoint(printers.PRINTERS["kobrax"].filaments)  # Anycubic's at 205 C
    assert "PETG-CF" not in printers.PRINTERS["creator5"].filaments


def test_a_type_a_printer_has_no_preset_for_is_refused_by_name():
    why = orca.no_preset([{"type": "PLA"}, {"type": "HIPS"}], printers.PRINTERS["k2se"])
    assert why.startswith("filament 2 is HIPS, and its slicer has no HIPS preset here")
    assert orca.no_preset([{"type": t} for t in MAIN if t != "TPU-AMS"], printers.PRINTERS["k2plus"]) == ""


def test_a_preset_takes_what_it_includes_over_its_parents_and_its_own_over_both(tmp_path):
    """Bambu Studio 2.9.1's X1C has its start G-code in an included template file: left out, the CLI
    used a generic one. The H2D's PLA Tough+ 0.6 keeps its own 0.98 flow over its template's 0.95."""
    files = {
        "base": {"name": "base", "machine_start_gcode": "generic", "filament_flow_ratio": ["1"], "keep": "base"},
        "tpl": {"name": "tpl", "instantiation": "false", "machine_start_gcode": ";===== machine: X1-0.4",
                "filament_flow_ratio": ["0.95"]},
        "x1c": {"name": "x1c", "inherits": "base", "include": ["tpl"], "filament_flow_ratio": ["0.98"]},
    }
    index = {}
    for name, d in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(d), encoding="utf-8")
        index[("machine", name)] = tmp_path / f"{name}.json"
    flat = templates.flatten("machine", "x1c", index)
    assert flat["machine_start_gcode"] == ";===== machine: X1-0.4"  # the include, over the parent
    assert flat["filament_flow_ratio"] == ["0.98"]  # its own, over the include
    assert flat["keep"] == "base" and flat["name"] == "x1c" and "include" not in flat and "instantiation" not in flat
    del index[("machine", "tpl")]
    with pytest.raises(Exception, match="includes 'tpl'"):
        templates.flatten("machine", "x1c", index)


def test_off_range_names_a_preset_that_contradicts_itself():
    kobra_x_abs = {"nozzle_temperature": ["205"], "nozzle_temperature_initial_layer": ["215"],
                   "nozzle_temperature_range_low": ["230"], "nozzle_temperature_range_high": ["280"]}
    assert templates.off_range(kobra_x_abs) == ("its nozzle_temperature is 205 C, outside the 230-280 C its own "
                                                "preset recommends")
    assert templates.off_range({**kobra_x_abs, "nozzle_temperature": ["260"], "nozzle_temperature_initial_layer":
                                ["260"]}) == ""
    assert templates.off_range({"nozzle_temperature": ["205"]}) == ""  # no range to hold it to


def test_a_single_value_goes_on_each_variant_where_another_filament_keeps_one_per_variant():
    """The H2D's PLA (three variants) keeps one toolchange retraction, its Generic TPU (four) one per variant."""
    assert orca.lined_up([["nil"], ["nil"] * 4], [3, 4]) == ["nil"] * 7
    assert orca.lined_up([["220"] * 3, ["240"] * 4], [3, 4]) == ["220"] * 3 + ["240"] * 4
    assert orca.lined_up([["PLA"], ["TPU"]], [3, 4]) == ["PLA", "TPU"]  # one per filament stays so
    assert orca.lined_up([["0.4"], ["0.8"]], [0, 0]) == ["0.4", "0.8"]  # a printer without variants


# ------------------------------------------------------- mixed temperatures ----

def _temps(semm="1", **types):
    """Templates for each type: (temperature, recommended low, high)."""
    return {t: {"nozzle_temperature": [str(a)], "nozzle_temperature_range_low": [str(lo)],
                "nozzle_temperature_range_high": [str(hi)], "single_extruder_multi_material": semm}
            for t, (a, lo, hi) in types.items()}


def test_orcaslicer_wont_slice_two_filaments_outside_each_others_range_on_one_nozzle():
    k2 = _temps(PLA=(220, 190, 230), PETG=(250, 220, 270), TPU=(220, 200, 250))
    note = orca.mixed_note([{"type": "PLA"}, {"type": "PETG"}], k2, "k2plus")
    assert note.startswith("filament 2 (PETG) prints at 250 C, outside the 190-230 C filament 1 (PLA) recommends")
    assert note.endswith("Remove mixed temperature restriction is on")
    assert orca.mixed_note([{"type": "PLA"}, {"type": "TPU"}], k2, "k2plus") == ""
    # A toolchanger's heads are apart, and Bambu Studio has no such rule.
    assert orca.mixed_note([{"type": "PLA"}, {"type": "PETG"}], _temps("0", PLA=(220, 190, 240),
                                                                       PETG=(255, 230, 270)), "zrultras") == ""
    assert orca.mixed_note([{"type": "PLA"}, {"type": "ABS"}], _temps(PLA=(220, 190, 240), ABS=(270, 240, 280)),
                           "x1c") == ""


def test_snapmaker_orca_wont_slice_a_high_temperature_filament_with_a_low_one():
    assert u1.mixed_note(["PLA", "ABS"]) == ("extruder 2 (ABS) prints hot and 1 (PLA) cool: Snapmaker Orca won't "
                                            "slice them together until Preferences > Allow high/low temperature "
                                            "filament mixing is on")
    assert u1.mixed_note(["ASA", "PC", "TPU"]).startswith("extruders 1 (ASA), 2 (PC) print hot and 3 (TPU) cool")
    assert u1.mixed_note(["PLA", "PETG", "TPU"]) == ""  # PETG mixes with either
    assert u1.mixed_note(["ABS", "PETG", "PC"]) == ""


@pytest.mark.usefixtures("stand_in")
def test_the_slice_check_says_which_preference_and_slices_with_it_on(tmp_path, monkeypatch):
    _made, f = _convert(tmp_path, _project(tmp_path, ["PLA", "TPU"]))
    runs = []

    def run(cmd, timeout, cwd=None):
        cmd = [str(c) for c in cmd]
        runs.append(cmd)
        if "--allow-mix-temp" not in cmd:
            return subprocess.CompletedProcess(cmd, 194, "run found error, exit | Selected nozzle temperatures are "
                                               "incompatible. Each filament's nozzle temperature must fall", "")
        out = Path(cmd[cmd.index("--outputdir") + 1])
        (out / "plate_1.gcode").write_text("T0\nG1 X1 Y1 E1\nT1\nG1 X2 Y2 E2\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(printers.SLICERS, "orca", replace(printers.SLICERS["orca"], app=tmp_path / "orca"))
    monkeypatch.setattr(check, "run_slicer", run)
    result = check.slice_check(f, KEY)
    assert result.exit == 0 and len(runs) == 2 and "--allow-mix-temp" in runs[1]
    _info, problems, warnings = result.text()
    assert problems == []
    assert warnings[0].startswith("OrcaSlicer won't slice these filaments together until Preferences > Control > "
                                  "Slicing > Remove mixed temperature restriction is on")


def test_snapmaker_orca_with_no_switch_is_checked_on_a_copy_with_its_hot_filaments_typed_petg(tmp_path, monkeypatch):
    f = _u1(tmp_path, _project(tmp_path, ["PLA", "ABS"]))
    sliced = []

    def run(cmd, timeout, cwd=None):
        cmd = [str(c) for c in cmd]
        with zipfile.ZipFile(cmd[-1]) as z:
            types = json.loads(z.read("Metadata/project_settings.config"))["filament_type"]
        sliced.append((cmd, types))
        if "ABS" in types:
            return subprocess.CompletedProcess(cmd, 194, "Cannot print multiple filaments which have large difference "
                                               "of temperature together.\nrun found error, exit", "")
        out = Path(cmd[cmd.index("--outputdir") + 1])
        (out / "plate_1.gcode").write_text("T0\nG1 X1 Y1 E1\nT1\nG1 X2 Y2 E2\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(printers.SLICERS, "snapmaker", replace(printers.SLICERS["snapmaker"], app=tmp_path / "snap"))
    monkeypatch.setattr(check, "run_slicer", run)
    result = check.slice_check(f, "u1")
    assert result.exit == 0 and [t[:2] for _c, t in sliced] == [["PLA", "ABS"], ["PLA", "PETG"]]
    assert not any("--allow-mix-temp" in c for c, _t in sliced)  # an invalid option to it
    _info, problems, warnings = result.text()
    assert problems == [] and "sliced on a copy with ABS typed PETG, at the file's own temperatures" in warnings[0]
    assert _settings(f)["filament_type"][1] == "ABS"  # the file itself keeps its type


# ------------------------------------------------------------- orca engine ----

def _convert(tmp_path, src, key=KEY):
    made = orca.convert(src, "Band", [key], tmp_path / "out")
    return made, tmp_path / "out" / "Band" / f"Band - {printers.PRINTERS[key].label}.3mf"


@pytest.mark.usefixtures("stand_in")
def test_a_type_whose_template_couldnt_be_made_is_refused_with_why(tmp_path, monkeypatch, capsys):
    built = templates.load(KEY)[1]
    monkeypatch.setattr(templates, "load", lambda key, install=True: (
        {"filament_keys": [], "missing": {"TPU": "its nozzle_temperature is 205 C, outside the 230-280 C"}},
        {"PLA": built["PLA"]}))
    made, f = _convert(tmp_path, _project(tmp_path, ["PLA", "TPU"]))
    assert made == (0, 1) and not f.exists()
    assert "its slicer has no TPU template here (its nozzle_temperature is 205 C" in capsys.readouterr().out


def test_a_failed_type_leaves_the_printers_other_templates_built(tmp_path, monkeypatch):
    """templates.build(): only PLA, the base, failing fails the printer."""
    p = printers.PRINTERS["k2plus"]
    exe = tmp_path / "orca"
    exe.write_text("")
    monkeypatch.setitem(printers.SLICERS, "orca", replace(printers.SLICERS["orca"], app=exe))
    monkeypatch.setattr(templates, "TEMPLATES", tmp_path / "templates")
    monkeypatch.setattr(templates, "preset_index", lambda vendor, slicer: {
        ("machine", p.machine): exe, ("process", p.process): exe})
    monkeypatch.setattr(templates, "_flat_for_cli", lambda kind, name, index: {})
    monkeypatch.setattr(templates, "_for_cli", lambda kind, name, index, tmp, printer=None: exe)
    monkeypatch.setattr(templates, "filament_keys", lambda settings, slicer: [])
    monkeypatch.setattr(templates, "app_version", lambda slicer: "2.4.2")

    def one(slicer, printer, ftype, name, index, machine, process, flat_machine, flat_process, tmp):
        if ftype == "ASA":
            return f"{name} isn't used for {printer.label}: its nozzle_temperature is 205 C", ()
        out = tmp / "out" / f"{ftype}.3mf"
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("Metadata/project_settings.config", json.dumps({"filament_type": [ftype]}))
        return "", ([], out)

    monkeypatch.setattr(templates, "_one_template", one)
    assert templates.build("k2plus") == []
    meta = json.loads((tmp_path / "templates" / "k2plus" / "meta.json").read_text(encoding="utf-8"))
    assert list(meta["missing"]) == ["ASA"] and "205 C" in meta["missing"]["ASA"]
    built = {f.stem for f in (tmp_path / "templates" / "k2plus").glob("*.3mf")}
    assert built == set(p.filaments) - {"ASA"}

    monkeypatch.setattr(templates, "_one_template", lambda *a: ("Creality Generic PLA @K2-all is missing", ()))
    assert templates.build("k2plus") == ["Creality Generic PLA @K2-all is missing"]


def test_a_slicer_that_hangs_or_crashes_twice_keeps_nothing(tmp_path, monkeypatch):
    """Not a fact about the type: a cached "no ASA here" would outlast the hang."""
    p = printers.PRINTERS["k2plus"]
    exe = tmp_path / "orca"
    exe.write_text("")
    monkeypatch.setitem(printers.SLICERS, "orca", replace(printers.SLICERS["orca"], app=exe))
    monkeypatch.setattr(templates, "TEMPLATES", tmp_path / "templates")
    monkeypatch.setattr(templates, "preset_index", lambda vendor, slicer: {
        ("machine", p.machine): exe, ("process", p.process): exe, **{("filament", n): exe for n in p.filaments.values()}})
    monkeypatch.setattr(templates, "_flat_for_cli", lambda kind, name, index: {})
    monkeypatch.setattr(templates, "_for_cli", lambda kind, name, index, tmp, printer=None: exe)
    runs = []

    def hang(cmd, timeout, cwd=None):
        runs.append(cmd)
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(templates, "run_slicer", hang)
    problems = templates.build("k2plus")
    assert len(runs) == 2 and problems[0].startswith("OrcaSlicer's command line hung saving the Creality K2 Plus")
    assert not (tmp_path / "templates" / "k2plus").exists()

    monkeypatch.setattr(templates, "run_slicer", lambda cmd, timeout, cwd=None: subprocess.CompletedProcess(cmd, -11))
    assert "crashed saving the Creality K2 Plus with Creality Generic PLA @K2-all, twice (exit -11)" in \
        templates.build("k2plus")[0]


# --------------------------------------------------------------- u1 engine ----

@pytest.mark.parametrize("src_type", ["PCTG", "ASA", "PC", "PA", "PLA-CF", "PETG-CF", "PA-CF", "PVA", "BVOH"])
def test_each_type_gets_snapmaker_orcas_own_u1_preset(tmp_path, src_type):
    src = _project(tmp_path, ["PLA", src_type])
    f = _u1(tmp_path, src)
    s = _settings(f)
    preset = u1.U1_PRESETS[src_type]
    want = u1.u1_filaments()["presets"][preset]["values"]
    assert s["filament_type"][:2] == ["PLA", src_type] and s["filament_settings_id"][1] == preset
    for k in ("nozzle_temperature", "nozzle_temperature_range_low", "filament_max_volumetric_speed", "filament_ids"):
        assert s[k][1] == want[k], k
    assert templates.off_range(want) == ""
    _info, problems = u1check.audit(f, src)
    assert problems == []


def test_a_type_the_u1_has_no_preset_for_is_refused(tmp_path, capsys):
    assert u1.convert(_project(tmp_path, ["PLA", "HIPS"]), "Band", ["u1"], tmp_path / "out") == (0, 1)
    out = capsys.readouterr().out
    assert "filament 2 is HIPS, and Snapmaker Orca has no U1 preset for it here" in out
    assert not (tmp_path / "out" / "Band" / "Band - Snapmaker U1.3mf").exists()


def test_the_u1_says_when_snapmaker_orca_wont_mix_its_temperatures(tmp_path, capsys):
    _u1(tmp_path, _project(tmp_path, ["PLA", "ABS"]))
    assert "extruder 2 (ABS) prints hot and 1 (PLA) cool" in capsys.readouterr().out


def test_check_3b_fails_an_older_u1_file_that_printed_asa_as_pla(tmp_path):
    src = _project(tmp_path, ["PLA", "ASA"])
    f = _u1(tmp_path, src)
    s = _settings(f)
    old = f.with_name("old.3mf")
    with zipfile.ZipFile(f) as zin, zipfile.ZipFile(old, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps({**s, "filament_type": ["PLA", "PLA", "PLA", "PLA"]}).encode()
            zout.writestr(item, data)
    _info, problems = u1check.audit(old, src)
    assert "extruder 2 is PLA, its source slot 2 ASA" in problems

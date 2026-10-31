"""The Snapmaker U1's file sliced in Snapmaker Orca, with a stand-in for its command line."""
import json
import zipfile

import pytest

from builders import bambu_project
from slicerbridge import printers
from slicerbridge.checks import orca as check
from slicerbridge.engines import u1 as engine


def _gcode(tower_at, time="1h 10m 5s", grams="12.5"):
    """One plate's G-code: 3 mm of model, 1.5 mm of prime tower at `tower_at`, two tools."""
    x, y = tower_at
    return (f"; HEADER_BLOCK_START\n; model printing time: 1h 2m; total estimated time: 1h 10m\nM83\nT0\n"
            f";TYPE:Outer wall\nG1 X10 Y10 E2.5\n;TYPE:Prime tower\nG1 X{x} Y{y} E1\nG1 X{x + 10} Y{y} E0.5\n"
            f"G1 E-0.8\nT1\n;TYPE:Inner wall\nG1 X12 Y12 E0.5\n"
            f"; estimated printing time (normal mode) = {time}\n; total filament used [g] = {grams}\n")


@pytest.fixture
def snapmaker(tmp_path, monkeypatch):
    """A Snapmaker Orca that writes a plate's G-code with its tower where the file puts it, or fails."""
    app = tmp_path / "Snapmaker Orca" / "snapmaker-orca"
    app.parent.mkdir()
    app.write_text("")
    monkeypatch.setenv("SLICERBRIDGE_SNAPMAKER_ORCA", str(app))  # and no Homebrew
    monkeypatch.setitem(printers.SLICERS, "snapmaker", printers.Slicer("Snapmaker Orca", app))
    runs = []

    def run(cmd, timeout, cwd=None):
        runs.append([str(c) for c in cmd])
        if fake.fails:
            return check.subprocess.CompletedProcess(cmd, 1, "", "[error] Nothing to be sliced")
        if fake.crashes:
            return check.subprocess.CompletedProcess(cmd, -11, "", "[error] calc_exclude_triangles:Unable to "
                                                     "create exclude triangles")
        out = cmd[cmd.index("--outputdir") + 1]
        with zipfile.ZipFile(cmd[-1]) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
        wont = [f"{k}: {v} not in range [0,5]" for k, v in fake.refuses.items() if cfg.get(k) == v]
        if wont:  # as Snapmaker Orca says it, and then `noise` lines more
            return check.subprocess.CompletedProcess(cmd, 238, "", "run found error, exit\nParam values in "
                                                     "3mf/config error: \n" + "\n".join(wont) + "".join(
                                                         f"\n[info] cleaning up {i}" for i in range(fake.noise)))
        for n, (x, y) in enumerate(zip(cfg["wipe_tower_x"], cfg["wipe_tower_y"]), start=1):
            (check.Path(out) / f"plate_{n}.gcode").write_text(_gcode((float(x), float(y))))
        return check.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(check, "run_slicer", run)
    fake = type("Fake", (), {"runs": runs, "fails": False, "crashes": False, "refuses": {}, "noise": 0, "app": app})
    return fake


def test_gcode_facts_has_the_slicers_estimate_and_the_towers_share(tmp_path):
    g = tmp_path / "plate_1.gcode"
    g.write_text(_gcode((200.0, 200.0)))
    facts = check.gcode_facts(g)
    assert facts["time"] == "1h 10m 5s" and facts["grams"] == 12.5
    assert facts["tower_share"] == pytest.approx(1.5 / 4.5)  # the retraction isn't filament laid
    assert dict(facts["tools"]) == {0: 1, 1: 1}
    bambu = tmp_path / "bambu.gcode"
    bambu.write_text("; total estimated time: 2h 3m\n; total filament weight [g] : 40.10\n")
    assert (check.gcode_facts(bambu)["time"], check.gcode_facts(bambu)["grams"]) == ("2h 3m", 40.1)


def test_the_u1_file_is_sliced_in_snapmaker_orca(project, tmp_path, snapmaker, capsys):
    assert engine.convert(project, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (1, 0)
    (cmd,) = snapmaker.runs
    assert cmd[0] == str(snapmaker.app)
    assert "--allow-newer-file" in cmd  # its version check refuses every project, its own too
    out = capsys.readouterr().out
    assert "sliced" in out and "1h 10m 5s, 12 g" in out and "(33% of the filament)" in out
    assert (tmp_path / "out" / "Cube" / "Cube - Snapmaker U1.3mf").exists()


def test_a_u1_file_snapmaker_orca_wont_slice_isnt_written(project, tmp_path, snapmaker, capsys):
    snapmaker.fails = True
    assert engine.convert(project, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (0, 1)
    assert "Snapmaker Orca didn't slice it" in capsys.readouterr().out
    assert not list((tmp_path / "out" / "Cube").glob("*.3mf"))


def test_without_snapmaker_orca_the_file_is_written_unsliced(project, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nowhere" / "snapmaker-orca"
    monkeypatch.setenv("SLICERBRIDGE_SNAPMAKER_ORCA", str(missing))
    monkeypatch.setitem(printers.SLICERS, "snapmaker", printers.Slicer("Snapmaker Orca", missing))
    assert engine.convert(project, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (1, 0)
    assert "⚠ not sliced: Snapmaker Orca isn't at" in capsys.readouterr().out


def test_snapmaker_orca_is_found_where_it_installs():
    s = printers.SLICERS["snapmaker"]
    assert s.cask == "snapmaker-orca" and s.exe == "Snapmaker_Orca"
    mac = printers.Slicer(s.name, check.Path("/Applications/Snapmaker Orca.app"), exe=s.exe)
    assert mac.binary.as_posix() == "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker_Orca"
    assert printers.env_var(s) == "SLICERBRIDGE_SNAPMAKER_ORCA"


def _u1_settings(tmp_path):
    with zipfile.ZipFile(tmp_path / "out" / "Cube" / "Cube - Snapmaker U1.3mf") as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def test_bambu_studios_auto_keeps_the_u1s_own_value(tmp_path, capsys):
    """Some Snapmaker Orca versions won't load -1 for these, Bambu Studio's auto: Gatefang's did."""
    src = bambu_project(tmp_path / "auto.3mf", extra_settings={
        "raft_first_layer_expansion": "-1", "tree_support_wall_count": "-1", "brim_width": "6"})
    assert engine.convert(src, "Cube", ["u1"], tmp_path / "out") == (1, 0)
    s, t = _u1_settings(tmp_path), engine.load_template(False)
    assert (s["raft_first_layer_expansion"], s["tree_support_wall_count"]) == (
        t["raft_first_layer_expansion"], t["tree_support_wall_count"])
    assert s["brim_width"] == "6"  # the rest is carried
    assert "kept the U1's own raft_first_layer_expansion" in capsys.readouterr().out


def test_a_setting_snapmaker_orca_wont_load_keeps_the_templates_and_slices(tmp_path, snapmaker, capsys):
    src = bambu_project(tmp_path / "wide.3mf", extra_settings={"brim_width": "6", "wall_loops": "4"})
    snapmaker.refuses = {"brim_width": "6"}
    assert engine.convert(src, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (1, 0)
    assert len(snapmaker.runs) == 2  # refused, then sliced without it
    s = _u1_settings(tmp_path)
    assert s["brim_width"] == engine.load_template(False)["brim_width"] and s["wall_loops"] == "4"
    assert "brim_width" not in s["different_settings_to_system"][0].split(";")
    out = capsys.readouterr().out
    assert "kept the U1's own brim_width" in out and "Snapmaker Orca won't take 6" in out


def test_a_crash_in_snapmaker_orcas_command_line_keeps_the_file(project, tmp_path, snapmaker, capsys):
    """Snapmaker Orca 2.4.0's command line crashed on Gatefang's U1 file; its app sliced it."""
    snapmaker.crashes = True
    assert engine.convert(project, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (1, 0)
    assert len(snapmaker.runs) == 2  # check 13 gives a crash a second try
    out = capsys.readouterr().out
    assert "⚠ not sliced: Snapmaker Orca's command line crashed on it (exit -11), twice" in out
    assert out.count("crashed") == 1  # one warning, not check 13's "was run again" as well
    assert (tmp_path / "out" / "Cube" / "Cube - Snapmaker U1.3mf").exists()


def test_a_setting_snapmaker_orca_wont_load_is_read_from_its_whole_log(tmp_path, snapmaker, capsys):
    """Check 13 shows the log's last six lines; the refusal can come before them."""
    src = bambu_project(tmp_path / "wide.3mf", extra_settings={"brim_width": "6"})
    snapmaker.refuses, snapmaker.noise = {"brim_width": "6"}, 10
    assert engine.convert(src, "Cube", ["u1"], tmp_path / "out", slice_it=True) == (1, 0)
    assert len(snapmaker.runs) == 2  # refused, then sliced without it
    assert "kept the U1's own brim_width" in capsys.readouterr().out


def test_a_crash_that_repeats_fails_check_13_unless_the_engine_excuses_it():
    s = check.Slice("OrcaSlicer", exit=-11, retried=-11, tail=["boom"])
    assert s.crashed and s.text() == ([], ["OrcaSlicer didn't slice it (exit -11): boom"],
                                      ["OrcaSlicer crashed (exit -11) and was run again"])
    s.crash_note = "not sliced: its command line crashed"
    assert s.text() == ([], [], ["not sliced: its command line crashed"])
    assert check.Slice("Snapmaker Orca", skipped="not sliced: not installed").text() == (
        [], [], ["not sliced: not installed"])

"""The Snapmaker U1's file sliced in Snapmaker Orca, with a stand-in for its command line."""
import json
import zipfile

import pytest

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
        out = cmd[cmd.index("--outputdir") + 1]
        with zipfile.ZipFile(cmd[-1]) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
        for n, (x, y) in enumerate(zip(cfg["wipe_tower_x"], cfg["wipe_tower_y"]), start=1):
            (check.Path(out) / f"plate_{n}.gcode").write_text(_gcode((float(x), float(y))))
        return check.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(check, "run_slicer", run)
    fake = type("Fake", (), {"runs": runs, "fails": False, "app": app})
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

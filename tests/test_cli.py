import os
import subprocess
import sys

from slicerbridge import __version__
from slicerbridge.cli import main


def test_version(capsys):
    try:
        main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert __version__ in capsys.readouterr().out


def test_printers_lists_every_printer(capsys):
    assert main(["printers"]) == 0
    assert "30 printers" in capsys.readouterr().out


def test_inspect_reports_used_slots_and_fit(project, capsys):
    assert main(["inspect", str(project), "--printers", "u1"]) == 0
    out = capsys.readouterr().out
    assert "uses 3: 1->1, 3->2, 5->3" in out and "plate 2 on the Snapmaker U1: fits" in out


def test_convert_writes_into_out_name(project, tmp_path, capsys):
    out = tmp_path / "files"
    assert main(["convert", str(project), "--name", "Cube", "-p", "u1", "-o", str(out), "--prefix", "Shop - "]) == 0
    assert (out / "Cube" / "Shop - Cube - Snapmaker U1.3mf").exists()
    assert main(["verify", str(out / "Cube"), "--source", str(project)]) == 0
    assert "1/1 passed" in capsys.readouterr().out


def test_names_windows_cant_hold_are_made_safe(project, tmp_path, capsys):
    """A file made on a Mac is still downloaded to Windows, where a ':' or '?' won't extract."""
    out = tmp_path / "files"
    assert main(["convert", str(project), "--name", "Dragon: Part 1?", "-p", "u1", "-o", str(out)]) == 0
    assert (out / "Dragon- Part 1-" / "Dragon- Part 1- - Snapmaker U1.3mf").exists()
    assert "isn't a file name Windows can open" in capsys.readouterr().out
    assert main(["convert", str(project), "--name", "Cube", "-p", "u1", "-o", str(out), "--suffix", "A/B"]) == 0
    assert (out / "Cube" / "Cube - Snapmaker U1 - A-B.3mf").exists()
    assert "isn't a file name Windows can open" in capsys.readouterr().out


def test_a_name_with_nothing_left_is_refused(project, tmp_path):
    """Only dots and spaces would leave an empty name: " - Snapmaker U1.3mf", loose in the output folder."""
    try:
        main(["convert", str(project), "--name", ". .", "-p", "u1", "-o", str(tmp_path / "files")])
    except SystemExit as e:
        assert "leaves nothing to name the files with" in str(e)
    else:
        raise AssertionError("a name of only dots and spaces should be refused")
    assert not (tmp_path / "files").exists()


def test_marks_survive_a_pipe(project, tmp_path):
    """Piped, Windows' stdout is its code page (cp1252), which has no ✓: printing one used to crash."""
    out = tmp_path / "files"
    assert main(["convert", str(project), "--name", "Cube", "-p", "u1", "-o", str(out)]) == 0
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    run = subprocess.run([sys.executable, "-m", "slicerbridge", "verify", str(out / "Cube")], capture_output=True, env=env)
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    assert "✓ pass" in run.stdout.decode("utf-8")


def test_map_is_for_prusa_only(project):
    try:
        main(["convert", str(project), "-p", "u1", "--map", "1=1"])
    except SystemExit as e:
        assert "Prusa printers only" in str(e)
    else:
        raise AssertionError("--map with a non-Prusa printer should be refused")


def test_not_a_3mf(tmp_path):
    try:
        main(["inspect", str(tmp_path / "missing.3mf")])
    except SystemExit as e:
        assert "not a .3mf file" in str(e)


def test_nothing_is_left_in_the_working_folder(project, tmp_path, monkeypatch):
    """Slicer command lines write logs (00000.log) where they're run: never in the user's folder."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert main(["convert", str(project), "-p", "u1", "-o", str(tmp_path / "files")]) == 0
    assert list(work.iterdir()) == []

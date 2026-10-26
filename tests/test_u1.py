"""The Snapmaker U1 engine, end to end: it needs no slicer."""
import zipfile

from builders import bambu_project
from slicerbridge.checks import u1 as check
from slicerbridge.engines import u1 as engine


def test_converts_and_compacts_the_used_slots(project, tmp_path):
    assert engine.convert(project, "Test Cube", ["u1"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / "Test Cube" / "Test Cube - Snapmaker U1.3mf"
    info, problems = check.audit(f, project)
    assert problems == []
    with zipfile.ZipFile(f) as z:
        assert z.comment == b"SlicerBridge"
        assert [c.upper()[:7] for c in check.palette(z)][:3] == ["#161616", "#DE4343", "#2E8B57"]  # slots 1, 3, 5
        assert check.histogram(check.paint_codes(z)) == {2: 4, 3: 4}


def test_more_colors_than_the_u1_holds_is_refused(tmp_path):
    src = bambu_project(tmp_path / "five.3mf", paint={0: 2, 1: 3, 2: 4, 3: 5})
    assert engine.convert(src, "Five", ["u1"], tmp_path / "out") == (0, 1)
    assert not (tmp_path / "out" / "Five").exists() or not any((tmp_path / "out" / "Five").glob("*.3mf"))


def test_a_file_slicerbridge_didnt_write_is_moved_aside(project, tmp_path):
    out = tmp_path / "out" / "Test Cube"
    out.mkdir(parents=True)
    theirs = out / "Test Cube - Snapmaker U1.3mf"
    with zipfile.ZipFile(theirs, "w") as z:
        z.writestr("hello.txt", "not ours")
    engine.convert(project, "Test Cube", ["u1"], tmp_path / "out")
    assert (out / "_previous" / theirs.name).exists()
    assert zipfile.ZipFile(theirs).comment == b"SlicerBridge"

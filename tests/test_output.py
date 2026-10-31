"""Landing a file and taking one back, the same for every engine; and one reader for a source's palette."""
import json
import zipfile

from builders import bambu_project
from slicerbridge import output, printers, source
from slicerbridge.engines import prusa, u1


def _zip(path, comment=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.comment = comment
        z.writestr("x", "x")
    return path


def test_a_file_lands_over_ours_and_beside_theirs(tmp_path, capsys):
    final = tmp_path / "Cube - Prusa XL.3mf"
    _zip(final, printers.STAMP)  # ours, from an earlier run
    output.land(_zip(output.partial(final), printers.STAMP), final)
    assert output.ours(final) and not output.partial(final).exists()
    for n in range(2):  # someone else's file goes aside, never over an earlier backup
        _zip(final)
        output.land(_zip(output.partial(final), printers.STAMP), final)
    assert sorted(f.name for f in (tmp_path / "_previous").iterdir()) == [
        "Cube - Prusa XL (2).3mf", "Cube - Prusa XL.3mf"]
    assert "moved the existing Cube - Prusa XL.3mf (not made by SlicerBridge)" in capsys.readouterr().out


def test_withdraw_takes_back_the_partial_file_and_ours_but_never_theirs(tmp_path, capsys):
    ours, theirs = _zip(tmp_path / "ours.3mf", printers.STAMP), _zip(tmp_path / "theirs.3mf")
    _zip(output.partial(ours))
    output.withdraw(ours, "it failed")
    output.withdraw(theirs, "it failed")
    assert not ours.exists() and not output.partial(ours).exists() and theirs.exists()
    assert capsys.readouterr().out.strip() == "removed the earlier ours.3mf: it failed"


def test_a_prusa_printer_that_fails_takes_back_its_files(project, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "out" / "Cube"
    mine = [_zip(folder / printers.out_name("Cube", "xl", plate=f" - Plate {n}"), printers.STAMP) for n in (1, 2)]
    others = [_zip(folder / printers.out_name("Cube", "mmu3", plate=" - Plate 1"), printers.STAMP),
              _zip(folder / printers.out_name("Cube", "xl", plate=" - Plate 3"))]  # theirs

    def broken(src, key, *args, **kwargs):  # halfway through plate 1
        _zip(output.partial(mine[0]))
        raise RuntimeError("boom")

    monkeypatch.setattr(prusa, "convert_one", broken)
    assert prusa.convert(project, "Cube", ["xl"], tmp_path / "out") == (0, 1)
    assert not any(f.exists() for f in mine) and not list(folder.glob("partial - *"))
    assert all(f.exists() for f in others)
    assert capsys.readouterr().out.count("removed the earlier Cube - Prusa XL - Plate") == 2


def test_a_prusa_printers_files_are_its_own_and_no_others(tmp_path):
    folder = tmp_path / "Cube"
    names = [printers.out_name("Cube", "xl", "", p, "UG") for p in ("", " - Plate 2")]
    for n in [*names, printers.out_name("Cube", "indx4", "", "", "UG"), printers.out_name("Cube", "xl")]:
        _zip(folder / n)
    _zip(output.partial(folder / printers.out_name("Cube", "xl", "", " - Plate 7", "UG")))
    assert [f.name for f in prusa.printer_files(folder, "Cube", "xl", suffix="UG")] == sorted(
        [*names, printers.out_name("Cube", "xl", "", " - Plate 7", "UG")])


def test_a_sliced_sources_stale_slice_info_doesnt_set_the_palette(tmp_path):
    """slice_info.config lists what a slice used, as it was then; project_settings.config is the palette."""
    src = bambu_project(tmp_path / "cube.3mf")
    stale = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(stale, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "Metadata/slice_info.config":
                data = data.replace(b"</config>", b'<plate><filament id="1" type="PLA" color="#00FF00"/>'
                                    b'<filament id="3" type="PLA" color="#0000FF"/></plate></config>')
            zout.writestr(item, data)
    want = json.loads(zipfile.ZipFile(src).read("Metadata/project_settings.config"))["filament_colour"]
    assert [f["color"] for f in source.palette(stale)] == want
    assert u1.convert(stale, "Cube", ["u1"], tmp_path / "out") == (1, 0)

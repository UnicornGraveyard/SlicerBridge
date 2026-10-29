"""The Snapmaker U1 engine, end to end: it needs no slicer."""
import json
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


TOWER_KEYS = ("prime_tower_brim_width", "wipe_tower_extra_rib_length", "wipe_tower_rib_width",
              "wipe_tower_extra_spacing", "wipe_tower_max_purge_speed")


def _u1_file(tmp_path, size: float, name: str = "Tall"):
    src = bambu_project(tmp_path / f"{name}.3mf", plates=1, size=size)
    assert engine.convert(src, name, ["u1"], tmp_path / "out") == (1, 0)
    return src, tmp_path / "out" / name / f"{name} - Snapmaker U1.3mf"


def _settings(f):
    with zipfile.ZipFile(f) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def _template():
    with zipfile.ZipFile(engine.template_path(False)) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def test_a_tall_plate_with_room_gets_the_heavier_tower(tmp_path, capsys):
    src, f = _u1_file(tmp_path, 120.0)
    s = _settings(f)
    assert {k: s[k] for k in TOWER_KEYS} == engine.TALL_TOWER
    assert "heavier prime tower" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("heavier prime tower set" in line for line in info)


def test_a_tall_plate_whose_strip_cant_hold_the_wider_base_gets_only_the_slower_tower(tmp_path, capsys):
    src, f = _u1_file(tmp_path, 230.0)  # leaves a strip about 35 mm wide for the tower
    s, t = _settings(f), _template()
    assert s["wipe_tower_max_purge_speed"] == engine.TALL_TOWER_TIGHT["wipe_tower_max_purge_speed"]
    assert {k: s[k] for k in TOWER_KEYS[:4]} == {k: t[k] for k in TOWER_KEYS[:4]}
    assert "⚠ 230 mm tall" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("slower tower only" in line for line in info)


def test_a_plate_at_the_limit_keeps_the_templates_tower(tmp_path, capsys):
    _src, f = _u1_file(tmp_path, 100.0)
    s, t = _settings(f), _template()
    assert {k: s[k] for k in TOWER_KEYS} == {k: t[k] for k in TOWER_KEYS}
    assert "prime tower (" not in capsys.readouterr().out


def test_check_12_fails_a_tall_file_on_the_light_tower(tmp_path):
    src, f = _u1_file(tmp_path, 120.0)
    t = _template()
    light = f.with_name("light.3mf")
    with zipfile.ZipFile(f) as zin, zipfile.ZipFile(light, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps({**json.loads(data), **{k: t[k] for k in TOWER_KEYS}}).encode()
            zout.writestr(item, data)
    _info, problems = check.audit(light, src)
    assert any("on the light prime tower" in p and "prime_tower_brim_width" in p for p in problems)


def test_a_tall_plate_with_room_for_the_brim_but_not_the_longer_ribs_gets_the_rest(tmp_path, capsys):
    src, f = _u1_file(tmp_path, 195.0, "Snug")
    s, t = _settings(f), _template()
    assert {k: s[k] for k in TOWER_KEYS} == {**{k: t[k] for k in TOWER_KEYS}, **engine.TALL_TOWER_STOCK_RIBS}
    assert "on the printer's own ribs" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("on its own ribs set" in line for line in info)

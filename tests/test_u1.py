"""The Snapmaker U1 engine, end to end: it needs no slicer."""
import json
import zipfile
from xml.etree import ElementTree

from builders import bambu_project
from slicerbridge import tall
from slicerbridge._vendor.panda2prusa import autobrim
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


def _tower(step):
    """The template's tower brought up to one of tall.py's steps: the same towers every other printer gets."""
    t = _template()
    return {k: {**t, **tall.settings(t, step["orca"])}[k] for k in TOWER_KEYS}


def test_a_tall_plate_with_room_gets_the_heavier_tower(tmp_path, capsys):
    src, f = _u1_file(tmp_path, 120.0)
    s = _settings(f)
    assert {k: s[k] for k in TOWER_KEYS} == _tower(tall.TOWER)
    assert "heavier prime tower" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("heavier prime tower set" in line for line in info)


def test_a_tall_plate_whose_strip_cant_hold_the_wider_base_gets_only_the_slower_tower(tmp_path, capsys):
    src, f = _u1_file(tmp_path, 230.0)  # leaves a strip about 35 mm wide for the tower
    s, t = _settings(f), _template()
    assert {k: s[k] for k in TOWER_KEYS} == _tower(tall.SLOWER) and float(s["wipe_tower_max_purge_speed"]) <= 60
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
    # 170 mm leaves the tower a 93 mm strip. On the template's ribs the heavier tower prints about
    # 57 mm wide inside a 10 mm brim; on the longer ones, 68. (At the 30 mm once assumed, 195 mm left room.)
    src, f = _u1_file(tmp_path, 170.0, "Snug")
    s, t = _settings(f), _template()
    assert {k: s[k] for k in TOWER_KEYS} == _tower(tall.STOCK_RIBS) != {k: t[k] for k in TOWER_KEYS}
    assert "on the printer's own ribs" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("on its own ribs set" in line for line in info)


def test_the_tower_is_placed_at_the_size_snapmaker_orca_prints_it(tmp_path):
    # SoftMaw's fit: 200 mm of model across the 270 mm bed, 4 colors at 0.2 mm. Snapmaker Orca printed that
    # tower 47.0 x 56.1 mm inside its 5 mm brim, and placed as 30 x 40 it ran off the bed (2026-11-24).
    src = bambu_project(tmp_path / "wide.3mf", plates=1, size=50.0, parts=[(n, {}) for n in (1, 2, 3, 4)],
                        extra_settings={"layer_height": "0.2"})
    assert engine.convert(src, "Wide", ["u1"], tmp_path / "out") == (1, 0)
    f = tmp_path / "out" / "Wide" / "Wide - Snapmaker U1.3mf"
    s = _settings(f)
    brim = float(s["prime_tower_brim_width"])
    tx, ty = float(s["wipe_tower_x"][0]), float(s["wipe_tower_y"][0])
    tower = (tx - brim, ty - brim, tx + 47.0 + brim, ty + 56.1 + brim)
    assert tower[0] >= 0.5 and tower[1] >= 1.0 and tower[2] <= 270.5 and tower[3] <= 271.0, tower
    with zipfile.ZipFile(f) as z:
        ((_n, lo, hi),) = check.plate_boxes(z)
    assert not (tower[0] < hi[0] and tower[2] > lo[0] and tower[1] < hi[1] and tower[3] > lo[1]), (tower, lo, hi)


def test_a_tower_check_12_finds_room_for_is_what_the_file_gets(tmp_path, monkeypatch, capsys):
    # Placement's estimate keeps slack that check 12 doesn't ask for at the spot the tower got: where the
    # check owes a tall file a heavier tower than placement chose, the file gets that one, at the same spot.
    slower = tall.STEPS["orca"][-1]
    monkeypatch.setattr(engine.orca_engine, "tall_tower",
                        lambda slug, plates, cfg, slicer: (tall.settings(cfg, slower[1]), slower[0]))
    src, f = _u1_file(tmp_path, 120.0)
    assert {k: _settings(f)[k] for k in TOWER_KEYS} == _tower(tall.TOWER)
    assert "over 100: heavier prime tower (" in capsys.readouterr().out
    info, problems = check.audit(f, src)
    assert problems == [] and any("heavier prime tower set" in line for line in info)


AUTO_BRIM = {"brim_type": "auto_brim", "brim_width": "5"}


def _object_settings(f):
    """{object id: its own settings} in the file's model_settings.config."""
    with zipfile.ZipFile(f) as z:
        root = ElementTree.fromstring(z.read("Metadata/model_settings.config"))
    return {o.get("id"): {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")}
            for o in root.findall("object")}


def _brim_file(tmp_path, monkeypatch, widths, name="Brim", **project):
    """A project's U1 file, where Bambu Studio's formula gives its objects' automatic brims `widths` in turn."""
    widths = iter(widths)
    monkeypatch.setattr(autobrim, "brim_width", lambda *args, **kwargs: next(widths))
    src = bambu_project(tmp_path / f"{name}.3mf", **project)
    assert engine.convert(src, name, ["u1"], tmp_path / "out") == (1, 0)
    return tmp_path / "out" / name / f"{name} - Snapmaker U1.3mf"


def test_an_automatic_brim_bambu_studio_leaves_out_is_no_brim_on_the_u1(tmp_path, monkeypatch, capsys):
    # SoftMaw: tall enough for an 11 mm automatic brim, which Bambu Studio leaves out of a print with supports.
    # Carried as "auto_brim", Snapmaker Orca printed all 11 mm of it, into the bed's edge (2026-11-24).
    f = _brim_file(tmp_path, monkeypatch, [11.0], plates=1, extra_settings={**AUTO_BRIM, "enable_support": "1"})
    s = _settings(f)
    assert s["brim_type"] == "no_brim" and "brim_type" in s["different_settings_to_system"][0].split(";")
    assert "no brim: Bambu Studio's automatic brim" in capsys.readouterr().out


def test_an_automatic_brim_bambu_studio_prints_is_left_to_snapmaker_orcas(tmp_path, monkeypatch, capsys):
    f = _brim_file(tmp_path, monkeypatch, [6.5], plates=1, extra_settings=AUTO_BRIM)  # no supports: it's printed
    assert _settings(f)["brim_type"] == "auto_brim" and "brim_type" not in _object_settings(f)["2"]
    assert "no brim" not in capsys.readouterr().out


def test_objects_bambu_studio_brims_apart_are_told_apart_on_the_u1(tmp_path, monkeypatch):
    f = _brim_file(tmp_path, monkeypatch, [0.0, 11.0], plates=2, extra_settings=AUTO_BRIM)  # squat, then tall
    assert _settings(f)["brim_type"] == "auto_brim"
    objects = _object_settings(f)
    assert objects["3"]["brim_type"] == "no_brim" and "brim_type" not in objects["4"]


def test_another_slicers_automatic_brim_is_its_own(tmp_path, monkeypatch):
    # An OrcaSlicer project's automatic brim is OrcaSlicer's, which Snapmaker Orca works out the same way.
    f = _brim_file(tmp_path, monkeypatch, [11.0], plates=1, extra_settings={
        **AUTO_BRIM, "enable_support": "1", "printer_model": "Creality K2 Plus",
        "printer_settings_id": "Creality K2 Plus 0.4 nozzle"})
    assert _settings(f)["brim_type"] == "auto_brim" and "brim_type" not in _object_settings(f)["2"]

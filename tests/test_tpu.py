"""TPU, both of Bambu's, and a model whose colors are its parts': each printer's preset for it,
and what the orca and u1 engines write for it. No slicer: the orca engine's is a stand-in."""
import json
import zipfile

import pytest

from builders import bambu_project
from slicerbridge import printers, templates
from slicerbridge.checks import orca as check, u1 as u1check
from slicerbridge.engines import orca, u1

BAMBU = [k for k, p in printers.PRINTERS.items() if p.slicer == "bambu"]
# A PLA part on filament 1 and a TPU band, solid, as its own part on filament 2: no paint.
BAND = [(1, {}), (2, {"sparse_infill_density": "100%"})]


def _settings(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def _project(tmp_path, types, name="Band", **kw):
    palette = ("#FFFFFF", "#3FBF6F", "#DE4343")[:len(types)]
    return bambu_project(tmp_path / f"{name}.3mf", palette=palette, types=types, plates=1, parts=BAND, **kw)


# ------------------------------------------------------------------ printers ----

def test_bambus_two_tpus_stay_apart():
    assert printers.BAMBU_TYPES["TPU"] == "TPU" and printers.BAMBU_TYPES["TPU-AMS"] == "TPU-AMS"


@pytest.mark.parametrize("key", BAMBU)
def test_every_bambu_printer_has_a_preset_for_each_tpu(key):
    f = printers.PRINTERS[key].filaments
    assert f["TPU"].startswith("Generic TPU") and "AMS" not in f["TPU"]
    assert f["TPU-AMS"].startswith("Generic TPU for AMS")


def test_no_orca_family_printer_takes_tpu_for_ams_and_the_creator_5s_take_no_tpu():
    family = {k: p for k, p in printers.PRINTERS.items() if p.slicer == "orca"}
    assert not [k for k, p in family.items() if "TPU-AMS" in p.filaments]
    assert {k for k, p in family.items() if "TPU" not in p.filaments} == {"creator5", "creator5pro"}


def test_soft_tpu_is_loaded_by_hand_where_a_filament_changer_feeds_the_colors():
    pla, tpu, ams = {"type": "PLA"}, {"type": "TPU"}, {"type": "TPU-AMS"}
    assert "external spool, loaded by hand" in orca.tpu_notes([pla, tpu], "h3c")[0]
    assert orca.tpu_notes([pla, tpu, tpu], "x1c")[0].startswith("filaments 2, 3 are TPU")
    assert orca.tpu_notes([pla, ams], "x1c") == []  # TPU for AMS goes through the AMS
    assert "through the CFS" in orca.tpu_notes([tpu, pla], "k2plus")[0]
    assert orca.tpu_notes([pla, tpu], "zrultras") == []  # a toolchanger: each head has its spool


def test_a_printer_without_the_preset_says_which_filament_and_why():
    why = orca.no_preset([{"type": "PLA"}, {"type": "TPU-AMS"}], printers.PRINTERS["k2plus"])
    assert why.startswith("filament 2 is Bambu's TPU for AMS (TPU-AMS)") and "soft TPU's preset isn't" in why
    assert orca.no_preset([{"type": "TPU"}], printers.PRINTERS["creator5"]).startswith("filament 1 is TPU, and")
    assert orca.no_preset([{"type": "TPU"}, {"type": "TPU-AMS"}], printers.PRINTERS["x1c"]) == ""


def test_a_rack_of_hotends_gets_one_per_filament_up_to_the_stock_rack():
    h3c = {"handle_hotend_as_extruder": "1", "nozzle_volume_type": ["Standard"], "extruder_max_nozzle_count": ["9"],
           "default_nozzle_volume_type": ["Standard"] * 7 + ["TPU285", "Standard"]}
    assert [orca.rack_hotends(h3c, n) for n in (1, 2, 9)] == [[1], [2], [8]]
    assert orca.rack_hotends({"nozzle_volume_type": ["Standard", "Standard"]}, 4) == [1, 1]  # the H2D's, the H2C's


def test_a_filament_goes_on_a_nozzle_its_preset_prints_on():
    """filament_printable has a bit per extruder: the H2D's Generic TPU is 2, extruder 2 only; the X2D's 1."""
    assert orca.printable_map({"filament_map": ["1", "1"], "filament_printable": ["3", "2"]}) == ["1", "2"]
    assert orca.printable_map({"filament_map": ["1", "1"], "filament_printable": ["3", "1"]}) == ["1", "1"]
    assert orca.printable_map({"filament_map": ["2", "2"], "filament_printable": ["1", "3"]}) == ["1", "2"]
    assert orca.printable_map({"filament_map": ["1"]}) == ["1"]  # an OrcaSlicer printer: nothing to go by
    plate = '<plate><metadata key="filament_maps" value="1 1 1"/></plate>'
    assert 'value="1 2"' in orca.remap_model_settings(plate, {1: 1, 3: 2}, [1, 3], ["1", "2"])
    assert 'value="1 1"' in orca.remap_model_settings(plate, {1: 1, 3: 2}, [1, 3])


# ------------------------------------------------- orca engine (stand-in slicer) ----

KEY = "zrultras"  # an OrcaSlicer toolchanger with a TPU preset


@pytest.fixture
def slicer(tmp_path, monkeypatch):
    """A KEY template per type, made from the U1's as test_orca_write.py's is, and an OrcaSlicer
    stand-in that reads every setting back as written."""
    p = printers.PRINTERS[KEY]
    source = u1.load_template(False)
    n = len(source["filament_colour"])
    filament_keys = sorted(k for k, v in source.items() if isinstance(v, list) and len(v) == n
                           and (k.startswith("filament_") or k == "nozzle_temperature"))
    base = {k: [v[0]] if k in filament_keys else v for k, v in source.items()}
    base.update({"printer_settings_id": p.machine, "print_settings_id": p.process, "inherits_group": ["", "", ""],
                 "different_settings_to_system": ["", "", ""], "wall_filament": "0", "sparse_infill_filament": "0",
                 "solid_infill_filament": "0"})
    by_type = {t: {**base, "filament_type": [t], "filament_settings_id": [p.filaments[t]],
                   "nozzle_temperature": ["230" if t == "TPU" else "220"],
                   "filament_max_volumetric_speed": ["3.2" if t == "TPU" else "15"]} for t in ("PLA", "TPU")}
    root = tmp_path / "templates"
    (root / KEY).mkdir(parents=True)
    with zipfile.ZipFile(u1.template_path(False)) as zin, zipfile.ZipFile(root / KEY / "PLA.3mf", "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps(by_type["PLA"]).encode()
            zout.writestr(item, data)

    def run(cmd, timeout, cwd=None):
        cmd = [str(c) for c in cmd]
        out = check.Path(cmd[cmd.index("--outputdir") + 1])
        with zipfile.ZipFile(out / cmd[cmd.index("--export-3mf") + 1], "w") as z:
            z.writestr("Metadata/project_settings.config", json.dumps(_settings(cmd[-1])))
        return check.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(templates, "load", lambda key, install=True: (
        {"filament_keys": filament_keys}, json.loads(json.dumps(by_type))))
    monkeypatch.setattr(orca, "TEMPLATES", root)
    monkeypatch.setattr(check, "TEMPLATES", root)
    monkeypatch.setattr(orca, "run_slicer", run)
    check._template.cache_clear()
    yield by_type
    check._template.cache_clear()


def _convert(tmp_path, src, key=KEY):
    made = orca.convert(src, "Band", [key], tmp_path / "out")
    return made, tmp_path / "out" / "Band" / f"Band - {printers.PRINTERS[key].label}.3mf"


def test_a_tpu_part_gets_the_tpu_preset_and_its_own_filament(tmp_path, slicer):
    src = _project(tmp_path, ["PLA", "TPU"])
    made, f = _convert(tmp_path, src)
    assert made == (1, 0)
    s = _settings(f)
    assert s["filament_type"] == ["PLA", "TPU"]
    assert s["filament_settings_id"] == [printers.PRINTERS[KEY].filaments["PLA"], "WonderMaker TPU 95A"]
    assert s["nozzle_temperature"] == ["220", "230"] and s["filament_max_volumetric_speed"] == ["15", "3.2"]
    with zipfile.ZipFile(f) as z:
        ms = z.read("Metadata/model_settings.config").decode()
    assert '<metadata key="extruder" value="2"/><metadata key="sparse_infill_density" value="100%"/>' in ms
    info, problems, _warnings = check.audit(f, src, KEY)
    assert problems == [] and any("by part, no paint (filaments [1, 2])" in line for line in info)


def test_tpu_for_ams_is_refused_where_there_is_no_preset_for_it(tmp_path, slicer, capsys):
    made, f = _convert(tmp_path, _project(tmp_path, ["PLA", "TPU-AMS"]))
    assert made == (0, 1) and not f.exists()
    assert "filament 2 is Bambu's TPU for AMS (TPU-AMS)" in capsys.readouterr().out


@pytest.mark.parametrize("marked, want", [(False, "0"), (True, "2")])
def test_the_walls_filament_is_carried_only_where_the_designer_set_it(tmp_path, slicer, marked, want):
    """Snapmaker Orca's default 1 for the walls' filament, carried, put a TPU band's walls in PLA."""
    diff = ["wall_filament" if marked else "", "", "", ""]
    src = _project(tmp_path, ["PLA", "TPU"], extra_settings={"wall_filament": "2" if marked else "1",
                                                             "different_settings_to_system": diff})
    made, f = _convert(tmp_path, src)
    assert made == (1, 0) and _settings(f)["wall_filament"] == want


def test_check_4_wants_paint_or_parts_in_two_filaments(tmp_path, slicer):
    src = bambu_project(tmp_path / "plain.3mf", plates=1, paint={}, parts=[(1, {}), (1, {})])
    made, _f = _convert(tmp_path, src)
    assert made == (0, 1)  # nothing to tell its colors apart: refused by the audit


def test_check_12_takes_a_filament_with_more_nozzle_variants(tmp_path, slicer):
    """The H2D's Generic TPU keeps four entries per nozzle variant, its PLA one: tied by filament_self_index."""
    src = _project(tmp_path, ["PLA", "TPU"])
    _made, f = _convert(tmp_path, src)
    s = _settings(f)
    key = "filament_max_volumetric_speed"
    for variants, entries, ok in ((["1", "2", "2", "2", "2"], ["15", "3.2", "3.2", "3.2", "3.2"], True),
                                  (None, ["15", "3.2", "3.2"], False)):
        odd = f.with_name(f"odd{ok}.3mf")
        changed = {**s, key: entries, **({"filament_self_index": variants} if variants else {})}
        with zipfile.ZipFile(f) as zin, zipfile.ZipFile(odd, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item)
                zout.writestr(item, json.dumps(changed).encode() if item.filename.endswith("project_settings.config")
                              else data)
        _info, problems, _w = check.audit(odd, src, KEY)
        assert ok == (not [p for p in problems if "per-filament settings" in p])


# ----------------------------------------------------------------- u1 engine ----

def _u1(tmp_path, src, name="Band"):
    assert u1.convert(src, name, ["u1"], tmp_path / "out") == (1, 0)
    return tmp_path / "out" / name / f"{name} - Snapmaker U1.3mf"


def test_every_u1_preset_is_bundled_with_its_values():
    data = u1.u1_filaments()
    template = u1.load_template(False)
    assert set(u1.U1_PRESETS.values()) <= set(data["presets"])
    for name, p in data["presets"].items():
        assert set(p["values"]) == set(data["keys"]) and p["values"]["filament_type"] == p["filament_type"]
        assert all(isinstance(template[k], list) and len(template[k]) == 4 for k in data["keys"] if k != "filament_ids")


@pytest.mark.parametrize("src_type, preset, kind", [
    ("TPU", "Generic TPU", "TPU"),
    ("TPU-AMS", "Snapmaker TPU 95A HF @U1 0.4 nozzle", "TPU"),
    ("PETG", "Generic PETG", "PETG"),  # it printed as PLA
    ("PETG-HF", "Snapmaker PETG HF", "PETG"),
    ("ABS", "Generic ABS", "ABS"),
])
def test_a_u1_extruder_gets_its_presets_values_not_the_templates_pla(tmp_path, src_type, preset, kind):
    src = _project(tmp_path, ["PLA", src_type])
    f = _u1(tmp_path, src)
    s, t = _settings(f), u1.load_template(False)
    want = u1.u1_filaments()["presets"][preset]["values"]
    assert s["filament_type"][:2] == ["PLA", kind] and s["filament_settings_id"][1] == preset
    for k in ("nozzle_temperature", "nozzle_temperature_initial_layer", "filament_max_volumetric_speed",
              "pressure_advance", "filament_ids"):
        assert s[k][1] == want[k], k
        assert s[k][0] == t[k][0], k  # the PLA's own column is the template's
    assert s["nozzle_temperature"][1] != t["nozzle_temperature"][1] or kind == "PLA"
    with zipfile.ZipFile(f) as z:
        si = z.read("Metadata/slice_info.config").decode()
    assert f'id="2" type="{kind}"' in si
    _info, problems = u1check.audit(f, src)
    assert problems == []


def test_tpu_for_ams_on_the_u1_says_what_it_prints_on(tmp_path, capsys):
    _u1(tmp_path, _project(tmp_path, ["PLA", "TPU-AMS"]))
    assert "extruder 2 is Bambu's TPU for AMS: the U1 has no preset for it, so it prints on Snapmaker TPU 95A HF" \
        in capsys.readouterr().out


def _u1_source(tmp_path, preset: str, temp: str):
    """A U1 project, its extruder 2 on `preset` at `temp`."""
    t = u1.load_template(False)
    cfg = {k: v for k, v in t.items() if k not in ("filament_colour",)}
    cfg.update({"filament_type": ["PLA", "TPU", "PLA", "PLA"],
                "filament_settings_id": [t["filament_settings_id"][0], preset, *t["filament_settings_id"][2:]],
                "nozzle_temperature": [t["nozzle_temperature"][0], temp, *t["nozzle_temperature"][2:]]})
    return bambu_project(tmp_path / "u1src.3mf", palette=("#FFFFFF", "#3FBF6F", "#FFFFFF", "#FFFFFF"),
                         types=cfg["filament_type"], plates=1, parts=BAND, extra_settings=cfg)


def test_a_u1_projects_own_preset_keeps_its_values(tmp_path):
    src = _u1_source(tmp_path, "Snapmaker TPU 90A @U1", "231")
    s = _settings(_u1(tmp_path, src))
    assert s["filament_settings_id"][1] == "Snapmaker TPU 90A @U1" and s["nozzle_temperature"][1] == "231"


def test_an_older_u1_file_whose_tpu_kept_the_pla_values_gets_the_tpus(tmp_path):
    src = _u1_source(tmp_path, "Generic TPU", "220")  # 0.4.3 wrote Generic TPU on the template's PLA
    s = _settings(_u1(tmp_path, src))
    assert s["nozzle_temperature"][1] == u1.u1_filaments()["presets"]["Generic TPU"]["values"]["nozzle_temperature"]


def test_check_3b_fails_a_u1_tpu_printed_as_pla(tmp_path):
    src = _project(tmp_path, ["PLA", "TPU"])
    f = _u1(tmp_path, src)
    s = _settings(f)
    bad = f.with_name("bad.3mf")
    with zipfile.ZipFile(f) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "Metadata/project_settings.config":
                data = json.dumps({**s, "filament_type": ["PLA", "PLA", "PLA", "PLA"]}).encode()
            zout.writestr(item, data)
    _info, problems = u1check.audit(bad, src)
    assert "extruder 2 is PLA, its source slot 2 TPU" in problems

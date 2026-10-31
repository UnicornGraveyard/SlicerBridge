"""The orca engine's write path without a slicer, so CI runs it: write(), settle() and read_back(),
and the tall-tower rebuild around check 14.

The printer's template is made from the U1's (a Bambu-format OrcaSlicer project with a rib
tower on a 270 mm bed), and a stand-in for OrcaSlicer's command line reads it back.
"""
import json
import zipfile

import pytest

from builders import bambu_project
from slicerbridge import printers, tall, templates
from slicerbridge.checks import orca as check
from slicerbridge.engines import orca, u1

KEY = "creator5"  # an OrcaSlicer printer with four heads, like the U1 the template comes from


def _settings(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


@pytest.fixture
def slicer(tmp_path, monkeypatch):
    """A KEY template, and an OrcaSlicer that reads settings back as they're written: but a value in
    `refuses` ({key: value}) fails the load, and one in `reads` ({key: (written, read)}) comes back as told."""
    p = printers.PRINTERS[KEY]
    source = u1.load_template(False)
    # One filament's column, as templates.py has a slicer write it, and the printer's presets.
    n = len(source["filament_colour"])
    filament_keys = sorted(k for k, v in source.items() if k.startswith("filament_") and isinstance(v, list)
                           and len(v) == n)
    tpl = {k: [v[0]] if k in filament_keys else v for k, v in source.items()}
    tpl.update({"printer_settings_id": p.machine, "print_settings_id": p.process, "filament_type": ["PLA"],
                "filament_settings_id": [p.filaments["PLA"]], "inherits_group": ["", "", ""],
                "different_settings_to_system": ["", "", ""]})
    root = tmp_path / "templates"

    def template(**changes):
        """Write the template, with `changes` to its settings."""
        settings = {**tpl, **changes}
        (root / KEY).mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(u1.template_path(False)) as zin, zipfile.ZipFile(root / KEY / "PLA.3mf", "w") as zout:
            for item in zin.infolist():
                data = zin.read(item)
                if item.filename == "Metadata/project_settings.config":
                    data = json.dumps(settings, indent=4).encode()
                zout.writestr(item, data)
        fake.settings = settings
        return settings

    def load(key, install=True):
        assert key == KEY
        return {"filament_keys": filament_keys}, {"PLA": json.loads(json.dumps(fake.settings))}

    def run(cmd, timeout, cwd=None):
        cmd = [str(c) for c in cmd]
        fake.runs.append(cmd)
        out = check.Path(cmd[cmd.index("--outputdir") + 1])
        cfg = _settings(cmd[-1])
        wont = [f"{k}: {v} not in range [0,5]" for k, v in fake.refuses.items() if cfg.get(k) == v]
        if wont:  # as OrcaSlicer says it
            return check.subprocess.CompletedProcess(cmd, 1, "", "\n".join(wont))
        cfg.update({k: read for k, (written, read) in fake.reads.items() if cfg.get(k) == written})
        with zipfile.ZipFile(out / cmd[cmd.index("--export-3mf") + 1], "w") as z:
            z.writestr("Metadata/project_settings.config", json.dumps(cfg))
        return check.subprocess.CompletedProcess(cmd, 0, "", "")

    fake = type("Fake", (), {"runs": [], "refuses": {}, "reads": {}, "settings": None, "template": template})
    template()
    monkeypatch.setattr(templates, "load", load)
    monkeypatch.setattr(orca, "TEMPLATES", root)
    monkeypatch.setattr(check, "TEMPLATES", root)
    monkeypatch.setattr(orca, "run_slicer", run)
    check._template.cache_clear()
    yield fake
    check._template.cache_clear()


def _convert(tmp_path, size, **extra):
    src = bambu_project(tmp_path / f"tall{size:g}.3mf", plates=1, size=size, extra_settings=extra or None)
    made = orca.convert(src, "Tall", [KEY], tmp_path / "out")
    return made, tmp_path / "out" / "Tall" / f"Tall - {printers.PRINTERS[KEY].label}.3mf"


@pytest.mark.parametrize("size, step", [(120.0, 0), (205.0, 1), (230.0, 2)])
def test_a_tall_plate_gets_the_heaviest_tower_it_has_room_for(tmp_path, slicer, capsys, size, step):
    assert _convert(tmp_path, size)[0] == (1, 0)
    name, rules = tall.STEPS["orca"][step]
    want = tall.settings(slicer.settings, rules)
    got = _settings(tmp_path / "out" / "Tall" / f"Tall - {printers.PRINTERS[KEY].label}.3mf")
    assert want and {k: got[k] for k in want} == want
    assert all(got[k] == slicer.settings[k] for k in tall.settings(slicer.settings, tall.TOWER["orca"]) if k not in want)
    assert set(want) <= set(got["different_settings_to_system"][0].split(";"))
    assert len(slicer.runs) == 1  # read back once: placement's step is check 14's
    assert f"over 100{', but' if step == 2 else ':'}" in (out := capsys.readouterr().out) and name in out
    assert "heavier tower base has no room" not in out  # check 14's warning: the line above says it


def test_a_tower_check_14_finds_room_for_is_rebuilt_at_that_step(tmp_path, slicer, capsys):
    """An auto brim squeezed to 3 mm leaves placement room for the slower tower only; at the spot it got,
    check 14 finds room for the brim and spacing on the printer's own ribs, and the file is made again so."""
    slicer.template(prime_tower_brim_width="-1")
    made, f = _convert(tmp_path, 216.0)
    assert made == (1, 0)
    assert len(slicer.runs) == 2  # the first file, then the one rebuilt at check 14's step
    got = _settings(f)
    want = tall.settings(slicer.settings, tall.STOCK_RIBS["orca"])
    assert {k: got[k] for k in want} == want and got["prime_tower_brim_width"] == "10"  # not the squeezed 3
    assert got["wipe_tower_extra_rib_length"] == slicer.settings["wipe_tower_extra_rib_length"]
    out = capsys.readouterr().out
    assert "over 100: heavier prime tower on the printer's own ribs" in out
    assert "prime tower brim 3 mm" not in out


def test_a_rebuilt_tower_keeps_its_own_brim_over_the_squeezed_one(tmp_path, slicer, monkeypatch, capsys):
    """Placement squeezed the auto brim to 3 mm (slower tower only); where check 14 then finds room at that
    spot for the printer's own ribs, the rebuilt file has that step's 10 mm brim, not the 3 (#16)."""
    slicer.template(prime_tower_brim_width="-1")
    owed = iter([1])  # check 14's verdict on the first file; the rebuilt one is kept
    monkeypatch.setattr(check, "owed_step_of", lambda f, key: next(owed, None))
    made, f = _convert(tmp_path, 222.0)
    assert made == (1, 0) and len(slicer.runs) == 2
    assert _settings(f)["prime_tower_brim_width"] == "10"
    assert "prime tower brim 3 mm" not in capsys.readouterr().out


def test_a_carried_value_the_slicer_wont_load_keeps_the_printers(tmp_path, slicer, capsys):
    slicer.refuses = {"brim_width": "6"}
    made, f = _convert(tmp_path, 40.0, brim_width="6", wall_loops="4")
    assert made == (1, 0) and len(slicer.runs) == 2  # refused, then read back without it
    got = _settings(f)
    assert got["brim_width"] == slicer.settings["brim_width"] and got["wall_loops"] == "4"
    assert "brim_width" not in got["different_settings_to_system"][0].split(";")
    assert "kept the printer's own brim_width (OrcaSlicer won't take '6'" in capsys.readouterr().out


def test_a_carried_value_read_back_as_another_keeps_the_printers(tmp_path, slicer, capsys):
    slicer.reads = {"wall_loops": ("4", "3")}
    made, f = _convert(tmp_path, 40.0, wall_loops="4")
    assert made == (1, 0) and _settings(f)["wall_loops"] == slicer.settings["wall_loops"]
    assert "wall_loops (OrcaSlicer read '4' back as '3')" in capsys.readouterr().out


def test_what_the_slicer_pads_or_respaces_is_written_as_it_reads_it(tmp_path, slicer):
    slicer.template(thumbnails="48x48/PNG,300x300/PNG")
    own = slicer.settings["extruder_colour"]
    slicer.reads = {"extruder_colour": (own, [*own, "#FFFFFF"]),
                    "thumbnails": ("48x48/PNG,300x300/PNG", "48x48/PNG, 300x300/PNG")}
    made, f = _convert(tmp_path, 40.0)
    assert made == (1, 0) and len(slicer.runs) == 1
    got = _settings(f)
    assert got["extruder_colour"] == [*own, "#FFFFFF"] and got["thumbnails"] == "48x48/PNG, 300x300/PNG"


def test_a_setting_of_ours_read_back_as_another_is_refused(tmp_path, slicer, capsys):
    slicer.reads = {"curr_bed_type": (templates.default_bed_type(slicer.settings), "Cool Plate")}  # compose()'s
    made, f = _convert(tmp_path, 40.0)
    assert made == (0, 1) and not f.exists()
    assert "reads curr_bed_type back as 'Cool Plate'" in capsys.readouterr().out


def test_a_refusal_naming_no_carried_setting_is_a_refusal(tmp_path, slicer, capsys):
    slicer.refuses = {"printable_height": slicer.settings["printable_height"]}
    made, f = _convert(tmp_path, 40.0)
    assert made == (0, 1) and not f.exists()
    assert "won't load the Flashforge Creator 5 settings" in capsys.readouterr().out


def test_an_orca_printer_that_fails_mid_write_leaves_no_partial_file(tmp_path, slicer, monkeypatch):
    def broken(f, src, key):  # a bug in the checks, after the file is written beside its final name
        raise RuntimeError("boom")

    monkeypatch.setattr(check, "audit", broken)
    src = bambu_project(tmp_path / "cube.3mf", plates=1)
    assert orca.convert(src, "Cube", [KEY], tmp_path / "out") == (0, 1)
    assert not list((tmp_path / "out" / "Cube").glob("*.3mf"))

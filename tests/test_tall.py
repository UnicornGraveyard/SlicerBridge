"""A tall print gets the heavier prime tower on every printer that prints one, in its slicer's names."""
import json
import zipfile

from builders import bambu_project
from slicerbridge import tall
from slicerbridge.checks import orca as check
from slicerbridge.engines import orca, prusa, u1


def test_the_tower_is_brought_up_to_the_minimums_and_no_further():
    cfg = {"prime_tower_brim_width": "-1", "wipe_tower_extra_rib_length": "30", "wipe_tower_rib_width": ["8", "8"],
           "wipe_tower_extra_spacing": "120%", "wipe_tower_max_purge_speed": "90"}
    assert tall.settings(cfg, tall.TOWER["orca"]) == {
        "prime_tower_brim_width": "10",          # auto counts as short
        "wipe_tower_rib_width": ["12", "12"],    # a list stays a list
        "wipe_tower_extra_spacing": "150%",
        "wipe_tower_max_purge_speed": "60",      # a ceiling
    }  # the 30 mm ribs are longer already
    assert tall.settings(cfg, tall.SLOWER["orca"]) == {"wipe_tower_max_purge_speed": "60"}
    assert tall.settings({"wipe_tower_brim_width": "2"}, tall.TOWER["prusa"]) == {"wipe_tower_brim_width": "10"}


def test_the_u1_engine_gives_the_same_towers():
    t = u1.load_template(False)
    assert u1.TALL_TOWER == tall.settings(t, tall.TOWER["orca"])
    assert u1.TALL_TOWER_STOCK_RIBS == tall.settings(t, tall.STOCK_RIBS["orca"])
    assert u1.TALL_TOWER_TIGHT == tall.settings(t, tall.SLOWER["orca"])


def test_each_slicer_steps_down_from_the_whole_tower_to_the_slower_one():
    assert [name for name, _r in tall.STEPS["bambu"]] == [
        "heavier prime tower", "heavier prime tower on the printer's own ribs", "slower tower only"]
    assert [name for name, _r in tall.STEPS["prusa"]] == ["heavier prime tower", "slower tower only"]
    assert {k for k, _v, _c in tall.STOCK_RIBS["bambu"]} == {
        "prime_tower_brim_width", "prime_tower_infill_gap", "prime_tower_max_speed"}


def test_longer_wider_ribs_widen_the_towers_footprint():
    base = {"prime_tower_width": "35", "prime_tower_brim_width": "3", "prime_tower_rib_wall": "1",
            "prime_tower_extra_rib_length": "0", "prime_tower_rib_width": "8", "prime_volume": "45",
            "layer_height": "0.2", "filament_colour": ["#000000", "#FFFFFF"]}
    stock = check.tower_size(base)
    heavy = check.tower_size({**base, **tall.settings(base, tall.TOWER["bambu"])})
    grow = 2 * ((20 + 12) - 8) / (2 * 2 ** 0.5)
    assert abs(heavy[0] - stock[0] - grow) < 1e-9 and abs(heavy[1] - stock[1] - grow) < 1e-9
    assert heavy[2] == 10


def _block(side, height=150.0):
    return orca.Plate(1, lo=[0.0, 0.0, 0.0], hi=[side, side, height])


def test_the_orca_engine_gives_each_tall_plate_the_heaviest_tower_it_has_room_for():
    cfg = u1.load_template(False)  # a 270 mm Bambu-format OrcaSlicer bed with a rib tower
    (full, _r), (stock, _s), (slower, _t) = tall.STEPS["orca"]
    assert orca.tall_tower("Roomy", [_block(120.0)], cfg, "orca") == (tall.settings(cfg, tall.TOWER["orca"]), full)
    assert orca.tall_tower("Snug", [_block(205.0)], cfg, "orca") == (
        tall.settings(cfg, tall.STOCK_RIBS["orca"]), stock)
    assert orca.tall_tower("Wide", [_block(230.0)], cfg, "orca") == ({"wipe_tower_max_purge_speed": "60"}, slower)
    assert orca.tall_tower("Short", [_block(120.0, 100.0)], cfg, "orca") == ({}, "")


def _tall_file(tmp_path, cfg):
    src = bambu_project(tmp_path / "tall.3mf", plates=1, size=120.0)
    f = tmp_path / "out.3mf"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(f, "w") as zout:
        for item in zin.infolist():
            data = json.dumps(cfg).encode() if item.filename == "Metadata/project_settings.config" else zin.read(item)
            zout.writestr(item, data)
    return f


def test_check_14_fails_a_tall_file_on_the_light_tower_and_passes_the_heavier(tmp_path):
    cfg = {**u1.load_template(False), "wipe_tower_x": ["200"], "wipe_tower_y": ["200"]}
    boxes = [(1, (0.0, 0.0, 0.0), (120.0, 120.0, 150.0))]  # the tower spot is well clear of it
    with zipfile.ZipFile(_tall_file(tmp_path, cfg)) as z:
        _info, problems, _w = check.tall_tower(z, cfg, boxes, "orca")
        assert problems and "on the light prime tower" in problems[0]
        heavy = {**cfg, **tall.settings(cfg, tall.TOWER["orca"])}
        info, problems, _w = check.tall_tower(z, heavy, boxes, "orca")
        assert problems == [] and "heavier prime tower set" in info[0]
        # A spot at the bed's edge can't hold the wider base: only the slower tower is owed.
        edge = {**cfg, "wipe_tower_x": ["235"], **tall.settings(cfg, tall.SLOWER["orca"])}
        _info, problems, warnings = check.tall_tower(z, edge, boxes, "orca")
        assert problems == [] and "slower tower only" in warnings[0]
        assert check.tall_tower(z, cfg, [(1, (0.0, 0.0, 0.0), (120.0, 120.0, 100.0))], "orca") == ([], [], [])
        # Room for the wider brim but not the longer ribs: the rest is owed, on the file's own ribs.
        snug = {**cfg, "wipe_tower_x": ["220"], **tall.settings(cfg, tall.STOCK_RIBS["orca"])}
        slower = {**cfg, "wipe_tower_x": ["220"], **tall.settings(cfg, tall.SLOWER["orca"])}
        problems = check.tall_tower(z, slower, boxes, "orca")[1]  # the slower tower alone isn't enough there
        assert problems and "prime_tower_brim_width" in problems[0] and "rib" not in problems[0]
        info, problems, _w = check.tall_tower(z, snug, boxes, "orca")
        assert problems == [] and "on its own ribs set" in info[0]
        # What the engine asks before it keeps a file: the step the tower's spot has room for.
        assert [check.owed_step(z, {**cfg, "wipe_tower_x": [x]}, boxes, "orca") for x in ("200", "220", "235")] == [0, 1, 2]


def test_the_prusa_layout_says_whether_the_wider_brim_has_room():
    bed = (0.0, 0.0, 250.0, 210.0, 220.0)
    roomy = prusa.Placement(False, (130.0, 90.0), (120.0, 120.0, 150.0))
    assert prusa.layout(roomy, bed, 60.0, brim=10.0)[2]
    tight = prusa.Placement(False, (10.0, 30.0), (240.0, 180.0, 150.0))
    assert not prusa.layout(tight, bed, 60.0, brim=10.0)[2]

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


def test_the_u1_engine_gives_the_same_tower():
    assert u1.TALL_TOWER == tall.settings(u1.load_template(False), tall.TOWER["orca"])
    assert u1.TALL_TOWER_TIGHT == tall.settings(u1.load_template(False), tall.SLOWER["orca"])


def test_longer_wider_ribs_widen_the_towers_footprint():
    base = {"prime_tower_width": "35", "prime_tower_brim_width": "3", "prime_tower_rib_wall": "1",
            "prime_tower_extra_rib_length": "0", "prime_tower_rib_width": "8", "prime_volume": "45",
            "layer_height": "0.2", "filament_colour": ["#000000", "#FFFFFF"]}
    stock = check.tower_size(base)
    heavy = check.tower_size({**base, **tall.settings(base, tall.TOWER["bambu"])})
    grow = 2 * ((20 + 12) - 8) / (2 * 2 ** 0.5)
    assert abs(heavy[0] - stock[0] - grow) < 1e-9 and abs(heavy[1] - stock[1] - grow) < 1e-9
    assert heavy[2] == 10


def _cube(size):
    return orca.Plate(1, lo=[0.0, 0.0, 0.0], hi=[size, size, size])


def test_the_orca_engine_gives_a_tall_plate_with_room_the_heavier_tower():
    cfg = u1.load_template(False)  # a 270 mm Bambu-format OrcaSlicer bed with a rib tower
    extra, roomy = orca.tall_tower("Tall", [_cube(120.0)], cfg, "orca")
    assert roomy and extra == tall.settings(cfg, tall.TOWER["orca"])
    extra, roomy = orca.tall_tower("Wide", [_cube(230.0)], cfg, "orca")
    assert not roomy and extra == {"wipe_tower_max_purge_speed": "60"}
    assert orca.tall_tower("Short", [_cube(100.0)], cfg, "orca") == ({}, False)


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


def test_the_prusa_layout_says_whether_the_wider_brim_has_room():
    bed = (0.0, 0.0, 250.0, 210.0, 220.0)
    roomy = prusa.Placement(False, (130.0, 90.0), (120.0, 120.0, 150.0))
    assert prusa.layout(roomy, bed, 60.0, brim=10.0)[2]
    tight = prusa.Placement(False, (10.0, 30.0), (240.0, 180.0, 150.0))
    assert not prusa.layout(tight, bed, 60.0, brim=10.0)[2]

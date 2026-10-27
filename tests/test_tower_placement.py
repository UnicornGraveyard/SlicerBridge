"""Where the prime tower goes: every nozzle's reach, the tower's printed size, and room by the model's outline."""
from slicerbridge.checks import orca as check
from slicerbridge.engines import orca as engine

H2D = {"printable_area": ["0x0", "350x0", "350x320", "0x320"],
       "extruder_printable_area": ["0x0,325x0,325x320,0x320", "25x0,350x0,350x320,25x320"]}


def test_a_two_nozzle_bed_is_where_both_nozzles_reach():
    assert check.reachable_area(H2D) == (25, 0, 325, 320)
    assert check.reachable_area({"printable_area": H2D["printable_area"]}) == (0, 0, 350, 320)


def test_plates_are_spaced_by_the_whole_bed_not_the_reach():
    bed = engine.Bed.of({**H2D, "printable_height": "325", "enable_prime_tower": "0"})
    assert (bed.x0, bed.x1) == (25, 325)
    assert engine.plate_origin(1, 2, bed) == (350 * engine.PLATE_STRIDE, 0)


def test_an_auto_brim_is_a_brim_and_rib_walls_build_square():
    cfg = {"prime_tower_width": "60", "prime_tower_brim_width": "-1", "prime_tower_rib_wall": "1",
           "filament_colour": ["#000000"] * 4, "layer_height": "0.16"}
    w, d, b = check.tower_size(cfg)
    assert b == check.AUTO_TOWER_BRIM
    assert abs((w - check.RIB_EXTRA[0]) - (d - check.RIB_EXTRA[1])) < 1e-9  # square but for the ribs
    assert check.tower_size({**cfg, "prime_tower_rib_wall": "0"})[0] == 60 + check.WALL_EXTRA


def test_a_big_triangle_marks_its_whole_outline():
    cells = set()
    check.add_outline(cells, [0, 40, 0], [0, 0, 40], [(0, 1, 2)])
    assert (2, 2) in cells and (18, 0) in cells and (0, 18) in cells  # inside, and along both legs
    assert (15, 15) not in cells  # past the hypotenuse


def test_the_tower_goes_where_the_outline_leaves_room():
    """A 230 x 200 mm L on a 256 mm bed: no strip beside its box, a free corner inside it."""
    bed = engine.Bed(0, 0, 256, 256, 250, 35, tower_brim=3)
    outline = {(i, j) for i in range(5, 120) for j in range(5, 105)}  # 10..240 x 10..210
    outline -= {(i, j) for i in range(85, 120) for j in range(5, 45)}  # its front-right corner empty
    spot = engine.tower_spot(outline, bed, 40, 40, (125, 110))
    assert spot is not None
    tx, ty = spot
    assert not check.outline_hits(outline, (tx - 3, ty - 3, tx + 43, ty + 43))
    assert tx >= 170 and ty < 90  # in that corner


def test_no_room_by_the_outline_is_no_spot():
    bed = engine.Bed(0, 0, 256, 256, 250, 35, tower_brim=3)
    outline = {(i, j) for i in range(0, 128) for j in range(0, 128)}
    assert engine.tower_spot(outline, bed, 40, 40, (128, 128)) is None

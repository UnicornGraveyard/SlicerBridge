"""Despeckling: paint islands under the threshold merged into their neighbor's color, nothing else touched.

The models are flat grids of 0.5 mm cells, two triangles each (0.25 mm² a cell, so 1 mm² is 4 cells),
drawn as art: a digit is the filament a cell is painted, "." unpainted.
"""
import re
import zipfile
from collections import Counter

import pytest

from builders import _rezip, bambu_project, leaf as builder_leaf, painted_grid, prusa_grid
from slicerbridge import cli
from slicerbridge.checks.u1 import paint_leaves
from slicerbridge.despeckle import despeckle, leaf, leaf_shares, report
from slicerbridge.errors import Refusal

OBJECT = "3D/Objects/object_1.model"
TRIANGLE = re.compile(rb"<triangle [^>]*>")
PAINT = re.compile(rb'(?:paint_color|slic3rpe:mmu_segmentation)="([0-9A-F]*)"')
STRIP = re.compile(rb'\s(?:paint_color|slic3rpe:mmu_segmentation)="[0-9A-F]*"')


def encode(node) -> str:
    """A TriangleSelector paint string: a node is a state, or (split, side, [children in serialized order])."""
    bits = []

    def put(v, k):
        bits.extend((v >> i) & 1 for i in range(k))

    def walk(n):
        if isinstance(n, int):
            put(0, 2)
            if n >= 3:
                put(3, 2)
                put(n - 3, 4)
            else:
                put(n, 2)
        else:
            split, side, kids = n
            put(split, 2)
            put(side, 2)
            for k in kids:
                walk(k)

    walk(node)
    bits += [0] * (-len(bits) % 4)
    nibbles = [sum(bits[i + j] << j for j in range(4)) for i in range(0, len(bits), 4)]
    return "".join("0123456789ABCDEF"[n] for n in reversed(nibbles))


def triangles(path, entry=OBJECT) -> list[bytes]:
    with zipfile.ZipFile(path) as z:
        return TRIANGLE.findall(z.read(entry))


def state(el: bytes) -> str:
    m = PAINT.search(el)
    if not m:
        return "."
    shares = leaf_shares(m.group(1))
    return str(next(iter(shares))) if len(shares) == 1 else "s"


def drawn(path, entry=OBJECT) -> str:
    """The grid's paint as art again; a cell whose two triangles differ is "?"."""
    tris = triangles(path, entry)
    cells = [a if a == b else "?" for a, b in ((state(tris[k]), state(tris[k + 1])) for k in range(0, len(tris), 2))]
    return cells


def art(text: str) -> list[str]:
    return [ch for line in text.strip().splitlines() for ch in line.strip()]


def run(tmp_path, before: str, min_area=1.0, **kw):
    src = painted_grid(tmp_path / "grid.3mf", before, **kw)
    r = despeckle(src, tmp_path / "out.3mf", min_area)
    return r, drawn(r.written or src)


# ---------------------------------------------------------------- the paint strings ----

@pytest.mark.parametrize("state_", range(1, 19))
def test_the_leaf_encoder_matches_the_strings_slicers_write(state_):
    assert leaf(state_) == builder_leaf(state_).encode()
    assert leaf_shares(leaf(state_)) == {state_: 1.0}
    assert paint_leaves(leaf(state_)) == Counter({state_: 1})


def test_state_3_is_two_nibbles():
    assert (leaf(1), leaf(2), leaf(3), leaf(4), leaf(18)) == (b"4", b"8", b"0C", b"1C", b"FC")
    with pytest.raises(ValueError):
        leaf(0)


def test_leaf_shares_split_triangles():
    assert leaf_shares("041") == {1: 0.5, 0: 0.5}  # bl2u1's test string: halves, filament 1 and the part's
    assert encode((1, 0, [1, 0])) == "041"
    # two sides split: the last child (serialized first) is the half, the corners quarters
    assert leaf_shares(encode((2, 1, [3, 1, 2]))) == {3: 0.5, 1: 0.25, 2: 0.25}
    assert leaf_shares(encode((3, 0, [5, 5, 1, 2]))) == {5: 0.5, 1: 0.25, 2: 0.25}
    nested = encode((1, 2, [(1, 0, [4, 2]), 7]))
    assert leaf_shares(nested) == {4: 0.25, 2: 0.25, 7: 0.5}
    assert paint_leaves(nested.encode()) == Counter({4: 1, 2: 1, 7: 1})  # the checks' own decoder agrees


# ---------------------------------------------------------------- what merges ----

def test_islands_take_the_color_around_them(tmp_path):
    r, after = run(tmp_path, """
        111111111
        121111111
        111113311
        111113311
        155511111
        444411111
    """)
    assert after == art("""
        111111111
        111111111
        111113311
        111113311
        111111111
        444411111
    """)  # the 3s and 4s are 1 mm² each: not under 1, so kept
    assert (r.total("islands_before"), r.total("islands_after"), r.changed) == (2, 0, 8)
    assert r.total("changed_area") == pytest.approx(1.0)
    assert r.from_to() == Counter({(5, 1): 6, (2, 1): 2})


def test_the_longest_border_wins(tmp_path):
    _, after = run(tmp_path, """
        1111111
        1666661
        1266661
        1266661
        1666661
        1111111
    """)  # the 2s touch 1 along 1 mm and 6 along 2 mm
    assert after == art("""
        1111111
        1666661
        1666661
        1666661
        1666661
        1111111
    """)


def test_an_island_goes_to_a_big_region_before_a_small_one(tmp_path):
    # The 2 touches the small ring of 3s along 1.5 mm and the big 4s along 0.5 mm: an edge to an
    # island counts a hundredth, so it takes 4. The ring goes to the 1s around it.
    _, after = run(tmp_path, """
        1111111
        1133311
        1132311
        4444444
        4444444
    """, min_area=1.5)
    assert after == art("""
        1111111
        1111111
        1114111
        4444444
        4444444
    """)


def test_two_islands_that_would_trade_colors_merge_instead(tmp_path):
    # A part that is all islands: each one's only neighbor is the other. The smaller takes the larger's color.
    r, after = run(tmp_path, "223")
    assert after == art("222")
    assert (r.total("islands_before"), r.total("islands_after")) == (2, 0)


def test_the_threshold_is_a_parameter(tmp_path):
    grid = """
        11111
        12221
        11111
    """
    _, after = run(tmp_path, grid, min_area=0.5)
    assert after == art(grid)  # 0.75 mm² is kept at 0.5
    _, after = run(tmp_path, grid, min_area=1.0)
    assert after == art("11111\n11111\n11111")


def test_areas_are_measured_as_placed(tmp_path):
    grid = """
        11111
        12211
        11111
    """
    _, after = run(tmp_path, grid, scale=2.0)
    assert after == art(grid)  # 0.5 mm² in the mesh, 2 mm² at twice the size
    _, after = run(tmp_path, grid, scale=1.0)
    assert "2" not in after


def test_unpainted_triangles_count_as_their_parts_filament(tmp_path):
    # The 1s are the part's own filament, like the unpainted cells: one region, not an island,
    # though the 3s border it along more.
    r, after = run(tmp_path, """
        .......
        3311133
        3333333
    """)
    assert r.changed == 0 and r.written is None  # nothing to change: nothing written
    # A speck of the part's filament inside 3 takes 3; a speck of 3 in the unpainted area takes the part's.
    r, after = run(tmp_path, """
        .......
        ..3....
        .......
        3333333
        33.3333
        3333333
    """)
    assert after == art("""
        .......
        ..1....
        .......
        3333333
        3333333
        3333333
    """)


def test_split_triangles_keep_their_paint_unless_recolored(tmp_path):
    tie = encode((1, 0, [1, 2]))           # half 1, half 2: a tie counts as the lower, 1
    mostly_2 = encode((2, 0, [2, 2, 1]))   # three quarters 2
    r, _ = run(tmp_path, """
        111111
        111111
        111211
        111111
    """, codes={(0, 0, 0): tie, (2, 3, 1): mostly_2})
    tris = triangles(r.written)
    assert PAINT.search(tris[0]).group(1).decode() == tie   # in the big region of 1: kept as it was
    island = (2 * 6 + 3) * 2
    assert [PAINT.search(t).group(1) for t in tris[island:island + 2]] == [b"4", b"4"]   # one leaf of 1 each
    assert r.changed == 2 and r.total("split_changed") == 1


def test_only_the_recolored_triangles_change_by_a_byte(tmp_path):
    src = painted_grid(tmp_path / "grid.3mf", """
        1111111
        1211131
        1111111
        1.11111
    """, extruder=2)  # the unpainted cell prints in 2: an island in 1
    _rezip(src, {"Metadata/plate_1.png": b"\x89PNG not really", "Metadata/notes.txt": b"left as it is"})
    r = despeckle(src, tmp_path / "out.3mf")
    assert r.changed == 6
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(r.written) as b:
        assert a.namelist() == b.namelist()
        for name in a.namelist():
            if name != OBJECT:
                assert a.read(name) == b.read(name), name
        old, new = a.read(OBJECT), b.read(OBJECT)
    assert TRIANGLE.split(old) == TRIANGLE.split(new)  # every byte between the triangles
    changed = [(x, y) for x, y in zip(TRIANGLE.findall(old), TRIANGLE.findall(new)) if x != y]
    assert len(changed) == 6
    for x, y in changed:  # the paint's value, or paint added to an unpainted triangle; the rest as it was
        assert STRIP.sub(b"", x) == STRIP.sub(b"", y)
        assert PAINT.search(y).group(1) == b"4"
    assert sum(b"paint_color" not in x for x, _ in changed) == 2


def test_triangles_written_another_way_are_read_and_kept(tmp_path):
    src = painted_grid(tmp_path / "grid.3mf", "11111\n12111\n11111")
    with zipfile.ZipFile(src) as z:
        model = z.read(OBJECT)
    odd = re.sub(rb'<triangle (v1="\d+" v2="\d+" v3="\d+") (paint_color="[0-9A-F]*")/>', rb"<triangle \2 \1 />", model)
    assert odd != model
    _rezip(src, {OBJECT: odd})
    r = despeckle(src, tmp_path / "out.3mf")
    assert r.changed == 2 and drawn(r.written) == art("11111\n11111\n11111")
    with zipfile.ZipFile(r.written) as z:
        new = z.read(OBJECT)
    assert TRIANGLE.split(odd) == TRIANGLE.split(new)
    assert sum(x != y for x, y in zip(TRIANGLE.findall(odd), TRIANGLE.findall(new))) == 2


def test_every_painted_mesh_in_a_model_file(tmp_path):
    # Two plates of 0.5 mm cubes, their meshes in one 3D/Objects file: a side of 3 and a side of 5 on each,
    # 0.25 mm² apiece, the four unpainted sides (1 mm²) the part's filament 1.
    src = bambu_project(tmp_path / "cubes.3mf", size=0.5)
    r = despeckle(src, tmp_path / "out.3mf")
    assert len(r.meshes) == 2 and r.changed == 8 and r.from_to() == Counter({(3, 1): 4, (5, 1): 4})
    tris = triangles(r.written)
    assert len(tris) == 24 and [state(t) for t in tris] == ["1"] * 4 + ["."] * 8 + ["1"] * 4 + ["."] * 8
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(r.written) as b:
        assert TRIANGLE.split(a.read(OBJECT)) == TRIANGLE.split(b.read(OBJECT))


def test_prusaslicer_paint(tmp_path):
    src = prusa_grid(tmp_path / "grid.3mf", """
        111111
        121111
        111111
        333333
        33.333
        333333
    """)
    r = despeckle(src, tmp_path / "out.3mf")
    assert drawn(r.written, "3D/3dmodel.model") == art("""
        111111
        111111
        111111
        333333
        333333
        333333
    """)
    tris = triangles(r.written, "3D/3dmodel.model")
    assert all(b"paint_color" not in t for t in tris)
    assert tris[(4 * 6 + 2) * 2].endswith(b' slic3rpe:mmu_segmentation="0C"/>')
    assert r.name(3) == "3 #DE4343"


def test_a_slice_kept_in_the_file_is_left_out(tmp_path):
    src = painted_grid(tmp_path / "grid.3mf", "11111\n12111\n11111")
    _rezip(src, {"Metadata/plate_1.gcode": b"G28\n", "Metadata/plate_1.gcode.md5": b"abc"})
    r = despeckle(src, tmp_path / "out.3mf")
    with zipfile.ZipFile(r.written) as z:
        assert "Metadata/plate_1.gcode" not in z.namelist() and "Metadata/project_settings.config" in z.namelist()
    assert r.dropped == ["Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5"]
    assert any(line.startswith("left out  : Metadata/plate_1.gcode,") for line in report(r))


# ---------------------------------------------------------------- refusals and the report ----

def test_the_source_is_never_written(tmp_path):
    folder = tmp_path / "models"
    src = painted_grid(folder / "grid.3mf", "11111\n12111\n11111")
    before = src.read_bytes()
    with pytest.raises(Refusal, match="written over"):
        despeckle(src, src)
    r = despeckle(src, None)  # a dry run: what would change, nothing written
    assert r.changed == 2 and r.written is None
    assert src.read_bytes() == before and sorted(p.name for p in folder.iterdir()) == ["grid.3mf"]


def test_refusals(tmp_path):
    with pytest.raises(Refusal, match="no paint"):
        despeckle(painted_grid(tmp_path / "plain.3mf", "...\n..."), tmp_path / "out.3mf")
    with pytest.raises(Refusal, match="positive area"):
        despeckle(painted_grid(tmp_path / "grid.3mf", "121"), tmp_path / "out.3mf", min_area=0)
    (tmp_path / "junk.3mf").write_bytes(b"not a zip")
    with pytest.raises(Refusal, match="not a zip"):
        despeckle(tmp_path / "junk.3mf", tmp_path / "out.3mf")


def test_a_filament_painted_only_in_islands_is_named(tmp_path):
    r, _ = run(tmp_path, "11111\n12111\n11111")
    assert r.gone() == [2]
    assert "⚠ filament 2 #CBC6B8 is painted nowhere now: all of it was islands under 1 mm²" in report(r)


# ---------------------------------------------------------------- the command line ----

def test_cli(tmp_path, capsys):
    src = painted_grid(tmp_path / "Dragon.3mf", "11111\n12111\n11111")
    assert cli.main(["despeckle", str(src), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "islands   : 1 under 1 mm²" in out and "dry run   : nothing written" in out
    assert not (tmp_path / "Dragon - despeckled.3mf").exists()

    assert cli.main(["despeckle", str(src)]) == 0
    out = capsys.readouterr().out
    assert "recolored : 2 triangles" in out and "from -> to: 2 #CBC6B8 -> 1 #161616: 2" in out
    assert (tmp_path / "Dragon - despeckled.3mf").exists()

    (tmp_path / "clean").mkdir()
    assert cli.main(["despeckle", str(src), "--min-area", "0.1", "--out", str(tmp_path / "clean")]) == 0
    out = capsys.readouterr().out
    assert "nothing to change: no file written" in out and not list((tmp_path / "clean").iterdir())

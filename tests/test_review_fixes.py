"""Three bugs an architecture review found: a rebuilt tower's brim, stale caches, a U1 failure's leftovers."""
import os

from builders import bambu_project
from slicerbridge.checks import orca as check
from slicerbridge.engines import orca, u1


def test_a_tower_rebuilt_heavier_keeps_its_own_brim():
    # Placement squeezed an auto-brim tower to 3 mm; the heavier step sets 10, and must keep it.
    assert orca.squeezed_brim(3.0, {"prime_tower_brim_width": "10", "prime_tower_max_speed": "60"}) is None
    assert orca.squeezed_brim(3.0, {"wipe_tower_brim_width": "10"}) is None
    assert orca.squeezed_brim(3.0, {"wipe_tower_max_purge_speed": "60"}) == 3.0  # the slower tower sets no brim
    assert orca.squeezed_brim(None, {"prime_tower_brim_width": "10"}) is None


def test_a_source_saved_again_is_read_again(tmp_path):
    """The MCP server is one process for hours: a re-saved source mustn't be checked against its old self."""
    src = bambu_project(tmp_path / "model.3mf", plates=1, size=40.0)
    assert check.source_sizes(src) == {1: (40.0, 40.0, 40.0)}
    before = os.stat(src)
    bambu_project(src, plates=1, size=60.0)
    os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))  # a clock that didn't tick
    assert check.source_sizes(src) == {1: (60.0, 60.0, 60.0)}
    assert check.source_facts(src) == check.u1.source_facts(src)


def test_a_u1_failure_leaves_no_partial_file_and_takes_the_earlier_one_back(project, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    assert u1.convert(project, "Cube", ["u1"], out) == (1, 0)
    final = out / "Cube" / "Cube - Snapmaker U1.3mf"
    assert final.exists()

    def broken(xml, transforms):  # a bug halfway through writing the file
        raise RuntimeError("boom")

    monkeypatch.setattr(u1, "retransform_items", broken)
    assert u1.convert(project, "Cube", ["u1"], out) == (0, 1)
    assert not final.exists()  # the earlier file doesn't stand in for this run
    assert not list((out / "Cube").glob("partial - *"))
    assert "removed the earlier Cube - Snapmaker U1.3mf" in capsys.readouterr().out

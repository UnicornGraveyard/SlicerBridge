"""What running on Windows needed: absolute paths, UTF-8, and one printer's failure staying its own."""
import json
from pathlib import Path

from slicerbridge import templates
from slicerbridge.checks import orca as check
from slicerbridge.cli import _abs
from slicerbridge.engines import orca as engine
from slicerbridge.printers import Slicer


def test_the_cli_hands_the_slicers_absolute_paths():
    """The slicers run in the temp folder: a relative path isn't found there (Bambu Studio exit -3)."""
    assert _abs("out/model.3mf").is_absolute()


def test_a_profile_with_non_ascii_bytes_is_indexed(tmp_path):
    """OrcaSlicer's Kobra X preset isn't ASCII; read in Windows' code page it was skipped as missing."""
    machine = tmp_path / "resources" / "profiles" / "Vendor" / "machine"
    machine.mkdir(parents=True)
    (machine / "p.json").write_bytes(json.dumps({"name": "Kobra X 0.4", "note": "Kōbra"}, ensure_ascii=False)
                                     .encode("utf-8"))
    slicer = Slicer("OrcaSlicer", tmp_path / "orca-slicer.exe")
    assert ("machine", "Kobra X 0.4") in templates.preset_index("Vendor", slicer)


def test_one_printers_failure_leaves_the_rest_their_files(monkeypatch, tmp_path):
    def write(m, key, slice_it, out, prefix=""):
        if key == "k2plus":
            raise RuntimeError("the slicer hung")
        return True

    monkeypatch.setattr(engine, "load_model", lambda src, name: object())
    monkeypatch.setattr(engine, "write", write)
    assert engine.convert(Path("m.3mf"), "M", ["k2plus", "k2pro", "k2se"], tmp_path) == (2, 1)


def test_a_flush_tower_counts_a_change_per_three_filaments():
    """IronJaw's K2 Pro flush tower printed about 45 x 60 mm; every filament every layer said 40 x 211."""
    cfg = {"purge_in_prime_tower": "1", "flush_volumes_matrix": ["0", "270"], "filament_colour": ["#000000"] * 4,
           "layer_height": "0.16"}
    assert abs(check.tower_area(cfg) - 1.25 * (4 / 3) * 270 / 0.16) < 1e-6

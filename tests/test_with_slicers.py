"""End to end with real slicers: run with SLICERBRIDGE_SLICER_TESTS=1 (minutes; builds templates).

Skipped in CI. Before a release, run them on a Mac with OrcaSlicer 2.4.2,
Bambu Studio and PrusaSlicer installed (docs/install.md).
"""
import os

import pytest

from slicerbridge import printers

pytestmark = [pytest.mark.slicer, pytest.mark.skipif(os.environ.get("SLICERBRIDGE_SLICER_TESTS") != "1",
                                                     reason="set SLICERBRIDGE_SLICER_TESTS=1 to run")]


def need(slicer: str):
    if not printers.SLICERS[slicer].installed:
        pytest.skip(f"{printers.SLICERS[slicer].name} isn't installed")


@pytest.fixture(autouse=True)
def real_cache(monkeypatch):
    """Templates take minutes to build: keep them between runs in the user's cache (or SLICERBRIDGE_CACHE)."""
    monkeypatch.delenv("SLICERBRIDGE_CACHE", raising=False)
    if os.environ.get("SLICERBRIDGE_TEST_CACHE"):
        monkeypatch.setenv("SLICERBRIDGE_CACHE", os.environ["SLICERBRIDGE_TEST_CACHE"])


# One printer per fix in docs/brand-slicers.md, and one plain one.
@pytest.mark.parametrize("key", ["k2plus", "q2", "kobra3", "kobrax"])
def test_orca_family_converts_and_slices(project, tmp_path, key):
    need("orca")
    from slicerbridge.engines import orca
    assert orca.convert(project, "Cube", [key], tmp_path / "out", slice_it=True) == (1, 0)


# One AMS printer, and one with two nozzles, one of which reaches only part of the bed.
@pytest.mark.parametrize("key", ["a1", "x2d"])
def test_bambu_converts_and_slices(project, tmp_path, key):
    need("bambu")
    from slicerbridge.engines import orca
    assert orca.convert(project, "Cube", [key], tmp_path / "out", slice_it=True) == (1, 0)


def test_prusa_printers_in_one_run(project, tmp_path):
    """Placing a plate moves the source's build items: every printer must start from the source."""
    need("prusa")
    from slicerbridge.engines import prusa
    made, failed = prusa.convert(project, "Cube", ["mmu3", "xl"], tmp_path / "out")
    assert (made, failed) == (4, 0)  # 2 plates x 2 printers

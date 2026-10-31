import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from builders import bambu_project  # noqa: E402
from slicerbridge import printers  # noqa: E402


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    """Never touch the user's real cache: templates and profiles go in a temp folder."""
    monkeypatch.setenv("SLICERBRIDGE_CACHE", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def no_snapmaker_orca(request, tmp_path, monkeypatch):
    """The U1 engine reads every file back in Snapmaker Orca when it's installed: not the user's, in
    the fast tests (tests/test_u1_slice.py has a stand-in). Its variable set, so Homebrew isn't tried."""
    if request.node.get_closest_marker("slicer"):
        return
    missing = tmp_path / "no Snapmaker Orca" / "snapmaker-orca"
    monkeypatch.setenv("SLICERBRIDGE_SNAPMAKER_ORCA", str(missing))
    monkeypatch.setitem(printers.SLICERS, "snapmaker", replace(printers.SLICERS["snapmaker"], app=missing))


@pytest.fixture
def project(tmp_path) -> Path:
    """Two plates, a 5-color palette, using slots 1, 3 and 5."""
    return bambu_project(tmp_path / "cube.3mf")


def installed(env: str, default: str) -> bool:
    return Path(os.environ.get(env, default)).exists()

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from builders import bambu_project  # noqa: E402


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    """Never touch the user's real cache: templates and profiles go in a temp folder."""
    monkeypatch.setenv("SLICERBRIDGE_CACHE", str(tmp_path / "cache"))


@pytest.fixture
def project(tmp_path) -> Path:
    """Two plates, a 5-color palette, using slots 1, 3 and 5."""
    return bambu_project(tmp_path / "cube.3mf")


def installed(env: str, default: str) -> bool:
    return Path(os.environ.get(env, default)).exists()

"""The release pieces agree: one version everywhere, a plugin pinned and locked, and every slicer setting reaches the code."""
import importlib.util
import json
import re
from pathlib import Path

from slicerbridge import __version__

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_every_place_names_the_package_version():
    assert set(release.versions().values()) == {__version__}


def test_the_changelog_has_this_version():
    assert release.notes(__version__)


def test_the_plugin_names_one_version():
    """Its manifest, pins and lock agree. It may trail the package: it moves once a release is on PyPI."""
    assert release.plugin_problems() == []


def test_the_plugin_launches_a_pinned_uvx_package():
    """The directory reads a lock only for `uvx <package>==<version>`; it can't follow `uv run`."""
    server = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["slicerbridge"]
    version = release.plugin_versions()["plugin/uv.lock"]
    assert server["command"] == "uvx"
    assert server["args"] == [f"slicerbridge=={version}", "mcp"]


def test_the_lock_is_pypi_only_hashed_and_small_enough():
    """Exact versions, PyPI sources and hashes earn the directory's Verified badge; past 256 KiB it isn't read."""
    lock = (ROOT / "plugin" / "uv.lock").read_text(encoding="utf-8")
    packages = [b for b in lock.split("[[package]]")[1:] if 'name = "slicerbridge-claude-plugin"' not in b]
    assert packages
    for block in packages:
        name = re.search(r'name = "([^"]+)"', block).group(1)
        assert 'source = { registry = "https://pypi.org/simple" }' in block, name
        assert 'hash = "sha256:' in block, name
    assert (ROOT / "plugin" / "uv.lock").stat().st_size < 200 * 1024  # headroom under the directory's 256 KiB


def test_every_slicer_setting_reaches_the_code():
    """The plugin's settings, the registry's variables and printers.py name the same slicer locations."""
    in_code = set(re.findall(r'"(SLICERBRIDGE_[A-Z_]+)"', (ROOT / "slicerbridge" / "printers.py").read_text(encoding="utf-8")))
    in_code.discard("SLICERBRIDGE_CACHE")
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    env = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["slicerbridge"]["env"]
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert set(env) == in_code
    assert {v["name"] for v in server["packages"][0]["environmentVariables"]} == in_code
    referenced = {re.fullmatch(r"\$\{user_config\.(\w+)\}", v).group(1) for v in env.values()}
    assert referenced == set(plugin["userConfig"])
    assert all(opt.get("default") == "" for opt in plugin["userConfig"].values())  # Cowork needs a default


def test_the_readme_claims_the_registry_name():
    name = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["name"]
    assert f"mcp-name: {name}" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_an_empty_setting_means_the_usual_place(monkeypatch):
    """The plugin passes "" for an unset location: that must fall back to /Applications, not to ''."""
    from slicerbridge import printers
    monkeypatch.setenv("SLICERBRIDGE_ORCASLICER", "")
    assert printers._app("SLICERBRIDGE_ORCASLICER", "/Applications/OrcaSlicer.app") == Path("/Applications/OrcaSlicer.app")


def test_a_location_can_name_variables_and_the_newest_version(tmp_path, monkeypatch):
    """Windows installs go under %ProgramFiles%, and Creality Print puts its version in the folder name."""
    from slicerbridge import printers
    for version in ("6.2", "6.10", "7.0 Beta", "7.0 Beta 2", "7.0 Beta3", "7.0"):
        (tmp_path / f"Creality Print {version}").mkdir()
        (tmp_path / f"Creality Print {version}" / "CrealityPrint.exe").touch()
    monkeypatch.setenv("SB_TEST_PROGRAMS", str(tmp_path))
    monkeypatch.delenv("SLICERBRIDGE_CREALITY_PRINT", raising=False)
    found = printers._app("SLICERBRIDGE_CREALITY_PRINT", str(Path("$SB_TEST_PROGRAMS") / "Creality Print *" / "CrealityPrint.exe"))
    assert found.parent.name == "Creality Print 7.0"  # not 6.10, and not a 7.0 beta, numbered or not
    order = ["Creality Print 6.10", "Creality Print 7.0 Beta", "Creality Print 7.0 Beta 2", "Creality Print 7.0 Beta3",
             "Creality Print 7.0", "Creality Print 7.1 Beta"]  # a newer version's beta is still newer
    assert sorted(order, key=lambda n: printers._newest(tmp_path / n / "CrealityPrint.exe")) == order
    nowhere = str(tmp_path / "Nothing *" / "x.exe")
    assert printers._app("SLICERBRIDGE_CREALITY_PRINT", nowhere) == Path(nowhere)  # shown as where it was looked for

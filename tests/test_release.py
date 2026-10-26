"""The release pieces agree: one version everywhere, and every slicer setting reaches the code."""
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


def test_the_plugin_pins_its_launch_command():
    args = json.loads((ROOT / "plugin" / ".mcp.json").read_text())["mcpServers"]["slicerbridge"]["args"]
    assert args == [f"slicerbridge=={__version__}", "mcp"]


def test_every_slicer_setting_reaches_the_code():
    """The plugin's settings, the registry's variables and printers.py name the same slicer locations."""
    in_code = set(re.findall(r'"(SLICERBRIDGE_[A-Z_]+)"', (ROOT / "slicerbridge" / "printers.py").read_text()))
    in_code.discard("SLICERBRIDGE_CACHE")
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    env = json.loads((ROOT / "plugin" / ".mcp.json").read_text())["mcpServers"]["slicerbridge"]["env"]
    server = json.loads((ROOT / "server.json").read_text())
    assert set(env) == in_code
    assert {v["name"] for v in server["packages"][0]["environmentVariables"]} == in_code
    referenced = {re.fullmatch(r"\$\{user_config\.(\w+)\}", v).group(1) for v in env.values()}
    assert referenced == set(plugin["userConfig"])
    assert all(opt.get("default") == "" for opt in plugin["userConfig"].values())  # Cowork needs a default


def test_the_readme_claims_the_registry_name():
    name = json.loads((ROOT / "server.json").read_text())["name"]
    assert f"mcp-name: {name}" in (ROOT / "README.md").read_text()


def test_an_empty_setting_means_the_usual_place(monkeypatch):
    """The plugin passes "" for an unset location: that must fall back to /Applications, not to ''."""
    from slicerbridge import printers
    monkeypatch.setenv("SLICERBRIDGE_ORCASLICER", "")
    assert str(printers._app("SLICERBRIDGE_ORCASLICER", "/Applications/OrcaSlicer.app")) == "/Applications/OrcaSlicer.app"

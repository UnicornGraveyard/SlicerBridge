#!/usr/bin/env python3
"""
release.py -- the checks and notes for a release (.github/workflows/release.yml).

    python scripts/release.py check 0.2.0    # every place that names the version agrees
    python scripts/release.py notes 0.2.0    # that version's CHANGELOG section
    python scripts/release.py plugin 0.2.0   # once 0.2.0 is on PyPI: move the plugin to it

A release is a tag v<version>. The version also lives in the package and the
MCP Registry metadata, and `check` refuses a release where they disagree.

The Claude plugin's launcher pins a version and ships a uv.lock for it, and a
lock can only name a version that's already on PyPI, so the plugin moves after
the release: `plugin` sets its manifest and pins and relocks with uv. Until then
it keeps running the previous version, so `check` only asks that the plugin's
manifest, pins and lock agree with each other. Standard library only, plus uv
for `plugin`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
LAUNCHER = PLUGIN / ".mcp.json"
PYPROJECT = PLUGIN / "pyproject.toml"
LOCK = PLUGIN / "uv.lock"


def versions() -> dict[str, str]:
    """Where the package's version is written down, and what each place says."""
    init = (ROOT / "slicerbridge" / "__init__.py").read_text()
    server = json.loads((ROOT / "server.json").read_text())
    return {
        "slicerbridge/__init__.py": re.search(r'__version__ = "([^"]+)"', init).group(1),
        "server.json version": server["version"],
        "server.json package version": server["packages"][0]["version"],
    }


def plugin_versions() -> dict[str, str]:
    """The SlicerBridge version the plugin runs, in each place the plugin names it."""
    pyproject = PYPROJECT.read_text()
    locked = re.search(r'^name = "slicerbridge"\nversion = "([^"]+)"', LOCK.read_text(), re.M)
    pin = re.search(r'"slicerbridge==([^"]+)"', pyproject)
    launch = re.search(r'"slicerbridge==([^"]+)"', LAUNCHER.read_text())
    return {
        "plugin/.claude-plugin/plugin.json": json.loads(MANIFEST.read_text())["version"],
        "plugin/.mcp.json launch pin": launch.group(1) if launch else "not pinned",
        "plugin/pyproject.toml version": re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
        "plugin/pyproject.toml pin": pin.group(1) if pin else "not pinned",
        "plugin/uv.lock": locked.group(1) if locked else "not locked",
    }


def notes(version: str) -> str:
    """The CHANGELOG section for `version`, without its heading."""
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## {re.escape(version)}\b.*?\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def plugin_problems() -> list[str]:
    """Places where the plugin disagrees with itself about the version it runs."""
    found = plugin_versions()
    if len(set(found.values())) == 1:
        return []
    return ["the plugin names more than one version: " + ", ".join(f"{k} says {v}" for k, v in found.items())]


def check(version: str) -> list[str]:
    """Problems that should stop a release of `version`; empty when it's ready."""
    problems = [f"{where} says {got}, not {version}" for where, got in versions().items() if got != version]
    if not notes(version):
        problems.append(f"CHANGELOG.md has no '## {version}' section")
    return problems + plugin_problems()


def move_plugin(version: str) -> list[str]:
    """Point the plugin at `version` (already on PyPI) and relock; returns the problems left."""
    manifest, launcher, pyproject = MANIFEST.read_text(), LAUNCHER.read_text(), PYPROJECT.read_text()
    MANIFEST.write_text(re.sub(r'("version": ")[^"]+(")', rf"\g<1>{version}\2", manifest, count=1))
    LAUNCHER.write_text(re.sub(r'"slicerbridge==[^"]+"', f'"slicerbridge=={version}"', launcher))
    PYPROJECT.write_text(re.sub(r'"slicerbridge==[^"]+"', f'"slicerbridge=={version}"',
                                re.sub(r'^version = "[^"]+"', f'version = "{version}"', pyproject, count=1, flags=re.M)))
    # --upgrade: every dependency moves to its newest release too, so the plugin's are current at each release
    lock = subprocess.run(["uv", "lock", "--upgrade", "--project", str(PLUGIN)], capture_output=True, text=True)
    if lock.returncode != 0:
        MANIFEST.write_text(manifest)
        LAUNCHER.write_text(launcher)
        PYPROJECT.write_text(pyproject)
        return [f"uv lock failed, plugin left as it was (is {version} on PyPI yet?):\n{lock.stderr.strip()}"]
    return plugin_problems()


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "notes", "plugin"):
        print(__doc__.strip().split("\n\n")[1])
        return 2
    command, version = sys.argv[1], sys.argv[2].removeprefix("v")
    if command == "notes":
        print(notes(version))
        return 0
    if command == "plugin":
        problems = move_plugin(version)
        for p in problems:
            print(f"!! {p}")
        if not problems:
            print(f"the plugin now runs {version}: commit plugin/ and push to main")
        return 1 if problems else 0
    problems = check(version)
    for p in problems:
        print(f"!! {p}")
    if not problems:
        print(f"ready to release {version}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

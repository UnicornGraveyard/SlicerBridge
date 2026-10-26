#!/usr/bin/env python3
"""
release.py -- the checks and notes for a release (.github/workflows/release.yml).

    python scripts/release.py check 0.2.0   # every place that names the version agrees
    python scripts/release.py notes 0.2.0   # that version's CHANGELOG section

A release is a tag v<version>. The version also lives in the package, the
plugin's manifest, the plugin's pinned launch command and the MCP Registry
metadata, and a release where they disagree would ship a plugin that starts
another version, so `check` refuses it. Standard library only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def versions() -> dict[str, str]:
    """Where the release's version is written down, and what each place says."""
    init = (ROOT / "slicerbridge" / "__init__.py").read_text()
    server = json.loads((ROOT / "server.json").read_text())
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    launch = json.loads((ROOT / "plugin" / ".mcp.json").read_text())["mcpServers"]["slicerbridge"]["args"]
    pinned = next((a.split("==", 1)[1] for a in launch if a.startswith("slicerbridge==")), "not pinned")
    return {
        "slicerbridge/__init__.py": re.search(r'__version__ = "([^"]+)"', init).group(1),
        "server.json version": server["version"],
        "server.json package version": server["packages"][0]["version"],
        "plugin/.claude-plugin/plugin.json": plugin["version"],
        "plugin/.mcp.json launch pin": pinned,
    }


def notes(version: str) -> str:
    """The CHANGELOG section for `version`, without its heading."""
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## {re.escape(version)}\b.*?\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def check(version: str) -> list[str]:
    """Problems that should stop a release of `version`; empty when it's ready."""
    problems = [f"{where} says {got}, not {version}" for where, got in versions().items() if got != version]
    if not notes(version):
        problems.append(f"CHANGELOG.md has no '## {version}' section")
    return problems


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "notes"):
        print(__doc__.strip().split("\n\n")[1])
        return 2
    command, version = sys.argv[1], sys.argv[2].removeprefix("v")
    if command == "notes":
        print(notes(version))
        return 0
    problems = check(version)
    for p in problems:
        print(f"!! {p}")
    if not problems:
        print(f"ready to release {version}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

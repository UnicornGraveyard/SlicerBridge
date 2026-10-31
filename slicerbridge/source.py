"""
source.py -- what every engine reads from a Bambu project the same way.

Standard library only.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


def palette(path: Path) -> list[dict]:
    """A Bambu project's filaments, [{"id": "1", "type": "PLA", "color": "#RRGGBB"}, ...], from
    project_settings.config, which always carries the whole palette.

    Projects are often saved unsliced, so slice_info.config may be an empty stub, and
    a sliced one lists what that slice used, as it was then. The checks take the
    palette from project_settings.config too (checks/u1.py source_facts).
    """
    with zipfile.ZipFile(path) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config").decode())
    colors, types = cfg.get("filament_colour", []), cfg.get("filament_type", [])
    return [{"id": str(i + 1), "type": types[i] if i < len(types) else "PLA", "color": c}
            for i, c in enumerate(colors)]

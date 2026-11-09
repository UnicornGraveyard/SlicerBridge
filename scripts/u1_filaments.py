#!/usr/bin/env python3
"""
u1_filaments.py -- write slicerbridge/data/u1/filaments.json from the Snapmaker Orca installed here.

    python scripts/u1_filaments.py

The u1 engine gives an extruder printing any type but PLA (PETG, ASA, PC, nylon,
a TPU...) the values of the U1 preset engines.u1.U1_PRESETS names for that type, from
this file, so a U1 file is made the same with or without Snapmaker Orca. Each preset goes through its inherits
chain as Snapmaker Orca loads it. Run it again after updating Snapmaker Orca, or
after changing U1_PRESETS: tests/test_u1_slice.py says when the file no longer
matches the installed profiles.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slicerbridge.engines import u1  # noqa: E402
from slicerbridge.printers import SLICERS, not_found  # noqa: E402


def main() -> int:
    if not SLICERS["snapmaker"].installed:
        print(f"!! {not_found(SLICERS['snapmaker'])}", file=sys.stderr)
        return 1
    data = u1.filament_data()
    u1.FILAMENTS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{u1.FILAMENTS.relative_to(ROOT)}: {len(data['presets'])} presets from Snapmaker Orca "
          f"{data['snapmaker_orca']}, {len(data['keys'])} settings each")
    return 0


if __name__ == "__main__":
    sys.exit(main())

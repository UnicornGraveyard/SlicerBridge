"""The heavier prime tower every engine gives a print taller than TALL_PRINT_MM.

The tower climbs as long as tools swap, and a face-up wall mount swaps tools nearly
to its top: Gatefang is 200 mm tall with three swaps a layer to about 176 mm. U1 users
lost its tower around layer 665 (2026-10-28), and every printer's own tower is as
light or lighter. So a tall file gets the levers Snapmaker's U1 guide names for a
collapsing tower, in each slicer's own words: a wider brim, longer and wider ribs,
wider purge line spacing, and a slower tower, so a nozzle that catches it hits
softer. A starting point, not yet proven on a print.

The settings are minimums (and the speed a ceiling): a printer whose own tower is
already heavier keeps its own. A file gets the heaviest of STEPS its plates have room
for: the whole tower (TOWER); else the same brim, spacing and speed on the printer's
own ribs (STOCK_RIBS), since longer ribs are what reach furthest; else only what
costs no room, the slower tower (SLOWER). The checks write these numbers out for
themselves rather than importing them.
"""
from __future__ import annotations

TALL_PRINT_MM = 100.0

# (setting, value, True if the value is a ceiling), per slicer's names. OrcaSlicer's are
# Snapmaker Orca's too (the U1). PrusaSlicer's tower has no ribs and no speed of its own.
TOWER = {
    "orca": (("prime_tower_brim_width", 10.0, False),
             ("wipe_tower_extra_rib_length", 20.0, False),
             ("wipe_tower_rib_width", 12.0, False),
             ("wipe_tower_extra_spacing", 150.0, False),
             ("wipe_tower_max_purge_speed", 60.0, True)),
    "bambu": (("prime_tower_brim_width", 10.0, False),
              ("prime_tower_extra_rib_length", 20.0, False),
              ("prime_tower_rib_width", 12.0, False),
              ("prime_tower_infill_gap", 150.0, False),
              ("prime_tower_max_speed", 60.0, True)),
    "prusa": (("wipe_tower_brim_width", 10.0, False),
              ("wipe_tower_extra_spacing", 150.0, False)),
}
RIBS = {"wipe_tower_extra_rib_length", "wipe_tower_rib_width", "prime_tower_extra_rib_length", "prime_tower_rib_width"}
# No room for the longer ribs: the brim, spacing and speed on the printer's own ribs.
STOCK_RIBS = {slicer: tuple(t for t in rules if t[0] not in RIBS) for slicer, rules in TOWER.items()}
# No room for the wider brim either: what costs none.
SLOWER = {slicer: tuple(t for t in rules if t[2]) for slicer, rules in TOWER.items()}


def _steps(slicer: str) -> tuple:
    """(name for the output, rules) to try in turn, heaviest first, each once. The last needs no room."""
    out = [("heavier prime tower", TOWER[slicer])]
    if STOCK_RIBS[slicer] != TOWER[slicer]:  # PrusaSlicer has no ribs to give up
        out.append(("heavier prime tower on the printer's own ribs", STOCK_RIBS[slicer]))
    return (*out, ("slower tower only", SLOWER[slicer]))


STEPS = {slicer: _steps(slicer) for slicer in TOWER}

PERCENT = {"wipe_tower_extra_spacing", "prime_tower_infill_gap"}


def _num(v) -> float | None:
    """A config number, "150%" included, the first of a list; None if it isn't one."""
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def settings(cfg: dict, rules: tuple) -> dict:
    """{setting: value} that brings `cfg`'s tower up to `rules`, in cfg's own format.

    Only settings cfg has; one already at or past its minimum (or under its ceiling)
    is left as it is. A brim of -1 (auto) counts as short of any minimum.
    """
    out = {}
    for key, want, ceiling in rules:
        if key not in cfg:
            continue
        got = _num(cfg[key])
        if got is not None and (got <= want if ceiling else got >= want):
            continue
        text = f"{want:g}%" if key in PERCENT else f"{want:g}"
        out[key] = [text] * len(cfg[key]) if isinstance(cfg[key], list) else text
    return out


def describe(changed: dict) -> str:
    """The settings a file got, for the output: "prime_tower_brim_width 10, ...". """
    return ", ".join(f"{k} {v[0] if isinstance(v, list) else v}" for k, v in changed.items())


def say(step: str, tallest: float, changed: dict) -> str:
    """The line the output gives a tall print's tower: the step it got, and its settings."""
    got = f" ({describe(changed)})" if changed else " (the printer's own already is)"
    if step == STEPS["orca"][0][0]:
        return f"{tallest:.0f} mm tall, over {TALL_PRINT_MM:g}: {step}{got}"
    if step != STEPS["orca"][-1][0]:
        return f"{tallest:.0f} mm tall, over {TALL_PRINT_MM:g}: {step}, no room for longer ones{got}"
    return (f"⚠ {tallest:.0f} mm tall, over {TALL_PRINT_MM:g}, but there's no room for a wider tower base: "
            f"{step}{got if changed else ''}. Slice it, and if the model leaves room, give the tower a bigger "
            f"brim and longer ribs")

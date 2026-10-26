"""High-level conversion orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ns
from .paint import used_states, PaintError
from .prusa import PrusaProject, is_prusa_3mf, read_prusa_3mf, remap_prusa_3mf
from .reader import read_bambu_3mf
from .writer import write_prusa_3mf, WriteStats


@dataclass
class ConversionResult:
    input_path: str
    output_path: str
    stats: WriteStats
    producer: str
    plates_available: list
    source: str = "bambu"  # "prusa" when the input was remapped rather than converted


def used_filaments(project) -> list[int]:
    """Sorted 1-based filament slots a project actually uses.

    Union of object/part extruder assignments and every state referenced by
    paint data. Bambu leaves unassigned objects on filament 1.
    """
    if isinstance(project, PrusaProject):
        return list(project.used)
    used: set[int] = set()
    for so in project.settings_objects.values():
        for ext in [so.extruder, *(p.extruder for p in so.parts)]:
            if ext:
                try:
                    used.add(int(ext))
                except ValueError:
                    pass
    codes: set[str] = set()
    for tree in [project.root_tree, *project.object_trees.values()]:
        for tri in tree.getroot().iter(ns.q(ns.CORE, "triangle")):
            code = tri.get("paint_color")
            if code:
                codes.add(code)
    for code in codes:
        try:
            used |= used_states(code)
        except PaintError:
            pass
    used.discard(0)
    if not used and (project.build_items or codes):
        used.add(1)  # something is printed and it defaults to filament 1
    return sorted(used)


def suggest_extruder_map(used: list[int]) -> dict[int, int]:
    """Compact sparse filament slots to sequential extruders 1..K."""
    return {old: i for i, old in enumerate(sorted(used), start=1)}


def default_extruder_map(project) -> dict[int, int]:
    """The mapping to offer first: Bambu slots compacted, Prusa tools left where they are."""
    used = used_filaments(project)
    if project.source == "prusa":
        return {f: f for f in used}
    return suggest_extruder_map(used)


def needs_mapping(project) -> bool:
    """Whether there is a filament mapping worth asking about.

    A Bambu project with one filament on slot 1 needs no decision; in a Prusa project
    even a single filament may be wanted on a different tool head.
    """
    used = used_filaments(project)
    return bool(used) and (project.source == "prusa" or used != [1])


def filament_noun(project) -> str:
    """What a filament slot of this project is called, e.g. 'Bambu filament'."""
    return "Tool" if project.source == "prusa" else "Bambu filament"


def output_suffix(source: str) -> str:
    return "_remapped" if source == "prusa" else "_prusa"


def painted_triangles(project) -> int:
    if isinstance(project, PrusaProject):
        return project.painted
    return sum(
        1
        for tree in [project.root_tree, *project.object_trees.values()]
        for tri in tree.getroot().iter(ns.q(ns.CORE, "triangle"))
        if "paint_color" in tri.attrib
    )


def structure(project) -> tuple[int, int]:
    """(model parts, build items) of a project."""
    if isinstance(project, PrusaProject):
        return project.objects, project.build_items
    return len(project.object_trees) + 1, len(project.build_items)


def filament_label(project, slot: int) -> str:
    """Human-readable description of a filament slot, e.g. '3 (PLA #8E2929)'."""
    bits = []
    if 0 < slot <= len(project.filament_types):
        bits.append(project.filament_types[slot - 1])
    if 0 < slot <= len(project.filament_colors):
        bits.append(project.filament_colors[slot - 1])
    return f"{slot} ({' '.join(bits)})" if bits else str(slot)


def convert_file(
    input_path: str,
    output_path: str,
    plates: Optional[list] = None,
    extruder_map: Optional[dict] = None,
    compact_extruders: bool = True,
) -> ConversionResult:
    """Convert a Bambu/Orca .3mf at ``input_path`` to a Prusa .3mf at ``output_path``.

    ``plates`` optionally restricts output to the given 1-based plate numbers.
    ``extruder_map`` explicitly maps Bambu filament slots to Prusa extruders
    (``{}`` keeps the original numbering); when None and ``compact_extruders`` is
    true, sparse slots are compacted to sequential extruders automatically.

    A PrusaSlicer .3mf is accepted too: it is copied with its tools remapped by
    ``extruder_map`` (filament settings follow), and is never compacted or split by
    plate.
    """
    if is_prusa_3mf(input_path):
        prusa = read_prusa_3mf(input_path)
        stats = remap_prusa_3mf(prusa, output_path, extruder_map)
        return ConversionResult(
            input_path=input_path,
            output_path=output_path,
            stats=stats,
            producer=prusa.producer,
            plates_available=[],
            source="prusa",
        )
    project = read_bambu_3mf(input_path)
    stats = write_prusa_3mf(
        project,
        output_path,
        plates=plates,
        extruder_map=extruder_map,
        compact_extruders=compact_extruders,
    )
    return ConversionResult(
        input_path=input_path,
        output_path=output_path,
        stats=stats,
        producer=project.producer,
        plates_available=project.plate_ids,
    )


def describe(input_path: str):
    """Read a project for inspection (used by --info and interactive prompts)."""
    if is_prusa_3mf(input_path):
        return read_prusa_3mf(input_path)
    return read_bambu_3mf(input_path)

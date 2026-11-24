"""A Bambu Studio project's automatic brim, on a printer whose slicer isn't Bambu Studio.

Bambu Studio's "Auto" brim and OrcaSlicer's work a width out per object the same way,
from how likely it is to tip or warp. But Bambu Studio prints none of it on an object
with supports, where OrcaSlicer and Snapmaker Orca print all of it. SoftMaw (141 mm
tall, tree supports, saved for the H3C) printed no brim in Bambu Studio 2.8.2, and
about 11 mm of one in Snapmaker Orca 2.4.1 on the U1 and in OrcaSlicer 2.4.2 on the K2
Plus: the model sits 5 mm from the bed's edge, so the brim ran into it (2026-11-24).
Carried as "auto_brim", the word arrives and the brim doesn't.

So each object's is worked out as Bambu Studio would (Panda2Prusa's reading of it, the
one the prusa engine prints), and an object it prints no brim on gets no_brim: the
whole project when that's every object on the project's brim, else the object alone.
An object it does brim keeps "auto": the printer's slicer has the same formula, exact
where this one is approximate. Only a Bambu printer's project is read this way: another
slicer's automatic brim is that slicer's own.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

from ._vendor.panda2prusa import writer
from ._vendor.panda2prusa.convert import describe
from ._vendor.panda2prusa.reader import ROOT_MODEL
from ._vendor.panda2prusa.settings import Warnings
from ._vendor.panda2prusa.transform import Transform

AUTO, NONE = "auto_brim", "no_brim"
SAID = "no brim: Bambu Studio's automatic brim leaves it out here, where this printer's slicer would print it"


def _one(v) -> str:
    return str((v[0] if v else "") if isinstance(v, list) else v or "")


def from_bambu_studio(src_cfg: dict) -> bool:
    """Whether the project's automatic brim is Bambu Studio's: it's set up for a Bambu printer."""
    return any(str(src_cfg.get(k) or "").startswith("Bambu Lab") for k in ("printer_model", "printer_settings_id"))


def worked_out(src: Path) -> dict:
    """{object id: (the brim type is its own, Bambu Studio prints no brim on it)} for each object
    on an automatic brim, its own or the project's. One it can't be worked out for counts as brimmed."""
    try:
        st = src.stat()
        return _worked_out(str(src), (st.st_mtime_ns, st.st_size))
    except OSError:
        return {}


@functools.lru_cache(maxsize=8)
def _worked_out(src: str, _stamp: tuple) -> dict:
    project = describe(src)
    whole = _one(project.project_settings.get("brim_type")) == AUTO
    objs, src_path = writer._collect_objects(project)
    out = {}
    for bi in project.build_items:
        key = writer._key(ROOT_MODEL, bi.objectid)
        so = project.settings_objects.get(bi.objectid)
        if bi.objectid in out or key not in objs:
            continue
        own = _one(so.settings.get("brim_type")) if so else ""
        if (own or (AUTO if whole else "")) != AUTO:
            continue
        got = {}
        mo = writer._plan_object(key, objs, src_path, so.parts) if so else None
        if mo is not None:
            try:
                got = writer._auto_brim(project, so, mo, objs, Transform.parse(bi.transform), "", Warnings())
            except ValueError:  # an unreadable transform: left as it is
                got = {}
        out[bi.objectid] = (bool(own), got.get("brim_type") == NONE)
    return out


def left_out(src: Path, src_cfg: dict) -> tuple[bool, set]:
    """(the whole project gets no_brim, ids of the objects that get one of their own).

    Nothing for a project that isn't Bambu Studio's, or has no automatic brim.
    """
    if not from_bambu_studio(src_cfg):
        return False, set()
    brims = worked_out(src)
    shared = [none for own, none in brims.values() if not own]
    whole = bool(shared) and all(shared)
    return whole, {oid for oid, (own, none) in brims.items() if none and (own or not whole)}


def carried(settings: dict, base: dict, whole: bool) -> dict:
    """`settings` (carry_print_settings()'s, onto `base`) with the project's brim as left_out() has it."""
    if not whole:
        return settings
    settings = {k: v for k, v in settings.items() if k != "brim_type"}
    return settings if base.get("brim_type") == NONE else {**settings, "brim_type": NONE}


def on_objects(model_settings: str, ids: set) -> str:
    """model_settings.config with no_brim as the own brim type of each object in `ids`."""
    for oid in sorted(ids):
        tag = re.search(rf'<object\b[^>]*\bid="{re.escape(oid)}"[^>]*>', model_settings)
        if not tag:
            continue
        end = min((i for i in (model_settings.find("<part", tag.end()), model_settings.find("</object>", tag.end()))
                   if i >= 0), default=len(model_settings))
        head = model_settings[tag.end():end]
        if 'key="brim_type"' in head:
            head = re.sub(r'(key="brim_type" value=")[^"]*(")', rf"\g<1>{NONE}\g<2>", head)
        else:
            head = f'<metadata key="brim_type" value="{NONE}"/>' + head
        model_settings = model_settings[:tag.end()] + head + model_settings[end:]
    return model_settings


def note(whole: bool, ids: set) -> str:
    """The line for the output, or "" when nothing changed."""
    if whole:
        return SAID
    return f"{SAID} (object{'s' * (len(ids) > 1)} {', '.join(sorted(ids))})" if ids else ""

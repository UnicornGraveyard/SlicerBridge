"""
mcp_server.py -- SlicerBridge as a local MCP server, for AI assistants.

    uvx slicerbridge mcp               # speaks MCP over stdio; add it to your assistant's MCP config
    slicerbridge-mcp                   # the same, once installed (pipx install slicerbridge)

It runs on your own machine, with your own slicers, over stdio: nothing is
uploaded or hosted, and it only reads and writes the paths you give it. Each
tool runs the same command as the `slicerbridge` CLI and returns what it
printed, so an assistant sees exactly the checks and warnings you would.

A conversion with slicing takes a minute or two per printer: ask for a few
printers at a time, or leave slicing for verify_files.
"""
from __future__ import annotations

import contextlib
import io
import os
import threading
from pathlib import Path
from typing import Annotated

try:
    from pydantic import Field

    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
    from mcp.types import ToolAnnotations
except ImportError as e:  # the server is an optional extra
    raise SystemExit("the MCP server needs the mcp package: pip install slicerbridge again") from e

from .cli import main as cli_main
from .printers import SETS

# All four hints on every tool, true or false: a client may read a missing one as its
# default, and some directories refuse a tool that leaves one out.
READS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = MCPServer("slicerbridge", instructions=(
    "SlicerBridge converts a multicolor .3mf project, saved by Bambu Studio, OrcaSlicer or a maker's fork of either "
    "(Creality Print, ElegooSlicer, Anycubic Slicer Next, Flash Studio, QIDI Studio), into a ready, checked file for each "
    "multicolor printer (Bambu Lab, Creality, Elegoo, Anycubic, Flashforge, QIDI, WonderMaker, Prusa, Snapmaker U1). "
    "Start with inspect_model to see the colors a model uses and how it fits each bed, then convert_model. "
    "A file SlicerBridge wrote can go back in for another printer. PrusaSlicer projects aren't read yet. "
    "A file that fails a check is never written; the result says why. Paths are on the user's machine."))

PRINTERS = Annotated[str, Field(description=(
    "Which printers: 'all', a set (" + ", ".join(SETS) + "), a vendor (bambu, creality, elegoo, anycubic, "
    "flashforge, qidi, wondermaker, prusa, snapmaker), or printer keys from list_printers, comma-separated."))]
PATHS = Annotated[list[str], Field(description=".3mf files, or folders of them.")]


# One command at a time: redirect_stdout is process-wide, and two slicer command
# lines at once crash (Bambu Studio's share a data folder).
_ONE_AT_A_TIME = threading.Lock()


def _run(*argv: str) -> str:
    """Run a slicerbridge command and return everything it printed, with its exit status.

    stdout is the MCP channel on a stdio server, so nothing may print to it directly;
    every slicer the engines run has its output captured too.
    """
    out = io.StringIO()
    with _ONE_AT_A_TIME, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli_main(list(argv))
        except SystemExit as e:  # argparse errors and refusals made before any work
            code = e.code if isinstance(e.code, int) else 1
            if e.code and not isinstance(e.code, int):
                print(e.code)
        except Exception as e:  # a bug: say so, and keep the server up
            code = 1
            print(f"!! unexpected error in slicerbridge {' '.join(argv[:1])}: {type(e).__name__}: {e}\n"
                  f"   Please report it: https://github.com/UnicornGraveyard/SlicerBridge/issues")
    return out.getvalue().rstrip() + f"\n\n[exit status {code}: {'ok' if code == 0 else 'something failed or was refused'}]"


def _path(p: str) -> str:
    return str(Path(os.path.expanduser(p)).resolve())


@mcp.tool(description="Every printer SlicerBridge writes for: its key, name, engine, slicer, the colors it "
                      "holds, and the sets it's in.", annotations=READS)
def list_printers(printers: PRINTERS = "all") -> str:
    return _run("printers", "--printers", printers)


@mcp.tool(description="Describe a multicolor .3mf project: its palette, the filament slots it actually uses, "
                      "each plate's size, and whether it fits each printer's bed (and at what scale if not).", annotations=READS)
def inspect_model(model_path: Annotated[str, Field(description="Path to the .3mf project.")],
                  printers: PRINTERS = "popular") -> str:
    return _run("inspect", _path(model_path), "--printers", printers)


@mcp.tool(description="Write a ready file per printer for a multicolor .3mf project (from Bambu Studio, OrcaSlicer, "
                      "a maker's fork, or a file SlicerBridge wrote), each one checked before "
                      "it is kept. Files go in <out_dir>/<name>/, named '<prefix><name> - <printer>.3mf', or "
                      "'<prefix><name> - <printer> - <suffix>.3mf' with a suffix. A "
                      "printer the model can't go to (more colors than it holds, an unknown filament type) is "
                      "refused with the reason; nothing wrong is written.", annotations=WRITES)
def convert_model(model_path: Annotated[str, Field(description="Path to the .3mf project.")],
                  printers: PRINTERS = "popular",
                  name: Annotated[str, Field(description="The model's name in file names; default: the file's name, less any printer in it.")] = "",
                  out_dir: Annotated[str, Field(description="Output folder; default: an 'out' folder beside the model.")] = "",
                  prefix: Annotated[str, Field(description="Text before every file name, e.g. 'My Shop - '.")] = "",
                  suffix: Annotated[str, Field(description="Text after the printer in every file name, e.g. "
                                                           "'My Shop' for 'Dragon - Prusa XL - My Shop.3mf'.")] = "",
                  slice_first: Annotated[bool, Field(description="Slice each file in its slicer before keeping it "
                                                                 "(a minute or two per printer).")] = False) -> str:
    src = Path(_path(model_path))
    argv = ["convert", str(src), "--printers", printers,
            "--out", _path(out_dir) if out_dir else str(src.parent / "out")]
    if name:
        argv += ["--name", name]
    if prefix:
        argv += ["--prefix", prefix]
    if suffix:
        argv += ["--suffix", suffix]
    if slice_first:
        argv.append("--slice")
    return _run(*argv)


@mcp.tool(description="Re-run SlicerBridge's checks on files it wrote (colors, paint, plates, size, settings), "
                      "and optionally slice each one.", annotations=READS)
def verify_files(paths: PATHS,
                 source_path: Annotated[str, Field(description="The project they were made from; enables "
                                                               "the color and size checks.")] = "",
                 slice_files: Annotated[bool, Field(description="Also slice each file in its slicer "
                                                                "(a minute or two each).")] = False) -> str:
    argv = ["verify", *map(_path, paths)]
    if source_path:
        argv += ["--source", _path(source_path)]
    if slice_files:
        argv.append("--slice")
    return _run(*argv)


@mcp.tool(description="Slice each file in the slicer its printer's maker ships (Creality Print, ElegooSlicer, "
                      "Anycubic Slicer Next, Flash Studio, QIDI Studio), where installed, and report what that "
                      "slicer makes of it.", annotations=READS)
def brand_check(paths: PATHS, printers: PRINTERS = "all") -> str:
    return _run("brand-check", *map(_path, paths), "--printers", printers)


@mcp.tool(description="Which slicers SlicerBridge finds, their versions, and where it keeps its templates.", annotations=READS)
def doctor() -> str:
    return _run("doctor")


def main() -> None:
    mcp.run()

"""The MCP server's tools, called in-process (needs the mcp extra)."""
import asyncio

import pytest

pytest.importorskip("mcp")
from slicerbridge.mcp_server import mcp  # noqa: E402


WRITERS = {"convert_model", "despeckle_paint"}


def call(name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return r.content[0].text if hasattr(r, "content") else r[0][0].text


def test_tools_are_listed_with_descriptions():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"list_printers", "inspect_model", "convert_model", "verify_files", "brand_check",
                          "despeckle_paint", "doctor"}
    assert all(t.description for t in tools.values())


def test_every_tool_says_how_it_behaves():
    """All four hints, explicitly: only convert_model and despeckle_paint write, and nothing reaches the network."""
    for t in asyncio.run(mcp.list_tools()):
        a = t.annotations.model_dump(by_alias=True)  # the names a client reads, whatever mcp's attributes are
        hints = [a.get(k) for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")]
        assert all(isinstance(h, bool) for h in hints), (t.name, hints)
        read_only, destructive, _idempotent, open_world = hints
        assert read_only == (t.name not in WRITERS) and not destructive and not open_world, t.name


def test_convert_and_verify_over_mcp(project, tmp_path):
    out = tmp_path / "mcp-out"
    text = call("convert_model", {"model_path": str(project), "printers": "u1", "name": "Cube", "out_dir": str(out)})
    assert "[exit status 0" in text and (out / "Cube" / "Cube - Snapmaker U1.3mf").exists()
    text = call("verify_files", {"paths": [str(out / "Cube")], "source_path": str(project)})
    assert "1/1 passed" in text


def test_despeckle_over_mcp(tmp_path):
    from builders import painted_grid
    src = painted_grid(tmp_path / "Dragon.3mf", "11111\n12111\n11111")
    text = call("despeckle_paint", {"model_path": str(src), "dry_run": True})
    assert "[exit status 0" in text and "islands   : 1 under 1 mm²" in text and "nothing written" in text
    text = call("despeckle_paint", {"model_path": str(src), "min_area_mm2": 0.5})
    assert "recolored : 2 triangles" in text and (tmp_path / "Dragon - despeckled.3mf").exists()
    assert "not a .3mf file" in call("despeckle_paint", {"model_path": "/nope/missing.3mf"})


def test_errors_come_back_as_text():
    assert "not a .3mf file" in call("inspect_model", {"model_path": "/nope/missing.3mf"})

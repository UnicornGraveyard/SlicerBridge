"""The MCP server's tools, called in-process (needs the mcp extra)."""
import asyncio

import pytest

pytest.importorskip("mcp")
from slicerbridge.mcp_server import mcp  # noqa: E402


def call(name, args):
    r = asyncio.run(mcp.call_tool(name, args))
    return r.content[0].text if hasattr(r, "content") else r[0][0].text


def test_tools_are_listed_with_descriptions():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"list_printers", "inspect_model", "convert_model", "verify_files", "brand_check", "doctor"}
    assert all(t.description for t in tools.values())


def test_convert_and_verify_over_mcp(project, tmp_path):
    out = tmp_path / "mcp-out"
    text = call("convert_model", {"model_path": str(project), "printers": "u1", "name": "Cube", "out_dir": str(out)})
    assert "[exit status 0" in text and (out / "Cube" / "Cube - Snapmaker U1.3mf").exists()
    text = call("verify_files", {"paths": [str(out / "Cube")], "source_path": str(project)})
    assert "1/1 passed" in text


def test_errors_come_back_as_text():
    assert "not a .3mf file" in call("inspect_model", {"model_path": "/nope/missing.3mf"})

# SlicerBridge for AI assistants (MCP)

`slicerbridge mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. An assistant connected
to it can look at a multicolor 3MF project, convert it for the printers you name, and read every check and
warning, the same as the CLI prints.

It runs locally over stdio, with your slicers. Nothing is uploaded, and it reads and writes only the paths it's
given ([privacy](privacy.md)).

## Claude Code and Cowork: the plugin

The SlicerBridge plugin adds the server and a skill that teaches Claude the workflow (inspect, convert, check,
never resize, never merge colors). It needs [uv](https://docs.astral.sh/uv/), which runs the server.

```text
/plugin marketplace add UnicornGraveyard/SlicerBridge
/plugin install slicerbridge@slicerbridge
```

SlicerBridge looks for each slicer in `/Applications`. If one is elsewhere, set its location in the plugin's
settings: `/plugin`, select SlicerBridge, **Configure**. An empty setting means `/Applications`.

The plugin runs `uvx slicerbridge==<its version> mcp`: an exact version, so an update to the plugin is an update
to the server too. Beside it, `plugin/uv.lock` pins that version and every package it needs to exact versions
with their PyPI hashes, for Python 3.12 and newer. Anthropic's directory checks it, and Claude Code versions
that launch plugins from their lock install exactly that set; older ones run plain `uvx`.

## Claude Desktop chats and other MCP clients

Add it to the client's MCP config. For Claude Desktop that's `claude_desktop_config.json` (Settings, Developer,
Edit Config); restart Claude Desktop afterwards.

```json
{
  "mcpServers": {
    "slicerbridge": {
      "command": "uvx",
      "args": ["slicerbridge", "mcp"],
      "env": { "SLICERBRIDGE_ORCASLICER": "/Applications/OrcaSlicer.app" }
    }
  }
}
```

Set any of the environment variables from [install.md](install.md) in `env`. A GUI app may not find `uvx` on
its `PATH`: give its full path (`which uvx`), or install SlicerBridge (`pipx install slicerbridge`) and use the
full path of `slicerbridge-mcp`.

### Claude Code without the plugin

```bash
claude mcp add slicerbridge -- uvx slicerbridge mcp
```

## Tools

| Tool | What it does | Writes files |
|---|---|---|
| `list_printers` | Every printer: key, name, engine, slicer, colors, sets | no |
| `inspect_model` | A project's palette, used slots, plates, and fit on each bed | no |
| `convert_model` | A checked file per printer into `<out_dir>/<name>/` | yes |
| `verify_files` | Re-run the checks on written files, optionally slicing them | no |
| `brand_check` | Slice files in their makers' own slicers | no |
| `doctor` | Which slicers are installed, their versions, the cache | no |

Conversions with slicing take a minute or two per printer. Ask for a few printers at a time, or convert without
slicing and run `verify_files` with slicing afterwards. The server runs one command at a time.

## Try it

> Inspect ~/Models/dragon.3mf and tell me which printers it fits. Then convert it for the Creality and QIDI
> printers, named "Dragon", and slice each file before keeping it.

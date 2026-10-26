# SlicerBridge for AI assistants (MCP)

`slicerbridge-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. An assistant connected
to it can look at a Bambu project, convert it for the printers you name, and read every check and warning, the
same as the CLI prints.

It runs locally over stdio, with your slicers. Nothing is uploaded, and it reads and writes only the paths it's
given.

## Install

```bash
pipx install "slicerbridge[mcp] @ git+https://github.com/UnicornGraveyard/SlicerBridge"
```

### Claude Code

```bash
claude mcp add slicerbridge -- slicerbridge-mcp
```

### Claude Desktop and other clients

Add it to the client's MCP config (for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "slicerbridge": {
      "command": "slicerbridge-mcp",
      "env": { "SLICERBRIDGE_ORCASLICER": "/Applications/OrcaSlicer.app" }
    }
  }
}
```

Set any of the environment variables from [install.md](install.md) in `env`.

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

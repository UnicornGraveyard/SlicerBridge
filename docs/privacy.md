# Privacy

SlicerBridge runs entirely on your computer.

- **It collects nothing.** No telemetry, analytics, crash reports, accounts or sign-ins.
- **It sends nothing.** It never uploads your models or the files it makes, and it makes no network requests
  of its own. Installing it (with pip, pipx or uv) downloads the package from PyPI; the Claude plugin's
  `uvx slicerbridge==<version>` does the same the first time it starts.
- **It reads and writes only what you give it:** the 3MF files you name, the output folder you name (or
  `out/` beside the model), and its cache of printer settings (`~/Library/Caches/SlicerBridge` on macOS,
  `~/.cache/slicerbridge` elsewhere, or `SLICERBRIDGE_CACHE`).
- **It runs your installed slicers' command lines** to build printer settings and to slice files. What those
  slicers do is up to them and their own privacy policies.
- **The MCP server** talks only to the AI assistant that started it, over stdio. It opens no network port.

Questions: [open an issue](https://github.com/UnicornGraveyard/SlicerBridge/issues). Security problems:
[report them privately](https://github.com/UnicornGraveyard/SlicerBridge/security/advisories/new).

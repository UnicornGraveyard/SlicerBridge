# Contributing to SlicerBridge

Thanks for helping more multicolor models reach more printers. Bug reports, printer requests, brand-slicer
findings and pull requests are all welcome.

## Ground rules

- **Never attach or commit someone's model.** Paid and licensed models stay private, including in issues. If a
  bug needs a model to show it, describe it (palette size, plates, parts, painted or not) or build a small
  project that shows the same thing. The tests build their own projects (`tests/builders.py`).
- **No `.3mf` files in the repository**, and no slicer profiles: SlicerBridge reads those from each user's own
  install.
- **A fix comes with a test** that fails without it.
- Be kind. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

```bash
git clone https://github.com/UnicornGraveyard/SlicerBridge && cd SlicerBridge
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,mcp]"
.venv/bin/pytest            # fast: no slicer needed
.venv/bin/ruff check .
```

On Windows the same, with `py -m venv .venv` and `.venv\Scripts\` in place of `.venv/bin/`.

The end-to-end tests need real slicers and build templates (minutes the first time):

```bash
SLICERBRIDGE_SLICER_TESTS=1 .venv/bin/pytest -m slicer
```

Run them before a pull request that changes an engine, a check or the printer table, and say so in the PR.

## What helps most

- **A printer.** See [docs/adding-a-printer.md](docs/adding-a-printer.md).
- **A brand slicer finding.** If a file won't open or slice in a maker's own slicer, run
  `slicerbridge brand-check` on it and open a "Slicer compatibility" issue with the output, the slicer's
  version, and where in its source the behavior comes from if you can find it.
- **Linux.** The slicer paths and profile folders are the missing pieces ([docs/install.md](docs/install.md#linux)).
- **Windows.** Trying OrcaSlicer, PrusaSlicer and the brand slicers there, and saying where they installed
  ([docs/install.md](docs/install.md#windows)).
- **Other filament types** than PLA, PETG and ABS (Prusa printers take PLA only so far).

## Pull requests

- Keep each one to one change, with a test.
- Match the code around it: plain, explicit, comments that say why.
- Update the docs and the README's printer table when you change what users see.
- `pytest` and `ruff check .` pass; CI runs both.

Releases: [docs/releasing.md](docs/releasing.md).

By contributing you agree that your contribution is licensed under the GPL v3 or later, like the rest of the
project.

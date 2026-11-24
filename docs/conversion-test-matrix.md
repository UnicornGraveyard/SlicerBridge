# 3MF conversion regression matrix

Use the [printer table](../README.md#what-comes-out) for per-printer keys and color limits. A target printer's output format and the ability to read a source file from its slicer are separate capabilities.

| Source fixture | Output or expected refusal | Regression evidence |
| --- | --- | --- |
| Bambu Studio 2.8, OrcaSlicer 2.4.2, QIDI Studio, Anycubic Slicer Next and ElegooSlicer application stamps | Generate a U1 project, audit it against the source, verify the target template version | Parameterized cases in [test_inputs.py](../tests/test_inputs.py) using `bambu_project` |
| Project saved by Flash Studio | README records a saved-project check. The synthetic input parametrization lacks a Flash Studio row. Capture the application stamp on a synthetic project before extending it. | [Input support](../README.md#what-goes-in) and [input cases](../tests/test_inputs.py) |
| Creality Print project | Input unverified. Obtain a small GUI-saved, multicolor synthetic project before claiming support. Creality target output has independent coverage. | [Brand slicer notes](brand-slicers.md) |
| Converted project | U1 → U1 must preserve paint and avoid duplicating the target name. The README also lists Bambu and Orca-family output as reusable inputs. | Round-trip test in [test_inputs.py](../tests/test_inputs.py) |
| Native PrusaSlicer project or mesh-only archive | Identify native Prusa input as unsupported. Reject mesh-only conversion with guidance. Prusa remains an output target. | Minimal ZIP fixtures in [test_inputs.py](../tests/test_inputs.py) |

| Output engine | Fast checks | Opt-in verification |
| --- | --- | --- |
| Bambu Studio and OrcaSlicer family | Verify settings read-back, fallback, tower placement and partial-write cleanup in [test_orca_write.py](../tests/test_orca_write.py). | [Real-slicer suite](../tests/test_with_slicers.py) selects representative profiles, including A1, X2D, K2 Plus and Q2. |
| PrusaSlicer | Check per-plate output, palette capacity, painted colors, verification against the source and mixed filament presets in [test_prusa_write.py](../tests/test_prusa_write.py). | Real-slicer suite converts Prusa profiles and checks type-specific presets. |
| Snapmaker Orca / U1 | Exercise successful slicing, missing executable, rejected settings and failure handling with a CLI stand-in in [test_u1_slice.py](../tests/test_u1_slice.py). | Real-slicer suite includes a U1 mixed-material case and checks its bundled presets. |

Build fixtures with [builders.py](../tests/builders.py) and the shared [pytest fixtures](../tests/conftest.py). Keep models and 3MF binaries out of PRs. For a regression, record the source slicer/version, target key, plate count, palette, expected check or refusal, and the smallest synthetic case that reproduces it.

From the environment in [CONTRIBUTING.md](../CONTRIBUTING.md), run `.venv/bin/pytest -q` and `.venv/bin/ruff check .`. Run `SLICERBRIDGE_SLICER_TESTS=1 .venv/bin/pytest -m slicer -rs` for real-engine coverage. Report installed versions, passed target cases and skips. A skipped executable leaves that target unverified.

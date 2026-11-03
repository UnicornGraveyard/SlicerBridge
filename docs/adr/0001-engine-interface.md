# ADR-0001: No common engine interface yet; close the Prusa verify gap now

**Status:** Proposed
**Date:** 2026-11-02
**Deciders:** Unicorn Graveyard (maintainer)

## Context

SlicerBridge writes one checked file per printer through three engines:

| Engine | Printers | Module | Checks |
|---|---|---|---|
| orca | 29 (10 Bambu Studio, 19 OrcaSlicer) | `engines/orca.py` (1,069 lines) | `checks/orca.py`: `audit()`, `slice_check()` |
| prusa | 4 (MMU3, XL, Core One INDX 4T/8T) | `engines/prusa.py` (1,201 lines) | inside the engine (`slice_check()`, check 9), only while writing |
| u1 | 1 (Snapmaker U1) | `engines/u1.py` (789 lines) | `checks/u1.py`: `audit()`; check 13 through `checks/orca.slice_check` |

The architecture review of 2026-10-31 found the engines solving the same problems three ways. 0.4.2 fixed most
of that:

- one output module (`output.py`: land, move aside, withdraw), #19;
- one source reader (`source.py`), #19;
- check 13 returns a structured result, not text, #18;
- one helper reads the settings a slicer won't load (`refused_settings`), #20;
- CI runs the orca engine's write path with a stand-in slicer, #17.

What is left is dispatch. Every command branches on the engine by name:

- `cli.py` has an `if engine == …` chain in `convert` (prusa alone takes `explicit`), `inspect` (u1's
  `inspect(src)` takes no keys), `printers`, `verify` and `templates`.
- `printers.py` answers `label_of`, `tools_of` and `vendor_of` with three parallel dicts of lambdas keyed by
  engine, and `engine_of` hard-codes `"u1"`.
- `checks/orca.slice_check` special-cases `key == "u1"` to pick Snapmaker Orca.
- `verify` can't re-check a Prusa file: it says "Prusa files are checked while they're written: convert again
  to re-check", because the Prusa checks live inside the engine.

The forces at play:

- **Correctness over elegance.** Every change is proven by converting Gatefang for all 34 printers and getting
  identical files. A refactor that only moves code buys nothing a user sees.
- **One maintainer.** Time spent on structure is time not spent on printers.
- **Growth is in printers, not engines.** Recent additions (Creator 5 and 5 Pro, INDX 4T/8T, the U1) were all
  rows in an existing engine's table. No fourth engine is planned. The likeliest one is reading PrusaSlicer
  projects as input, which is an input reader, not an output engine.
- **The Prusa verify gap is user-facing.** A Prusa file that sat in `out/` can't be re-checked after a slicer
  update; the others can.

## Decision

**Don't introduce a common engine interface now.** Keep explicit per-engine dispatch, and make two targeted
changes:

1. **Give the Prusa engine an `audit(f, src)` that `verify` calls**, like the other two engines. Its eleven
   checks run on a written file: the ones that need only the file always (structure, tool range, the config
   PrusaSlicer reads, on the bed, and PrusaSlicer loading it when installed), the rest with `--source`
   (paint, colors, modifiers, size). `verify --slice` slices it in PrusaSlicer as the engine already does
   while writing. Eight of the checks compare the file with what only the conversion knew (which plate it
   is, the slot-to-tool mapping, whether it fit the bed), and `--map` can change the mapping, so **each Prusa
   file records them** in its zip comment, after the `SlicerBridge` stamp: PrusaSlicer never reads the
   comment, and SlicerBridge only checks how it starts. A file written before that falls back to the
   default mapping, with a warning, and `verify --map` for one converted with a map.
2. **Replace `printers.py`'s three dicts of lambdas with one lookup**: a small `PrinterInfo(label, tools,
   vendor, engine, slicer)` for every key, built once from the three tables, so `label_of`, `tools_of`,
   `vendor_of` and `engine_of` read one record instead of switching on the engine.

Revisit the interface (Option B below) when any one of these happens: a fourth output engine is planned; a
fifth command needs its own per-engine branch in `cli.py`; or a change to one engine's call signature has to be
mirrored by hand in `cli.py` again.

## Options Considered

### Option A: Explicit dispatch, plus the two targeted changes (chosen)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low: two local changes, no new abstraction |
| Cost | About a day for the four PRs, plus an hour or two on a Mac for the Gatefang 34-printer comparison |
| Scalability | Fine for three engines; adding a fourth means editing five `cli.py` branches |
| Team familiarity | High: the code stays as it reads today |

**Pros:** Closes the one user-facing gap (Prusa verify), and puts the Prusa engine's write path in CI. Removes the most error-prone duplication (three
parallel lambda tables that must list the same engines). Leaves code that just went through a refactor alone
while it settles. Every engine keeps the arguments it actually needs (`explicit` for Prusa).

**Cons:** `cli.py` keeps its `if engine == …` chains. A fourth engine stays a multi-file change.

### Option B: A small engine protocol with a registry

Each engine module exposes `convert(src, name, keys, out, …)`, `inspect(src, keys)`, `audit(f, src)` and
`slice_check(f, key)`, and `engines/__init__.py` registers them by name. `cli.py` loops over the registry.
`printers.py` asks each engine for its printers.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium: one protocol, a registry, uniform signatures (Prusa's `explicit` becomes an option every engine accepts and ignores) |
| Cost | Two to three days: touches `cli.py`, `printers.py`, all three engines and their tests, and needs the full Gatefang comparison |
| Scalability | Good: a fourth engine is one module plus a registry line |
| Team familiarity | Medium: a protocol to learn and keep honest |

**Pros:** `cli.py` stops branching. `verify` covers every engine by construction. The `key == "u1"` special
case in `checks/orca.slice_check` goes away.

**Cons:** It pays for an extensibility no current plan needs. Uniform signatures hide real differences (Prusa
writes one file per plate; the U1 has one printer and no keys to inspect). It does the Prusa verify fix too, but
three days later.

### Option C: Full plugin architecture

Engines and printers as installable plugins (entry points), a shared geometry module for the tower and plate
model, and per-printer classes.

| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Cost | A week or more, and a long tail of behaviour to re-prove |
| Scalability | Best, including third-party printers |
| Team familiarity | Low |

**Pros:** Others could add printers without forking. The geometry the checks share with the engine would have
one owner.

**Cons:** No third party is asking to add printers. The geometry sharing is documented and deliberate (0.4.2,
[checks.md](../checks.md#what-the-checks-share-with-the-engines)). Highest risk of a behaviour change that only
shows up on a real print.

## Trade-off Analysis

The question is whether to pay for extensibility before it's needed. Since 0.4.2, the engines' shared work
(output, source reading, slice results, refused settings) has one implementation each. What's left duplicated is
routing: about five `if engine == …` chains in `cli.py`, which are easy to read and fail loudly when wrong. B
would remove them, but its real benefit, a cheap fourth engine, has no customer yet. Meanwhile its uniform
signatures would blur the real differences between engines.

The Prusa verify gap is the one place where the missing interface costs a user something, and it can be closed
directly. The lambda tables in `printers.py` are the one place where the duplication can silently go wrong (an
engine added to one dict and not another), and one record removes that without an interface.

So A takes B's two concrete benefits for a third of the cost, and leaves a clear trigger for doing B.

## Consequences

- **Easier:** re-checking any file SlicerBridge wrote, Prusa ones included, after a slicer update. Looking up a
  printer's label, tool count, vendor or engine: one record, one place.
- **Harder:** nothing new. Adding a fourth engine stays as hard as today: a module, plus a branch in each `cli.py`
  command and in `printers.py`'s record builder.
- **To revisit:** this decision, at any of the triggers above. If a PrusaSlicer-project *reader* is added, it
  belongs with `source.py`, not as an engine, and doesn't trigger B.

## Action Items

One PR each, in this order:

1. [ ] **One record per printer.** `printers.py`: a `PrinterInfo(key, label, tools, vendor, engine, slicer)`
   for every key; `label_of`, `tools_of`, `vendor_of` and `engine_of` read it, and `cli printers` takes the
   slicer from it. A test pins every key's answers to what they were before.
2. [ ] **The Prusa write path in CI.** A test fixture writes a minimal made-up Prusa profile per printer (the
   settings the engine reads, none of Prusa's own), so `convert_one` runs without PrusaSlicer, as #17 did
   for the orca engine. Checks 9 and 10 skip themselves without PrusaSlicer, as they do today.
3. [ ] **Prusa files record how they were made.** The zip comment becomes the stamp plus a small JSON record:
   plate number, mapping, whether the model fit the bed, whether it was turned. Prusa files then differ
   from today's by that comment alone.
4. [ ] **`verify` re-checks Prusa files.** The engine's `verify()` takes an optional expectation (source,
   plate, mapping, placement); `audit(f, src)` builds it from the file's record and the source; `cli verify`
   calls it instead of the "convert again to re-check" warning, and takes `--map` for files written before
   (3). It runs check 9 (PrusaSlicer `--info`) whenever PrusaSlicer is installed, as converting does.
   Tests: a written file passes with and without `--source`; a wrong color or a model off the bed fails; a
   file without a record passes with the warning. `docs/checks.md` and the CHANGELOG.
5. [ ] **Prove it on a Mac:** `pytest`, the slicer tests (`SLICERBRIDGE_SLICER_TESTS=1 pytest -m slicer`), and
   Gatefang for all 34 printers, the same as `main`'s apart from the Prusa files' zip comment (compare the
   files' contents, not their bytes); `verify out/GateFang --source …` passes 34/34 with the Prusa files
   checked. Then release.
6. [ ] Set this ADR's status to Accepted once 1–4 are merged.

# 24 — Take ownership of the toolkit's test suite

> **Status**: 🔲 Not started
> **Created**: 2026-07-30
> **Project**: custos (`tesseract-trading/custos/`)
> **Authority**: owner decision 2026-07-30, recorded in PS Plan 60 (§Slice D 结果, option (b))
> **Depends on**: PS Plan 60 Slice D ✅ (imports already name the toolkit)
> **Blocks**: PS Plan 60 Slice E — PS cannot delete `shared/` while this coverage lives only there
> **multi_session_scope**: **true** (83 files, ~1500 tests, cross-repo)

## Context

Plan 06 vendored philosophers-stone's `shared/` tree into this repository as
`custos_toolkit` / `custos_toolkit_nautilus`, and Plan 07 declared this repository its
authority. **The code came; the tests did not.**

Measured 2026-07-30:

| | tests covering that code |
|---|---|
| philosophers-stone | **1720** (~100 files) |
| custos | **2** (`tests/toolkit/test_nautilus_strategy_registry.py`) |

So the toolkit that executes live trades is protected by two tests inside the repository
that owns it. Everything else guarding it sits in a repository that cannot change it.

That asymmetry is not merely untidy. **This repository can break its own toolkit with a
green CI**, because nothing here exercises the filters, the config loader, the risk
manager, the warmup machinery, the coordinators or the sizing paths. PS would go red, but
PS cannot fix the toolkit — it can only file a plan here.

PS Plan 60 Slice D already rewrote those tests to name `custos_toolkit*`, so they run
against this repository's code as they stand. What remains is moving them to where the
code lives.

## Goal

The toolkit's coverage lives in this repository and runs in its CI. Breaking the toolkit
here turns this repository's own suite red.

## Non-goals

- **Not a rewrite.** These tests already pass against the toolkit. Porting them is a move
  plus whatever fixture wiring this repository needs, not a redesign.
- **Not PS's consumer tests.** Tests that exercise PS strategies, the artifact chain, the
  deploy paths or the Hummingbot subtree stay in PS. See the classification below.
- **Not the toolkit's own source.** No behaviour change to `custos_toolkit*` in this plan.
  If a ported test fails, that is a finding to triage, not a licence to edit the test until
  it passes.

## Scope (PS-side scan, 2026-07-30)

Classified by the top-level packages each test file imports:

| category | count | disposition |
|---|---|---|
| toolkit only | **83** | **move here** |
| mixed (toolkit + PS's own) | 18 | stay in PS — they import `trend` / `momentum` / `portfolio` / `deploy` / `scripts`, i.e. they test PS's *consumption* of the toolkit |
| PS only | 42 | stay in PS (Hummingbot, sidecar/deploy, artifact chain) |

The 83 cover: filters (both the platform-neutral set and the engine-backed set), config
loader and validators, risk manager / controller / equity / order calculators, position
sizing and tracking, signals, warmup, protocols, and the Nautilus adapter's coordinators,
orders, pair context, tick monitor, sltp, snapshot and startup validator.

## What the move has already taught us — read before starting

**An import scan does not find every dependency.** PS's Slice D residue check swept import
statements and reported clean. `tests/test_filter_manager.py` was still loading its subject
by filesystem path:

```python
spec_from_file_location("filter_manager", "shared/nautilus/filter_manager.py")
```

It therefore tested **PS's copy**, leaving this repository's `filter_manager` at zero
coverage while 39 tests passed. Pointing it at the toolkit immediately exposed two layering
assertions that only held for PS's copy.

So the port must check, per file, that the subject under test is this repository's module:

- no `spec_from_file_location` / `Path(...).read_text()` against a repo-relative source path
- `inspect.getsource(module)` rather than reading a path, where source is inspected at all
- after porting, assert the module origin: a filter created from the wrong package still
  satisfies every behavioural test, which is precisely why the layering guards exist

## Tasks

### Task 1: Land a landing zone and prove one file end to end

**RED**: port a single non-trivial file — `test_filter_manager.py` is the right first pick,
since it is the one already known to have had a path dependency — and have it fail before
the fixtures exist.

**Implementation**: decide where ported tests live (`tests/toolkit/` already exists with one
file) and what they may depend on. Establish whether this repository's `conftest.py` needs
anything PS's provides. Do not port in bulk before one file passes here.

**Commit**: `test(toolkit): adopt the first of the toolkit's own tests`

### Task 2: Port the platform-neutral set

**Implementation**: filters, config, risk, position, signals, warmup, protocols. These need
no engine and should be the cheapest. Record any test that fails on arrival rather than
adjusting it into passing — a failure here means the vendored copy diverged from what PS
was testing, which is a finding worth its own entry.

**Commit**: `test(toolkit): adopt the platform-neutral coverage`

### Task 3: Port the Nautilus adapter set

**Implementation**: coordinators, orders, pair context, tick monitor, sltp, snapshot,
startup validator, filter manager, indicators. These need `nautilus_trader`; mirror
whatever skip/gate convention this repository already uses for engine-dependent tests.

**Commit**: `test(toolkit): adopt the engine adapter coverage`

### Task 4: Make the coverage load-bearing, then let PS drop its copy

**Implementation**: the ported tests must run in this repository's default verification —
coverage that exists but is not run is the situation this plan set out to fix. Then notify PS that
Plan 60 Slice E may delete `shared/`.

**Verify**: mutate one toolkit source file deliberately (e.g. invert a filter threshold
comparison) and confirm this repository's suite goes red. Exit-code-zero is not the
evidence; the red is.

**Commit**: `docs(custos): record that the toolkit's coverage now lives with the toolkit`

## Verification

- [ ] `make verify` green with the ported tests included
- [ ] A deliberate mutation in `custos_toolkit*` turns this repository's suite red — the
      only evidence that the coverage is load-bearing rather than merely present
- [ ] No ported test loads its subject from a filesystem path
- [ ] PS notified that Slice E is unblocked

## Deviations & improvements

- Any test that fails on arrival: record it here with the divergence it exposes. Those are
  the most valuable output of this plan, since they are differences between the vendored
  copy and the code PS was actually testing.
- If a ported test needs a fixture this repository does not have, note whether the fixture
  belongs here or the test belongs in PS after all.
- PS keeps `test_taste_guard_nautilus_base.py`-style code-style guards out of scope: PS
  should not police this repository's style, and the subject it scans is disappearing.

## Close-out Report

（执行完成后填写）

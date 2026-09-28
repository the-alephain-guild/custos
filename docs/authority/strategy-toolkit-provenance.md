# Strategy Toolkit Provenance and Extraction Authority

## Authority chain

The Custos strategy toolkit extraction is governed, in order, by:

1. `strategy-toolkit-inventory-v1.json`, the immutable Task 2 classification of all 241 legacy files.
2. `strategy-toolkit-extraction-v1.json`, the Task 4 mapping from pinned source Git blobs to package targets.
3. `scripts/check-toolkit-extraction.py`, the deterministic zero-rewrite verifier.
4. The Task 4 receipt, which may become handoff-ready only after focused and repository gates pass at an exact commit.
5. `strategy-toolkit-typing-closure-v1.json`, the versioned digest map from the exact Task 4 implementation to the typed Task 4b candidate.
6. The Task 4b receipt, which binds exact implementation commit `5a19a816d4f6d90e7d3fbde80d39f562decd8c4b` and is `READY_TYPING_CLOSURE` for the Task 4b handoff scope only.

The inventory remains historical evidence and is not regenerated after cutover. The extraction manifest binds every old path and target path to SHA-256 digests and to the exact source commit.

## Canonical package layout

- `custos-strategy-toolkit` owns the Python 3.11-compatible contracts and 36 platform-neutral implementation files under `custos_toolkit`.
- `custos-strategy-toolkit-nautilus` owns 55 Nautilus adapter files under `custos_toolkit_nautilus.adapter`.
- Its private `_vendor/pandas_ta` namespace owns the 150-file MIT-licensed vendor snapshot.
- `src/custos/engines/nautilus/toolkit/__init__.py` is an implementation-free deprecation marker only. It must not change `sys.path`, register fake distributions, or expose old `shared` or top-level `pandas_ta` aliases.

There is one canonical implementation for every inventory entry. The old `shared/` and `vendor/` implementation trees must not remain after extraction.

## Zero-rewrite rule

Algorithm and business behavior are copied from the pinned source blobs without rewrite. The only permitted transformations are:

- mapping old `shared` imports into `custos_toolkit` or `custos_toolkit_nautilus.adapter`;
- mapping top-level `pandas_ta` imports into the private `custos_toolkit_nautilus._vendor.pandas_ta` namespace;
- replacing the vendor's installed-distribution lookup with fixed `0.0.0+vendored` metadata, removing the former `pkg_resources` shim.

The extraction checker reconstructs every expected target from the pinned Git blob using exactly those transformations. Any other byte drift fails closed.

## License and dependency boundary

The private vendor license is shipped at `custos_toolkit_nautilus/_vendor/pandas_ta/LICENSE`. No wheel may publish `shared` or `pandas_ta` as a top-level package. The base distribution must remain importable on Python 3.11 without NautilusTrader; engine imports and vendor dependencies remain confined to the Nautilus distribution.

## Upstream sync procedure

The upstream research copy `philosophers-stone/shared/` was retired on 2026-09-28
(philosophers-stone plan 64): the toolkit is the only implementation, and there is no
longer a tree to compare it against. `make toolkit-sync-check`, which diffed the
vendored subset against that tree, is removed with its tests; shared behaviour changes
start in this repository and reach strategies through a new toolkit release candidate.

A future upstream change requires a new versioned inventory and extraction manifest. It must not mutate v1 evidence. The change must:

1. pin the new upstream and source commits;
2. classify every added, removed, and changed file;
3. generate a new one-to-one extraction receipt;
4. pass fixed-input signal/order-intent and indicator parity;
5. prove both wheels ship no retired top-level aliases.

Task 4 establishes package authority only. Runtime verifier activation and consumer cutover remain later Plan 18 tasks.

## Task 4b typing closure

Task 4b does not rewrite or replace Task 4 evidence. The historical extraction manifest,
baseline, and receipt stay immutable. The closure manifest binds each of the 241 Task 4
target digests to its typed target digest, separately records local type stubs and typed
boundary support files, and requires whole-package strict mypy with zero errors. Private
vendor bytes remain identical to Task 4 and stay outside mypy only because they are
third-party source; their digests and fixed-input parity remain mandatory.

Exact implementation commit `5a19a816d4f6d90e7d3fbde80d39f562decd8c4b` passed a
clean exact-HEAD `make verify`: 508 tests passed, 4 skipped, and 1 xfailed; all 241
historical extraction mappings, authority/assets gates, and whole-package strict mypy
profiles (40 base files and 59 Nautilus files) passed. The receipt is therefore
`READY_TYPING_CLOSURE` with `handoff_ready=true` only for Task 4b. Task 5's public
pre-import verifier/attestation contract and Task 6's immutable release candidate still
block 18b, runtime-ready, and production-ready claims.

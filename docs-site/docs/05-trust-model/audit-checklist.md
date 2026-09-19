---
title: "Audit checklist"
sidebar_position: 7
---

Record the source revision, dependency profile and dirty state before reviewing the runner. Keep source checks, image checks and deployed acceptance as separate results.

## Source checks

```bash
make verify
make toolkit-typecheck
uv run python scripts/check-docs-site.py
```

Install dependencies first. The base suite may skip Nautilus-dependent tests; inspect skips and run the NT profile when evaluating engine behavior. A green baseline is evidence for the tested scope, not proof of every security claim.

| Area | Source/test entry points |
|---|---|
| Credentials | `src/custos/core/per_key_vault.py`, `tests/test_credential_lifecycle.py` |
| Signed admission | `src/custos/core/engine_lifecycle.py`, `tests/test_engine_lifecycle.py` |
| Offline boundary | `src/custos/offline/mode_guard.py`, `tests/test_offline_mode_guard.py` |
| Venue declarations | `tests/test_nt_venue_wiring.py`, `tests/test_nt_sodex_venue.py` |
| Breaker and containment | `src/custos/offline/safety.py`, `tests/core/test_fallback_breaker.py` |
| Fact durability and arithmetic | `tests/test_runner_fact_store.py`, `tests/test_strategy_signal_fact_contract_v1.py` |

Inspect failure tests as well as passing paths. Check that keys do not enter logs or outbound telemetry, modes cannot bypass admission, and rejected operations do not reach a venue. Text searches help locate code but do not prove absence of a leak or bypass.

## Operational checks

Run the [standalone sandbox exercise](/getting-started/standalone-sandbox) for actual local identity, encryption, broker, apply and stop behavior. It uses the simulation host and does not test a real venue or signed end-to-end deployment.

```bash
make verify-local-v030
```

This checks the built image contract and its revision label. It does not include full standalone or signed deployment acceptance. Test the exact deployed artifact separately and retain the result with its image digest and revision.

## Boundaries to review

Signed commands require upstream authority; offline commands are explicit unsigned sandbox/testnet input. Offline material cannot authorize live execution. Current live composition remains disabled. A venue key's actual permissions and host access controls need operator verification beyond a local scope declaration.

Follow [release verification](/trust-model/signed-release-chain) for signed artifacts and [release status](/release-governance/release-status) for open acceptance boundaries.

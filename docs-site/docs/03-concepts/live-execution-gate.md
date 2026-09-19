---
title: "Execution admission"
sidebar_position: 3
---

Signed deployments pass admission before engine construction. A refused deployment produces a typed terminal outcome; recoverable runtime failures use a separate bounded retry path.

## Signed-lane checks

| Check | Applies to |
|---|---|
| Verified artifact runtime capability | All modes |
| Runtime mode matches signed command | All modes |
| Host supports the mode and connector | All modes |
| Credential declares `trade_no_withdraw` | Testnet/live |
| Live execution enabled in the composition | Live |
| Bound signed promotion evidence present | Live |

Live execution is disabled by default. The signed daemon accepts a verified runtime approval bound to the installed image and source revision. This approval does not replace the signed deployment promotion, artifact checks or current risk policy. See [production preparation](/operator-guide/production-preparation).

`sandbox-sim` supports sandbox only. Nautilus connector declarations vary by mode; see the generated [connector table](/engines/nautilus-trader). Binance, SoDEX and OKX each have spot and linear perpetual configuration paths; account and release acceptance remain separate requirements.

## Offline admission

Offline work is selected explicitly with `--reconcile-strategy-id`. Its separate mode guard rejects live before parsing/publishing the spec or reading credentials. It uses local strategy material rather than signed release capability, and does not produce promotion evidence. Local safety remains active.

## Inspecting the implementation

The signed supervisor checks admission in `src/custos/core/engine_lifecycle.py`. Offline boundaries are in `src/custos/offline/mode_guard.py`. Relevant tests include `tests/test_engine_lifecycle.py`, `tests/test_nautilus_host_capability.py`, `tests/test_nt_venue_wiring.py` and `tests/test_offline_mode_guard.py`.

Admission establishes execution eligibility. Continuous exposure and drawdown enforcement are described in [safety during disconnects](/trust-model/safety-survives-disconnect).

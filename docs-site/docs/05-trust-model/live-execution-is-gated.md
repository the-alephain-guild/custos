---
title: "Live execution is gated"
sidebar_position: 3
---

Live execution requires signed deployment authority, a compatible engine and connector, a permitted credential, verified artifact capability and bound promotion evidence. The current daemon composition keeps live disabled.

The complete condition table is maintained in [execution admission](/concepts/live-execution-gate). `SandboxSimulationHost` declares only sandbox, and the offline lane rejects live independently. Starting a live transport session cannot change these execution checks.

## Verification

```bash
uv run pytest tests/test_engine_lifecycle.py \
  tests/test_nautilus_host_capability.py \
  tests/test_nt_venue_wiring.py tests/test_offline_mode_guard.py
```

The tests cover refusal before engine actions, per-mode connector declarations and offline mode boundaries. They are source-level evidence; they do not certify a deployed image or a venue account. See [release status](/release-governance/release-status) for current acceptance limits.

After admission, local safety continues enforcing exposure and drawdown. A permitted deployment can still be stopped by those guards.

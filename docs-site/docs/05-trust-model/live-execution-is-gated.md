---
title: "Live Execution Is Always Gated"
sidebar_position: 3
---

# Live Execution Is Always Gated

Before Custos will place an order on a real venue, four independent checks must
pass. If any one fails, the deployment is refused. The gate never degrades to a
weaker mode, and it never accepts silently.

The mechanics are in [live execution gate](/concepts/live-execution-gate). This
page is about why it is a guarantee rather than a feature, and how to verify
that it actually holds.

## The failure it prevents

Custos ships more than one execution host. The no-op host accepts a deployment,
reports healthy, and places no orders — which is exactly what you want for
rehearsing enrollment and reconciliation.

In `live` mode that same behaviour is the dangerous one. An order routed to a
host that quietly does nothing is indistinguishable, from the outside, from an
order that succeeded. Your status says running, your logs say healthy, and you
find out at reconciliation that nothing was ever placed.

So the gate refuses rather than degrades. A refused deployment is loud and
recoverable; a silently ignored one is neither.

## The four checks

| Layer | Question | Refusal event |
|---|---|---|
| 1 | Does this engine declare live support? | `g6_gate_live_capability_denied` |
| 2 | Does it support the venue named in the deployment? | `g6_gate_venue_unsupported` |
| 3 | Does the strategy code hash match the local source? | `g6_gate_code_hash_mismatch` |
| 4 | Is the credential scoped `trade_no_withdraw`? | `g6_gate_credential_scope_violation` |

Each emits a distinct event, so an operator can tell from the log which one
refused rather than guessing.

Layer 3 is what stops a signed deployment from running code other than the code
it was approved for. Layer 4 is a backstop — the vault already refuses to store
a withdraw-capable credential — because one enforcement point is one mistake
away from being bypassed.

## Separation of duties

Live deployments additionally require at least two distinct approvers recorded
in the signed deployment. A deployment without them is refused with
`sod_approval_missing`.

Approval is an ARX decision. Custos does not grant it and does not evaluate who
counts as an approver — it refuses to act without evidence that the decision
happened.

## Verifying it

The interesting question is not whether the checks exist, but whether they are
**live** rather than dead code. A check that can never fire looks identical, in
a passing test suite, to a check that always passes.

```bash
# no venue client constructed outside the engine host
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ --exclude=host.py --exclude=venue_binance.py
```

Returns nothing on a clean tree. A venue client built anywhere else would be a
path around the gate entirely.

Read `supports_live` and `supports_venue` in
`src/custos/engines/nautilus/host.py` — that is the capability surface
admission queries. The no-op host declares `supports_live() -> False`, which is
what layer 1 reads.
<!-- disclosure-ok: auditable source location -->

Coverage:

| What | Test |
|---|---|
| Capability declarations per host | `tests/test_nautilus_host_capability.py` |
| Which host a given mode may bind | `tests/test_main_host_selection.py` |
| Venue adapter and credential wiring | `tests/test_nt_binance_venue.py` |

<!-- disclosure-ok: auditable source location -->

One detail worth checking while reading: the trading-mode comparison is
case-insensitive. ARX and the runner serialize the mode differently, and a
case-sensitive comparison here would produce a gate that silently never fires —
the exact dead-check failure this section is about.

## What the gate does not do

It protects the boundary between a deployment and a venue. It is not a risk
engine and has no opinion on whether a trade is wise. Exposure limits are
enforced separately and continuously — see
[safety survives a disconnect](./safety-survives-disconnect).

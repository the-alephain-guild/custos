---
title: "Live Execution Is Always Gated"
sidebar_position: 3
---

# Live Execution Is Always Gated

Before Custos will run a deployment against a real venue, a fixed set of
conditions must hold. If any of them does not, the deployment is refused. The
gate never degrades to a weaker mode, and it never accepts silently.

The mechanics are in [live execution gate](/concepts/live-execution-gate). This
page is about why it is a guarantee rather than a feature, and how to check that
it actually holds.

## The failure it prevents

Custos ships more than one execution host. One of them simulates: it accepts a
deployment, runs the entire local lifecycle, and never connects to a venue —
which is exactly what you want for rehearsing enrollment and reconciliation.

In a real-venue mode that same behaviour becomes the dangerous one. A deployment
routed to a host that does not trade is indistinguishable, from the outside,
from one that trades correctly. Your status says running, your logs say healthy,
and you find out at reconciliation that nothing was ever placed.

So the gate refuses rather than degrades. A refused deployment is loud and
recoverable; a silently simulated one is neither.

## What is actually checked

Seven conditions, evaluated in one place before any engine is constructed. Four
apply to every deployment; three are specific to real-venue and live modes.

| Condition | Applies to |
|---|---|
| Artifact runtime capability is `READY` | every mode |
| Runtime mode equals the signed mode | every mode |
| Engine declares support for that mode | every mode |
| Engine declares support for the signed connector | every mode |
| Credential is scoped `trade_no_withdraw` | `testnet`, `live` |
| Live execution is enabled in this build | `live` |
| Signed promotion evidence is present | `live` |

Two of these deserve emphasis because they are what make the guarantee hard to
work around.

**The build carries the live capability.** Live execution is off by default and
is turned on only in the composition root that consumes the final image receipt.
It is not an environment variable and not a configuration setting, so it is not
something an operator can enable under pressure — including under pressure
applied by someone else.

**The credential is scoped independently.** A withdraw-capable credential is
refused here even though the vault already refuses to store one. Two enforcement
points, because one is always one mistake away from being none.

## Approval stays upstream

Live deployments carry signed promotion evidence issued by ARX. Custos verifies
that the evidence is present and bound to this deployment. It does not evaluate
who approved it, how many approvers there were, or whether the approval was
sound.

That split is the point. Custos refuses to act without evidence that a decision
was made, and holds no opinion about the decision — which is why compromising
the runner does not let you manufacture an approval, and compromising the
approval path does not let you reach a venue without a runner that will execute.

## Verifying it

The interesting question is not whether the checks exist, but whether they are
**live** rather than dead code. A check that can never fire looks identical, in
a passing test suite, to a check that always passes.

Read `_require_authorized_runtime` in `src/custos/core/engine_lifecycle.py`.
That one function is the entire gate — there is no second admission path, and no
caller reaches engine construction without going through it. Confirm that for
yourself rather than taking this page's word for it: the value of an open runner
is that the claim and the code are in the same repository.
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

The host capability surface it queries is in
`src/custos/engines/nautilus/host.py`. `SandboxSimulationHost.supports_trading_mode`
returns true only for `sandbox`; `NtTradingNodeHost` accepts all three modes.
That declaration is what refuses a live deployment on the simulation host — the
host says what it can do, and admission believes it rather than maintaining a
separate list that could drift.
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

Then confirm there is no path around the gate at all:

```bash
grep -rn 'CEXOMS\|BinanceClient\|OKXClient' src/ \
  --exclude=host.py --exclude=venue_binance.py
```

Returns nothing on a clean tree. A venue client constructed anywhere else would
bypass admission entirely, and no amount of correctness inside the gate would
compensate for it.

Coverage:

| What | Test |
|---|---|
| Per-host mode and connector declarations | `tests/test_nautilus_host_capability.py` |
| Which host a given selection binds | `tests/test_main_host_selection.py` |
| Blocked capability and live mode refuse before any engine action | `tests/test_engine_lifecycle.py` |
| Venue adapter and credential wiring | `tests/test_nt_binance_venue.py` |
<!-- disclosure-ok: auditable source location, custos is open for exactly this -->

The third row is the one that matters most for this page. It asserts that a
blocked capability and a live-mode refusal both happen *before* any engine
action — which is what makes this an admission gate rather than a cleanup
routine.

## What the gate does not do

It governs admission, not conduct. It has no opinion on whether a trade is wise.
Exposure limits and drawdown breakers are enforced separately and continuously,
by modules that do not know which engine is underneath — see
[safety survives a disconnect](./safety-survives-disconnect).

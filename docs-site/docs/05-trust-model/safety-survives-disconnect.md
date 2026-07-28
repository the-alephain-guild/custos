---
title: "Safety Survives a Disconnect"
sidebar_position: 4
---

# Safety Survives a Disconnect

Losing the connection to ARX does not stop your strategies, and it does not
leave them unguarded. Both halves of that sentence are load-bearing.

Stopping on disconnect would be its own failure mode: a network blip would
flatten positions nobody asked to close. Continuing without limits would be
worse. So the runner keeps executing the desired state it last verified, while
local enforcement keeps watching.

## What keeps working offline

- **The aggregate exposure cap.** Risk-increasing orders are refused once the
  runner's own limit is reached.
- **The fallback breaker.** Drawdown and notional thresholds are evaluated
  locally on every tick.
- **The zombie watchdog.** A deployment whose engine has been disconnected
  beyond the grace period is escalated rather than assumed healthy.
- **The signed fact outbox.** Observations accumulate durably and publish when
  connectivity returns, with identity and sequence unchanged.

Risk-*reducing* intents stay permitted throughout. A cap that blocked you from
closing a position would be a cap that traps you in one.

## Where the limits come from

The limits are not local defaults and not deployment configuration. They come
from a signed policy owned upstream, keyed by tenant, logical trading mode and
runner.

`sandbox`, `testnet` and `live` are logical modes. Nothing else can create or
override this policy — not the deployment's own `risk_config`, not a command,
not a toolkit manifest, and not a Custos default. The reconciler never reads
`risk_config` for these limits at all.

### Verification

Before a policy is accepted, Custos verifies the policy digest, the exact event
bytes, the derived subject, the event bindings, the fingerprint and the Ed25519
signature — then checks that tenant, mode and runner scope actually match this
runner.

### Revision fencing

Each immutable revision carries a unique policy id, one monotonic `revision`, a
digest, an effective time, an expiry, and an exact fence naming the previous
revision's id, revision number and digest.

Revision 1 has no prior. Every successor advances `revision` by exactly one and
binds the revision before it. Supersession is *derived* from that successor
edge rather than stored as a mutable status — there is no field an attacker
could flip to retire a policy. The only terminal statuses are `revoked` and
`expired`.

A policy that is missing, stale, conflicting, downgraded, wrong-scope,
inactive, not yet effective, expired, or invalidly signed **fails closed**.

## Surviving a restart

After verification, the policy and its verification material are stored in the
runner's existing fact database — one scoped policy head, not a second database
and not a second queue.

Restart recovery re-applies the same rules: a missing, inactive, premature or
expired policy is rejected on the way back up, exactly as it would be on the
way in. A restart is not a way to get a weaker policy accepted.

## Live has no fallback

`sandbox` and `testnet` may fall back to an explicit strictest local
configuration, so a rehearsal environment stays usable.

**Live has no fallback.** Missing, revoked or expired durable authority blocks
risk-increasing execution outright. There is no compile-time override and no
local escape hatch — because the situation where you most want to bypass the
limit is exactly the situation where it matters.

## Operational view

For thresholds, alerting and recovery steps during an actual outage, see the
[emergency playbook](/operator-guide/emergency-playbook).

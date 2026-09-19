---
title: "Safety during disconnects"
sidebar_position: 4
---

Local safety evaluation runs independently of message delivery. Losing upstream connectivity does not by itself request a position close. The two delivery lanes enforce different policy scopes.

| Lane | Limit source | Scope | Reporting |
|---|---|---|---|
| Signed | Verified, versioned runner safety policy | Tenant + logical mode + runner | Durable signed facts |
| Offline | Strict local defaults, optionally overridden by validated spec risk limits | Per deployment | Best-effort unsigned status |

## Signed policy

The signed deployment's own `risk_config` cannot override the runner aggregate cap. Policy acceptance verifies signature, exact bytes/subject, digest, scope, validity and revision fencing. Verified material and reservations are retained in the runner database.

Each successor revision advances by one and binds the preceding id/revision/digest. Missing, conflicting, revoked or expired policy fails closed. Sandbox/testnet can use an explicit strict local fallback; live has no fallback. Current live execution is also disabled independently of policy availability.

Risk-reducing orders remain distinct from new exposure. Check containment outcomes; requesting a close is not proof that the venue filled it.

## Offline guard

`risk_config` accepts `max_total_notional` and `max_drawdown_pct` as positive decimal strings or integers. Unknown keys and invalid values are rejected. Limits are per deployment and are not a signed runner-wide budget.

The guard evaluates on its own clock. It waits for engine readiness, with a bounded startup timeout, then treats unreliable valuation as a failure. A trip latches: the guard attempts flattening, stops the deployment and refuses further generations while latched. It continues trying to stop an attached engine rather than leaving the strategy running.

The latch is process-local. Restarting can clear it and retained desired state can redeploy, so inspect positions, outstanding orders and the original failure before restarting. A restart is a recovery action, not proof that risk has disappeared.

## Reporting and recovery

Signed facts survive temporary delivery failure in the durable outbox, subject to available storage. Offline status publication is best effort and is not an audit substitute. Preserve local state and logs during outages. See [emergency recovery](/operator-guide/emergency-playbook).

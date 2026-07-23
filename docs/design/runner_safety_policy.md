# Runner safety policy consumer

Crucible-rust owns the versioned aggregate runner-cap policy. Its authority key
is tenant id + logical trading mode + runner UUID. `live`, `sandbox` and
`testnet` are logical modes; the physical sim database role is not a mode.
ARX authorization, DeploymentSpec `risk_config`, deployment commands, Custos
defaults and toolkit manifests cannot create or override this owner policy.

## Exact contract boundary

Custos consumes the signed Crucible runner-safety-policy V1 producer receipt and
its exact schema/golden pins at the runtime boundary; it does not vendor a second
owner asset set. `CrucibleRunnerSafetyPolicyAuthenticator` verifies the Rust
struct-order compact JSON policy digest, exact event bytes, derived subject,
event bindings, fingerprint and Ed25519 signature before validating
tenant/mode/runner scope.
This is not key-sorted JCS. The golden signature is synthetic contract evidence
and is never accepted as runtime signature evidence.

Each immutable revision has a unique policy id, one monotonic `revision`, digest,
effective time, expiry and exact `previous` policy id/revision/digest fence.
Revision 1 has no prior. Every successor advances only `revision` by exactly one
and binds the prior revision. Supersession is derived from that successor edge;
it is not a mutable status. The only terminal statuses are `revoked` and
`expired`. Human activation/revocation events require a canonical ActorAssertion
JTI, while the deterministic system-expiry successor carries a null JTI.
Missing, stale, conflicting, downgraded, wrong-scope, inactive,
not-yet-effective, expired or invalidly signed policy fails closed.

## Durable code boundary

After signature verification, exact policy and verification material are stored
in the existing RunnerFact SQLite database. The sole first-production state
schema V1 includes one scoped policy head; it does not add a second database or
outbox. A successor must advance `revision` by exactly one and match the durable
prior fence.
Restart recovery rejects missing, inactive, premature and expired policy.

`LocalCapConfig` and `FallbackBreakerConfig` can only be built from that verified
policy or from the explicit strictest sandbox/testnet fallback. Live has no
fallback. The reconciler never reads DeploymentSpec `risk_config` for these
limits. Risk-reducing intents remain permitted by the local cap contract.

The daemon uses this resolver for sandbox, testnet and live. A current, valid,
owner-signed policy enables the same execution boundary in every mode. Live has
no compile-time override or local fallback: missing, revoked or expired durable
authority blocks risk-increasing execution.

## Current readiness

The Crucible producer handoff at `fe93008` is pinned exactly, including producer
code `d52bb16`, schema/golden digests, exact subject and signature profile. The
native engine boundary, aggregate reservation lifecycle and valid-live-policy
code path are focused verified. Physical mode migration execution, launched
NATS publication/PubAck, real daemon consumption and release promotion remain
open, so runtime, live deployment and production readiness remain false.

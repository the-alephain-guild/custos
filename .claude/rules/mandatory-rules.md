# Custos mandatory rules

## Ownership

- ARX authenticates actors and authorizes intent.
- Crucible owns all business workflows, DeploymentSpecs,
  DeploymentInstances and canonical facts.
- Custos owns local execution, safety and signed runner observations only.
- Custos must not publish or relay canonical business state through ARX.

## Runtime identity

- deployment_instance_id is the primary key for reconciler, engine, watchdog,
  breaker, credential and telemetry state.
- deployment_spec_id is immutable configuration provenance only.
- tenant and mode must be explicit and must agree across subject, envelope and
  payload.

## Trust

- Accept deployment desired state only after Crucible exact-byte and
  exact-subject signature verification.
- Live mode fails closed without signed promotion evidence.
- RunnerFacts use enrolled runner signing keys and address one exact deployment
  instance.
- No unsigned compatibility or network-trust fallback is permitted on the signed
  lane, and none in live mode under any circumstance.

### Offline lane (sandbox and testnet only)

Custos runs a second, separately named delivery lane for local strategy-logic
verification, where the operator owns the machine, the identity and the NATS
instance. It accepts unsigned desired state. It is not a fallback: it never
degrades from the signed lane, never carries canonical business state, and never
substitutes for signature verification. Its bounds are mechanical, not
conventional:

- Permitted in `sandbox` and `testnet` only. `live` is refused at the boundary by
  `src/custos/offline/mode_guard.py`, on the mode carried in the spec and on the
  mode passed at the command line, independently and before anything is parsed,
  published or written.
- Opt-in. The daemon composes it only when `arx-runner start` is given
  `--reconcile-strategy-id`; the default composition is unchanged.
- Non-promotable. Nothing it produces is promotion evidence.
- Separately contracted. It carries `OfflineDeploymentSpec` and never parses or
  emits canonical V1 command bytes.

`authority-manifest.json` `offline_lane` and `verify_offline_lane` in
`scripts/check-authority-docs.py` enforce these bounds; a lane module that is
neither routed through the guard nor declared mode-agnostic fails the gate.

## Safety

- Local stop and flatten remain available during upstream outage.
- Invalid commands are terminally rejected and audited.
- Transient engine or delivery failures remain retryable.
- No safety or audit failure may be silently swallowed.

## Repository authority

authority-manifest.json and scripts/check-authority-docs.py define the local
authority gate. Update them together with any migration, ownership or protocol
change.

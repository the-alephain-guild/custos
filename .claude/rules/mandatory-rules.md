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

### Runtime observations and replacement

- Audit sink failures must reach the host degradation mechanism. Logging alone
  does not satisfy failure handling. Callback isolation must preserve both the
  failure signal and execution of the original strategy handler. See C17.
- Prepare a complete reconciliation capture before publishing any of it. Persist
  its manifest, chunks, checkpoint and close atomically; reject different capture
  bytes under an already recorded identity. Test rollback and restart. See C18.
- CASH NAV values account balances once using trusted conversion prices. Do not
  add strategy notional to inventory already valued. Missing nonzero asset prices
  are unreliable. MARGIN equity retains its declared settlement scope. See C19.
- Independent reconciliation must align account scope, currency, quantity units
  and valuation price. Required evidence cannot be silently omitted. Cash
  inventory is separate from strategy ownership and cost basis; derivatives use
  wallet balance and common-mark valuation. Consumer validation is required for
  contract changes. See C20.
- Structural generation changes must use supported engine lifecycle operations.
  Resolve replacement materials before stopping; retain breaker state and equity
  high-water marks across replacement. Apply the new generation only after a
  successful engine operation; retries must not create duplicate nodes. See C21.
- Runner aggregate exposure belongs to the stable tenant, mode and runner scope;
  replacing the signed policy changes the active limits without abandoning open
  exposure or reservations recorded under earlier revisions. Net-position
  reductions consume every durable opening lot for that position in FIFO order,
  atomically and across restart.
- Reconciliation compares realized PnL only for a complete position cycle opened
  and flattened inside one period. Internal net PnL is compared with venue gross
  realized PnL less separately observed commission; incomplete and cross-period
  cycles do not create a falsely comparable scope.
- An executed venue fact is authoritative history. Persist the fill, exposure and
  any durable risk latch atomically; pre-trade limits may block future intent but
  must not roll back an execution that already happened. See C23.
- Auxiliary heartbeat and supervision tasks never replace the main operation's
  committed outcome. Their cancellation and exception observation are bounded;
  late transport failure cannot rewrite applied as retry-exhausted. See C24.
- A watcher revalidates durable desired generation and fingerprint immediately
  before stop, restart or quarantine. Authority drift retires the watcher without
  touching the engine. Critical supervision capability is fail-loud. See C25.
- Current test-count checks must include plans and fixes. Record fresh evidence
  in the current report; preserve historical close-outs and acceptance receipts.
  Local test success does not establish venue or production readiness. See C22.

## Repository authority

authority-manifest.json and scripts/check-authority-docs.py define the local
authority gate. Update them together with any migration, ownership or protocol
change.

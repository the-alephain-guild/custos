# Nautilus host V1

## Responsibility

`src/custos/engines/nautilus/host.py` adapts a verified Custos deployment to a
NautilusTrader `TradingNode`. It owns engine process construction, venue client
configuration, readiness observation, stop/reconfigure behavior and engine
telemetry. It does not own deployment authorization, StrategyRelease state,
artifact verification, credential scope policy or command acknowledgement.

## Sole execution chain

```text
Crucible signed DeploymentSpec command
  -> CommandIntakeCoordinator (exact bytes + durable desired state)
  -> authenticated StrategyReleaseArtifactResolverV1
  -> StrategyArtifactRuntimeV1 (BOM/attestation/member verification)
  -> immutable activation + NautilusRuntimeEntryPointLoaderV1
  -> ActivatedEngineArtifactV1
  -> EngineLifecycleSupervisor
  -> NtTradingNodeHost
  -> durable applied lifecycle + RunnerFact enqueue
  -> inbound command ACK
```

No source-path, artifact-path, registry-name, `code_hash`, `create_strategy`
factory or unsigned command branch exists.

## Engine ABI

```python
async def deploy(
    spec: dict,
    credential: dict,
    artifact: ActivatedEngineArtifactV1,
) -> str: ...
```

`spec` is the typed local view of Crucible's `execution_config`. `artifact`
contains the already-built strategy and immutable activation ID. The host adds
that strategy object to the node; it never imports strategy code itself.

## Fail-closed gates

Before engine start, `EngineLifecycleSupervisor` requires:

1. artifact runtime capability is READY;
2. signed command mode equals the local execution view;
3. the engine supports the signed connector **in the signed mode**;
4. testnet/live credentials are `trade_no_withdraw`;
5. live host capability is explicit;
6. live has signed Crucible promotion evidence;
7. the immutable production runtime receipt has enabled live execution;
8. the strategy's declared trading scope -- connector, claimed instruments and
   leverage -- equals the signed deployment's, and the signed strategy config
   carries no `trading` section. A mismatch is a terminal refusal: the instance is
   quarantined at once under `strategy_trading_scope_mismatch`,
   `strategy_trading_scope_undeclared` or `signed_strategy_config_overrides_trading`
   without spending the restart budget.

The default live enable gate is false. It becomes true only in the composition
root that consumes the final exact-image receipt; it is not a compatibility or
operator bypass flag.

## Signed opening preparation

Every newer verified desired command atomically records an instance opening gate
in the existing RunnerFact SQLite database. Running commands start at
`awaiting_release`; paused, stopped and archived commands revoke the gate. The
record binds tenant, mode, runner, instance, immutable spec id/digest, generation
and exact command fingerprint. Replaying an old command does not recreate a
missing gate, and a restart does not grant trading permission.

The canonical daemon always supplies this platform gate. A host configured with
any signed observation or boundary component refuses missing observation context
or a missing platform gate before adding the strategy to its node. Strategy
configuration cannot select an unsigned lane or synthesize a release. Independent
local and offline execution retain their own existing authority and guards; a
failed signed setup never falls back to either.

The gate is installed before strategy execution and blocks single/list submit,
modify, native close (including reduce-only and the native default), and every
other order-creating method. Existing algorithm/emulation bypass rejection stays
in force. Cancellation remains available only while the bound durable desired
identity is current; it holds a SQLite `BEGIN IMMEDIATE` control action so a
new stop/pause desired command waits for that cancellation, then fences later
actions by the old generation. Refusal recording occurs after that transaction
is released, so its signed fact can write the same outbox. Native synchronous
callbacks reenter the same thread's control transaction and revalidate their own
authority. Their refusal callbacks are queued until the outermost transaction
has committed or rolled back and closed; they are then signed before the outer
gate call returns. A different thread never inherits that connection. The daemon
composes one shared outbox; independently constructing another outbox for the same
path is not a supported callback composition.

This foundation has no `released` state, successful release command consumer,
acknowledgement producer or opening API. It intentionally keeps all signed
instances closed, including instances recovered from an older database. It does
not establish complete account inventory or a persistent signed-daemon process
fence. Existing local-runtime process fencing is unchanged. A reliable platform
process fence, stop/pause precedence, complete account inventory, exact signed
release identity and the atomic acknowledgement/outbox transaction are mandatory
before any subsequent implementation may open this gate.

Do not roll this foundation onto a trading instance. Drain and reconcile it at
the venue before a coordinated upgrade, preserving its evidence and state.
Reduce-only/native containment cannot close inherited positions while this gate
is shut; use the venue's authorized operator procedures if intervention is needed.
There is no grandfathered permission, strategy setting or command-line bypass.
Local tests establish the refusal boundary only; venue logs, restart failure
windows, exact-image acceptance and production rollout require separate evidence.

## Venue capability is per mode

`supports_venue(venue, mode)` answers from one allow-list per trading mode, not
from a single set. Listing a connector for a mode is a claim that this runner can
take it all the way to that mode, and the modes do not cost the same: sandbox and
testnet need a data feed and an execution config, while live additionally needs
signed promotion evidence, live credential handling and real-venue evidence. A
single set would make listing a connector at all a claim that it can run live.

Every listed connector resolves to a venue module that builds its NT client
configs. The allow-list is plain strings with no venue code behind it, so the two
are held against each other mechanically rather than by convention: each mode's
set must equal the union of what the venue modules declare for that mode, and
each listed pairing must actually build. A venue module refuses the modes it does
not deliver on its own, so widening the allow-list alone cannot open an execution
path.

Currently wired: Binance (spot and USDT-perpetual) in all three modes; SoDEX
(spot and perpetuals) in sandbox and testnet only.

The same connector answers three further questions, each from one place: the venue
named in signed RunnerFacts (and therefore scoping every fill event id), the
independent ledger its reconciliation evidence is read from, and the venue's cap on
a client order id. A venue that has no ledger source or no measured cap says so
rather than borrowing another venue's.

## Runtime identity and safety

`deployment_instance_id` keys active nodes, lifecycle authority, RunnerFact
contexts, watchdog state, breaker state and stop/restart operations.
`deployment_spec_id`, digest and generation are fencing provenance only.

Runner notional reservations, signed cap policy, fallback breaker and zombie
watchdog remain engine-neutral modules. They consume `ExecutionEngineProtocol`
Tier-2 methods and must be composed around the new command runtime coordinator;
they never restore the removed DeploymentReconciler path.

## Stop boundary

A signed stop runs the shutdown policy, asks the node to stop and waits for its
run to end. The account is read once after the run has ended and before the node
is disposed: nothing trades after the run ends, and disposal empties the captured
portfolio. That read, with the source time of every price it used, is the stop's
boundary valuation; a read that is unreliable, takes a price without a source
time, or receives a binary float is not one.

The read takes only what the terminal valuation carries: equity and, per open
position, quantity, mark price and currency. It leaves the cost basis out, because
NautilusTrader reports a position's opening average only as a binary float; the
periodic reads that need the average still take it. "Receives a binary float"
means an input of this read arrived as a Python `float`; it is refused, not
converted. Other inputs are unwrapped from the engine's types (`as_decimal()`,
`.value`, then their decimal string); the read does not check a closed list of
types or the canonical form of a string, which the fact wire enforces. The check
is on what crosses into the read, not on the engine's own arithmetic: on a margin
account the engine computes unrealized PnL, and in its simulated account realized
PnL, in binary floating point before rounding it to the settlement currency. For a
single position the result was observed to differ from the exactly computed value
by at most one unit of the settlement currency's smallest denomination at ordinary
sizes, and by more as notional and fill count grow (largest observed on USDT at 8
decimal places: 10 units at a notional of about 10^8 with 3 fills, 21 with 30
fills, 114 at about 10^9). These are sample maxima, not a bound, and account
equity adds the error of every position.

A run that ignores the graceful stop is cancelled. A run that outlasts the
cancellation as well may still be running, so nothing is committed: the stop is
recorded as awaiting its reap, the delivery is acknowledged, and the outcome is
committed once the run ends, or after a restart, without the shutdown policy
running again. `applied` for a stop therefore always means the node is no longer
running, and a stop ends once: applied and retry-exhausted exclude each other.

A runner that declares `settlement.terminal_valuation = "v1"` commits the stop
together with `RunnerInstanceTerminalValuationFact.v1` in one batch, after any
settlement month close it owed, and seals the instance stream; afterwards only
the instance's archive may be enqueued.

## Credential boundary

Credential material is decrypted only through
`VaultRunnerCredentialResolverV1`. The vault record must bind the signed scope
digest, and real-venue credentials must be `trade_no_withdraw`. Secrets are
used to construct the venue client, never written to state, logs, commands or
RunnerFacts.

## Import isolation

`NautilusRuntimeEntryPointLoaderV1` proves the adapter module originated under
the immutable activation root and rejects a module cached from another
activation. The entry point must implement `StrategyRuntimeAdapterV1`; there is
no class-discovery or legacy factory fallback.

## Toolkit sync discipline

The Custos toolkit packages are the sole execution authority for the v1.team
runner. Philosophers-Stone may retain its research source and the independent
legacy Crucible Python image lane, but Custos never imports either as a runtime
fallback and they are not evidence for the team release.

The crucible Docker preservation window keeps the existing
`deploy/nautilus/Dockerfile` and `deploy/hummingbot/Dockerfile.image` publication
and deployment paths available for the legacy Python product. Removing or
migrating those paths belongs to a future `crucible-runtime-migration` plan and
is not a prerequisite for v1.team.

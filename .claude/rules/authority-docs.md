# Authority documents

The machine-readable authority manifest is `authority-manifest.json`. Its
required authority snapshot is repository-local at
`docs/authority/ecosystem-authority.json`, so a standalone clone can run the
gate. Workspace authority documents are optional alignment inputs only.

Precedence:

1. ecosystem data ownership;
2. migration rollout and physical database contract;
3. ecosystem domain model;
4. ecosystem architecture;
5. accepted ADRs;
6. local Custos domain and protocol documents.

Custos owns local execution and signed observations only.
deployment_instance_id is the runtime key. ARX authorization and Crucible
business decisions must not be reimplemented in the runner.

`docs/design/strategy-toolkit.md` and
`docs/authority/strategy-contract-assets-v1.json` are authoritative for the
sole first-production Custos ArtifactRef ABI. The current type is
`StrategyArtifactRefV1` with `schema_version: 1`. There is one parser,
dataclass, schema, golden set, asset index and authority entry; predecessor
assets and runtime fallbacks are deleted. Philosophers-Stone produces the
canonical release BOM; Crucible owns StrategyRelease and artifact selection.

`custos-strategy-contract-v1-producer-receipt.json` remains
`CANONICAL_V1_PENDING_CONSUMER_RECEIPTS`. Custos produces the execution ABI;
PS and Crucible consumer receipts remain external and null until those owners
pin the exact V1 bytes. Never invent, vendor or pre-register downstream receipts.

Plan 18 T5d-B and Plan 19 T2 share one
`READY_COMMAND_CONSUMER_CONTRACT_ONLY` receipt. Crucible Plan 89 remains the sole
runner-command schema/golden producer; Custos consumes the signed V1 producer
receipt and exact pins at the boundary and exports only
`CrucibleRunnerDeploymentCommandV1` as its consumer.
The consumer retains exact signed event bytes, excludes signature bytes from the
producer fingerprint, and accepts only DeploymentSpec command material. StrategyRelease
artifact authority is resolved through the authenticated Crucible V1 resolver rather
than embedded in the command. Custos publishes no command
schema, and this STOP changes no daemon, reconciler or runtime composition.

Plan 19 T4 is `READY_DURABLE_STATE_STORE_ONLY`. Desired/applied state, exact
command bytes and receipts, outcomes, leases, activation/quarantine, local policy
references, reservations, exposure checkpoints, RunnerFact sequence and pending
PubAck all share the existing RunnerFact SQLite database and outbox. The stream
identity is tenant + mode + runner + `deployment_instance_id`; spec id, spec digest
and generation are signed fencing/provenance only. This is the first production
store contract: no spec-keyed stream, cutover table, migration API or compatibility
parser exists. The instance stream starts at sequence 1 and continues across
generation changes. This receipt
does not wire engine apply or the daemon and does not claim runtime or production
readiness; Plan 19 T5 is the next gate.

Plan 18 T5e is `PREPARED_BLOCKED_EXTERNAL_RUNTIME_RECEIPTS`. The corrected
runtime reads only the T4 durable desired command after T3 signature/intake,
independently verifies the signed runner-local policy, derives members only from
the complete PS `StrategyReleaseBomV1`, verifies the detached bundle and Crucible
acceptance evidence, quarantines before atomic activation, commits active state
before any Python import, and constructs a deep-frozen `StrategyExecutionContext`.
Historical indexes and path/hash/parameter compatibility fields do not exist.
`DevelopmentSourceRefV1` is an explicit,
non-promotable sandbox-only union member. Because no real PS bundle receipt or
Crucible C6 acceptance receipt exists yet, capability/runtime/production remain
false; tests may exercise a synthetic future capability but must not publish READY.

Plan 19 T5 is `PREPARED_BLOCKED_ARTIFACT_RUNTIME_CAPABILITY`. The sole V1 engine
protocol defines typed `EngineReadyReceipt` and
`EngineTerminalEvent`, persists restart budget in `command_in_progress_lease`
inside the same RunnerFact SQLite database, and routes ready or terminal state
through the T4 atomic lifecycle transactions. Timeout, task failure and zombie
disconnect use one bounded restart/backoff/quarantine state machine; restart
replay probes a matching ready engine before deploy. The daemon now fails when
any long-running task exits unexpectedly and shuts down in intake/deployment/
fact-flush/transport order. The team daemon is not composed while the Plan 18
T5e real artifact capability is false, and live remains false.

Plan 19 T6 is `READY_RELIABLE_PORTFOLIO_SEMANTICS_ONLY`.
`NautilusPortfolioSnapshotProvider` is the sole adapter for actual portfolio
equity, trusted marked positions and position PnL consumed by status, breaker and
RunnerFact risk observations. Missing equity or mark is explicitly unreliable and
the breaker fails closed. This receipt does not authorize a runner aggregate cap:
T7 must consume the Crucible Plan 99 signed versioned policy, DeploymentSpec
`risk_config` cannot override it, and live/runtime/production remain false.

Plan 19 T7B is `READY_CODE_ONLY_PENDING_CR99_PRODUCER_RECEIPT`. The canonical V1
focused suite passes and Custos durably enforces verified policy, reservation and
native order boundaries, but it does not vendor a second owner asset set. Authority
is tenant + logical mode + runner UUID. Until the exact Crucible producer receipt is
available and the team daemon consumes a real owner policy, policy capability,
live, runtime and production remain false.

Plan 19 T8a defines the sole `custos.runner-fact.v1` contract: the
instance-keyed, generation-non-reset schema, signed golden, exact
signing-preimage vector, five-projector capability and 13-kind parity matrix.
Runtime-log identities include the complete stream authority; lifecycle
identities use stable command/apply identity and exclude observation time. The
capability loader pins the closed projector map and unknown-kind disposition.
The synthetic signing key is golden evidence only. There is no predecessor
candidate or sequence-cutover fixture. Phase A exact-byte compatibility is
complete: producer asset commit
`7d8f3d3e1c1ba71bbc9e382a078f4a71cbe67094`, Crucible consumer code
`09a5a4b7843af2068f0bc4331a8062466a38f2ba` and Crucible receipt
`c1efcf6a9f82eed08b7931f1c7e8b02ba1020138` form the acyclic handoff,
backfilled by Custos `2adfc27`. The producer receipt is
`PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN`. Runtime RC, real runtime round trip,
engine/daemon, live/runtime and production remain false until the independent
receipts named by Plan 19 arrive.

The current contract implementation is
`packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py`.
The canonical V1 receipt binds that path and coordinated V1 bytes. No second
source, re-export shim or historical runtime authority remains.

Run make check-authority after changing ownership or protocols.

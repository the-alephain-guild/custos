# NATS contract

## Inbound desired state

Crucible directly publishes signed domain-event envelopes for both creation and
later desired-state changes:

    crucible_rust.domain.<tenant>.<mode>.deployment.
      DeploymentSpecReadyForRunner.<runner_id>.<deployment_instance_id>

    crucible_rust.domain.<tenant>.<mode>.deployment.
      DeploymentInstanceDesiredStateChanged.<runner_id>.<deployment_instance_id>

Custos uses a durable, runner-scoped JetStream consumer and manual ACK/NAK. The
verifier binds the exact subject and exact event bytes to the provisioned
Crucible Ed25519 key. Tenant, mode, runner, instance, canonical spec id and
canonical digest must agree across subject, event and payload.

Both event types carry a complete canonical DeploymentSpec plus explicit
generation and lifecycle_state. Missing values are invalid; Custos never
defaults a signed desired-state command.

## Canonical digest

`sha256-canonical-json-v1` hashes only DeploymentSpecCanonicalPayloadV1. The
command envelope and digest field are excluded. The field set is exact, object
keys are recursively sorted, arrays retain order and compact UTF-8 JSON bytes
are hashed. Cross-language golden fixtures must accompany any algorithm change.

## Outbound facts

The NATS command client has no outbound business publication API. Custos writes
typed facts to RunnerFactOutbox; the separate RunnerFact publisher signs and
publishes batches directly for Crucible ingestion.

ARX does not publish or relay deployment commands and is not a destination for
Custos business facts. Its availability is irrelevant to command delivery and
fact publication after machine authorization has been provisioned.

## Reconciliation capture and valuation

A reconciliation period is prepared completely before publication. The manifest,
chunks, valuation checkpoint and period close enter one SQLite transaction.
A failed capture consumes no sequence numbers and leaves no partial outbox rows.
An atomic group replay with different capture bytes is rejected.

`RunnerValuationCheckpointFact.v1` has two explicit valuation shapes. Derivatives
carry wallet balance, signed base quantities, entry prices and independent marks.
The consumer aligns both equity values to the same mark before comparing them.
A required checkpoint with missing data cannot close the period.

Cash checkpoints carry `cash_inventory` and an empty derivative `positions` list.
Inventory rows contain `asset`, `internal_quantity`, `venue_quantity`,
`internal_mark_price` and `common_mark_price`. Prices are in the checkpoint's
settlement currency; its own price is one. Internal inventory must reproduce
internal NAV, and venue quantities must match the bound venue balance snapshot.
The consumer compares each asset quantity and common-price NAV, then fills and
fees. Cash inventory does not attribute account holdings to strategy positions
or invent entry costs. Strategy realized PnL remains a settlement observation;
it is not compared against a fabricated cash-venue realized-PnL ledger.

The current implementation supports a single valuation currency with direct
spot conversion markets. Nonzero assets without a supported independent price
prevent completion. Custos and Crucible must deploy the coordinated cash-inventory
contract before this path is used. Historical acceptance receipts describe their
recorded commits and are not refreshed by this source change.

# NATS contract

## Inbound desired state

The authenticated control consumer uses an issued, exact tenant/runner/mode
configuration. New authorizations include these three subject families:

    crucible.runner.command.v1.<tenant_id>.<runner_id>.<trading_mode>
    crucible.runner.policy.v1.<tenant_id>.<runner_id>.<trading_mode>
    crucible.runner.opening-release.v1.<tenant_id>.<runner_id>.<trading_mode>

The consumer uses its existing runner-scoped delivery subject, manual ACK/NAK
and a single pending acknowledgement. The verifier binds the exact subject and exact event bytes to the provisioned Ed25519 key. Tenant, mode,
runner, instance, spec identity and digest must agree with the command. Creation
and later desired-state commands carry the complete spec, generation and
lifecycle state; missing values are invalid.

Transport authority remains V1, contract revision 2. Existing exact base
command/policy authorizations keep their original documents and digests.
A runner advertising opening release must have an authenticated authority that
also includes its exact opening-release filter before the daemon starts.
An old base authority or the unsigned development lane cannot enable that
capability. Obtain or rotate authority through the existing authorized workflow;
do not edit a vault or create a consumer to expand permissions locally.

An opening command does not advance deployment generation. Its transport does
not prove that the account was reconciled, that an acknowledgement was accepted,
or that trading is allowed. Those decisions retain their separate checks.

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

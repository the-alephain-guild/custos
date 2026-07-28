---
title: "Strategy Artifact Signing & Verification"
sidebar_position: 2
---


# Strategy Artifact Signing & Verification

The engine adapter is a local deep module. It hides process and framework
details from reconciliation while preserving exact deployment identity.

## Identity rule

Every lifecycle, risk, snapshot, watchdog and breaker operation is keyed by
deployment_instance_id. deployment_spec_id is immutable configuration
provenance and must not address a running process.

## Conceptual interface

    class EngineProtocol(Protocol):
        async def deploy(
            self,
            spec: RuntimeSpec,  # contains deployment_instance_id
            credential: LocalCredential,
        ) -> EngineHandle: ...

        async def reconfigure(
            self,
            spec: RuntimeSpec,  # contains deployment_instance_id
        ) -> None: ...

        async def stop(self, deployment_instance_id: UUID) -> None: ...
        async def flatten(self, deployment_instance_id: UUID) -> None: ...
        async def snapshot(self, deployment_instance_id: UUID) -> EngineSnapshot: ...
        async def wait_ready(
            self,
            authority: EngineLifecycleAuthority,
            *,
            timeout_secs: float,
        ) -> EngineReadyReceipt: ...
        async def wait_terminal(
            self,
            authority: EngineLifecycleAuthority,
        ) -> EngineTerminalEvent: ...

The concrete Nautilus adapter may use framework-specific trader or node ids,
but those ids must be deterministically derived from and mapped back to the
deployment instance. It must never collapse two instances of one spec into a
single handle.

`EngineReadyReceipt` binds the exact instance, spec, spec digest and generation.
It is valid only after the node task, data and execution connectivity, portfolio,
reconciliation, strategy lifecycle and mandatory mode capabilities are all ready.
Creating an asyncio task is not readiness. `EngineTerminalEvent` binds the same
instance/spec/generation and carries a typed reason plus retryability.

## Portfolio valuation contract

`NautilusPortfolioSnapshotProvider` is the single Nautilus valuation adapter for
engine status, marked positions, breaker inputs and RunnerFact risk rows. Equity
comes only from `portfolio.equity(venue)`. Each open position uses one trusted
cache mark, passes that exact mark object to `position.unrealized_pnl(mark_price)`,
and derives gross notional from absolute quantity times the current mark. Entry
price plus unrealized PnL is not an equity proxy.

A missing or invalid venue, equity or mark produces a typed unreliable snapshot;
it never substitutes a guessed financial value. Engine status exposes reliability
and its reason. The reconciler obtains breaker notional and equity from that one
status snapshot per tick and freezes/flattens fail closed when it is unreliable.
RunnerFact risk observations use the same provider and cannot retain a divergent
Nautilus conversion path.

## Failure contract

Adapter errors are local execution outcomes. The reconciler decides ACK or NAK
and emits a signed RunnerFact; the adapter cannot mutate ARX business
state or ask ARX to authorize a recovery action.

The lifecycle supervisor persists its bounded restart counter in the
existing RunnerFact SQLite `command_in_progress_lease`. It probes a matching
durably applied engine before deploying on redelivery, uses exponential backoff,
and commits ready or retry-exhausted/quarantine through the atomic lifecycle
transaction. It creates no database, journal or durable local queue.

Current authority status is `PREPARED_BLOCKED_ARTIFACT_RUNTIME_CAPABILITY`.
The adapter contract is implemented, but the v1.team daemon remains disabled
while no verified artifact capability is present. Live readiness is false.
Portfolio valuation is independently `READY_RELIABLE_PORTFOLIO_SEMANTICS_ONLY`;
that scoped receipt does not satisfy the signed runner-policy or runtime gates.

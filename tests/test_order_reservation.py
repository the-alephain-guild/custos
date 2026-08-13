"""Durable runner-level reservation lifecycle."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_fact import (
    OrderReservationRebuildEntry,
    RunnerFactIdentity,
    RunnerFactOutbox,
    RunnerStateAuthorityError,
    RunnerStateStore,
)

TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
POLICY_ID = UUID("20000000-0000-4000-8000-000000000001")
INSTANCE_A = UUID("30000000-0000-4000-8000-000000000001")
INSTANCE_B = UUID("30000000-0000-4000-8000-000000000002")
SPEC_ID = UUID("40000000-0000-4000-8000-000000000001")


def _store(path: Path) -> RunnerStateStore:
    outbox = RunnerFactOutbox(path)
    store = RunnerStateStore(
        outbox=outbox,
        identity=RunnerFactIdentity(Ed25519PrivateKey.generate(), "reservation-test-key"),
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        authority_resolver=lambda _verified: pytest.fail("not used"),
    )
    with outbox._connect() as connection:
        for instance_id in (INSTANCE_A, INSTANCE_B):
            connection.execute(
                """
                INSERT OR IGNORE INTO desired_deployments (
                    deployment_instance_id, tenant_id, trading_mode, runner_id,
                    deployment_spec_id, deployment_spec_digest, generation,
                    command_event_id, exact_subject, command_fingerprint,
                    verified_event_bytes_digest, signer_key_id, signature_profile,
                    verification_receipt, canonical_command, exact_event_bytes,
                    desired_status, quarantine_reason, updated_at_ns
                ) VALUES (?, ?, 'sandbox', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 1)
                """,
                (
                    str(instance_id),
                    TENANT_ID,
                    str(RUNNER_ID),
                    str(SPEC_ID),
                    "a" * 64,
                    f"command-{instance_id}",
                    f"subject-{instance_id}",
                    "b" * 64,
                    "c" * 64,
                    "command-key",
                    "test",
                    "{}",
                    "{}",
                    b"{}",
                ),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO runner_cap_policy (
                policy_id, policy_revision, policy_digest,
                tenant_scope, trading_mode, runner_id, previous_policy_id,
                previous_policy_revision, previous_policy_digest,
                settlement_currency, max_order_notional, max_notional,
                effective_at_ns, expires_at_ns, policy_status, signer_key_id,
                signature_profile, exact_subject, fingerprint,
                verified_event_bytes_digest, exact_event_bytes, signed_policy,
                policy_json, consumed_at_ns
            ) VALUES (
                ?, 1, ?, ?, 'sandbox', ?, NULL, NULL, NULL,
                'USDT', '100', '150', 1, 4102444800000000000, 'active',
                'policy-key', 'test', 'policy.subject', ?, ?, ?, ?, '{}', 1
            )
            """,
            (
                str(POLICY_ID),
                "d" * 64,
                TENANT_ID,
                str(RUNNER_ID),
                "e" * 64,
                "f" * 64,
                b"{}",
                b"{}",
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO runner_cap_policy_head (
                tenant_scope, trading_mode, runner_id, policy_id,
                policy_revision, policy_digest, updated_at_ns
            ) VALUES (?, 'sandbox', ?, ?, 1, ?, 1)
            """,
            (TENANT_ID, str(RUNNER_ID), str(POLICY_ID), "d" * 64),
        )
    return store


def test_synchronous_engine_callback_api_commits_the_reservation_lifecycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-sync-state.sqlite3")

    assert not store.has_order_reservation_sync(INSTANCE_A, "order-sync")
    reserved = store.reserve_order_notional_sync(
        event_id="reserve-sync",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-sync",
        policy_id=POLICY_ID,
        requested_notional=Decimal("25"),
    )
    filled = store.record_order_fill_sync(
        event_id="fill-sync",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-sync",
        fill_notional=Decimal("10"),
        fill_quantity=Decimal("10"),
    )
    released = store.release_order_reservation_sync(
        event_id="reject-sync",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-sync",
        reason="rejected",
    )

    assert reserved.state == "reserved"
    assert store.has_order_reservation_sync(INSTANCE_A, "order-sync")
    assert filled.filled_exposure == Decimal("10")
    assert released.state == "filled"
    assert released.reserved_notional == Decimal("0")


def test_market_fill_can_settle_above_quote_reservation_within_signed_caps(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-market-fill-state.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve-market",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-market",
        policy_id=POLICY_ID,
        requested_notional=Decimal("90"),
    )

    filled = store.record_order_fill_sync(
        event_id="fill-market",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-market",
        fill_notional=Decimal("95"),
        fill_quantity=Decimal("95"),
    )

    assert (filled.reserved_notional, filled.filled_exposure, filled.state) == (
        Decimal("0"),
        Decimal("95"),
        "filled",
    )


@pytest.mark.asyncio
async def test_profitable_full_close_releases_entry_cost_basis_by_quantity(
    tmp_path: Path,
) -> None:
    """Exit-price movement must not turn a flat venue position into a state error."""

    store = _store(tmp_path / "runner-profitable-close-state.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve-profitable-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-profitable-close",
        policy_id=POLICY_ID,
        requested_notional=Decimal("44.18442"),
    )
    filled = store.record_order_fill_sync(
        event_id="fill-profitable-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-profitable-close",
        fill_notional=Decimal("44.18442"),
        fill_quantity=Decimal("0.007"),
    )

    closed = store.record_position_reduction_sync(
        event_id="close-profitable-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-profitable-close",
        reduction_notional=Decimal("44.20206"),
        reduction_quantity=Decimal("0.007"),
    )
    exposure = await store.load_runner_exposure(POLICY_ID)

    assert filled.filled_quantity == Decimal("0.007")
    assert (closed.filled_quantity, closed.filled_exposure, closed.state) == (
        Decimal("0"),
        Decimal("0"),
        "closed",
    )
    assert exposure.open_exposure == Decimal("0")


@pytest.mark.asyncio
async def test_partial_close_releases_proportional_entry_cost_basis(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-partial-close-cost-basis.sqlite3")
    await store.reserve_order_notional(
        event_id="reserve-partial-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial-close",
        policy_id=POLICY_ID,
        requested_notional=Decimal("80"),
    )
    await store.record_order_fill(
        event_id="fill-partial-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial-close",
        fill_notional=Decimal("80"),
        fill_quantity=Decimal("8"),
    )

    reduced = await store.record_position_reduction(
        event_id="reduce-partial-close",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial-close",
        reduction_notional=Decimal("26"),
        reduction_quantity=Decimal("2"),
    )
    exposure = await store.load_runner_exposure(POLICY_ID)

    assert (reduced.filled_quantity, reduced.filled_exposure, reduced.state) == (
        Decimal("6"),
        Decimal("60"),
        "filled",
    )
    assert exposure.open_exposure == Decimal("60")


def test_multiple_partial_fills_accumulate_without_dropping_the_source_reservation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-partial-fill-state.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve-partial",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial",
        policy_id=POLICY_ID,
        requested_notional=Decimal("70"),
    )

    first = store.record_order_fill_sync(
        event_id="fill-partial-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial",
        fill_notional=Decimal("31"),
        fill_quantity=Decimal("31"),
    )
    second = store.record_order_fill_sync(
        event_id="fill-partial-2",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-partial",
        fill_notional=Decimal("39"),
        fill_quantity=Decimal("39"),
    )

    assert (first.reserved_notional, first.filled_exposure, first.state) == (
        Decimal("39"),
        Decimal("31"),
        "partially_filled",
    )
    assert (second.reserved_notional, second.filled_exposure, second.state) == (
        Decimal("0"),
        Decimal("70"),
        "filled",
    )


@pytest.mark.asyncio
async def test_reserve_is_atomic_runner_wide_idempotent_and_fail_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-state.sqlite3")

    first = await store.reserve_order_notional(
        event_id="reserve-a",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        policy_id=POLICY_ID,
        requested_notional=Decimal("90"),
    )
    replay = await store.reserve_order_notional(
        event_id="reserve-a",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        policy_id=POLICY_ID,
        requested_notional=Decimal("90"),
    )

    assert first == replay
    assert first.reserved_notional == Decimal("90")
    assert first.state == "reserved"
    with pytest.raises(RunnerStateAuthorityError, match="idempotency"):
        await store.reserve_order_notional(
            event_id="reserve-a",
            deployment_instance_id=INSTANCE_A,
            client_order_id="order-a",
            policy_id=POLICY_ID,
            requested_notional=Decimal("91"),
        )
    with pytest.raises(RunnerStateAuthorityError, match="per-order"):
        await store.reserve_order_notional(
            event_id="reserve-over-order",
            deployment_instance_id=INSTANCE_B,
            client_order_id="order-over-order",
            policy_id=POLICY_ID,
            requested_notional=Decimal("101"),
        )
    with pytest.raises(RunnerStateAuthorityError, match="runner aggregate"):
        await store.reserve_order_notional(
            event_id="reserve-over-runner",
            deployment_instance_id=INSTANCE_B,
            client_order_id="order-over-runner",
            policy_id=POLICY_ID,
            requested_notional=Decimal("61"),
        )


@pytest.mark.asyncio
async def test_fill_replace_cancel_and_close_preserve_exposure_invariants(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "runner-state.sqlite3")
    await store.reserve_order_notional(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )

    partial = await store.record_order_fill(
        event_id="fill-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        fill_notional=Decimal("40"),
        fill_quantity=Decimal("40"),
    )
    replaced = await store.replace_order_reservation(
        event_id="replace-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        new_reserved_notional=Decimal("30"),
    )
    canceled = await store.release_order_reservation(
        event_id="cancel-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        reason="canceled",
    )
    reduced = await store.record_position_reduction(
        event_id="close-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        reduction_notional=Decimal("15"),
        reduction_quantity=Decimal("15"),
    )
    closed = await store.record_position_reduction(
        event_id="close-2",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-a",
        reduction_notional=Decimal("25"),
        reduction_quantity=Decimal("25"),
    )
    exposure = await store.load_runner_exposure(POLICY_ID)

    assert (partial.reserved_notional, partial.filled_exposure, partial.state) == (
        Decimal("60"),
        Decimal("40"),
        "partially_filled",
    )
    assert (replaced.reserved_notional, replaced.filled_exposure) == (
        Decimal("30"),
        Decimal("40"),
    )
    assert (canceled.reserved_notional, canceled.state) == (Decimal("0"), "filled")
    assert (reduced.filled_exposure, reduced.state) == (Decimal("25"), "filled")
    assert (closed.filled_exposure, closed.state) == (Decimal("0"), "closed")
    assert exposure.open_exposure == Decimal("0")
    assert exposure.reserved_notional == Decimal("0")
    assert exposure.total_exposure == Decimal("0")


@pytest.mark.asyncio
async def test_restart_rebuild_reconciles_trusted_open_exposure_and_reservations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-state.sqlite3"
    store = _store(database)
    await store.reserve_order_notional(
        event_id="reserve-stale",
        deployment_instance_id=INSTANCE_A,
        client_order_id="stale-order",
        policy_id=POLICY_ID,
        requested_notional=Decimal("80"),
    )

    rebuilt = await store.rebuild_runner_exposure(
        event_id="rebuild-1",
        policy_id=POLICY_ID,
        open_exposure=Decimal("75"),
        active_reservations=(
            OrderReservationRebuildEntry(
                deployment_instance_id=INSTANCE_B,
                client_order_id="live-order",
                reserved_notional=Decimal("20"),
            ),
        ),
        source_digest="1" * 64,
    )
    replay = await _store(database).rebuild_runner_exposure(
        event_id="rebuild-1",
        policy_id=POLICY_ID,
        open_exposure=Decimal("75"),
        active_reservations=(
            OrderReservationRebuildEntry(
                deployment_instance_id=INSTANCE_B,
                client_order_id="live-order",
                reserved_notional=Decimal("20"),
            ),
        ),
        source_digest="1" * 64,
    )

    assert rebuilt == replay
    assert rebuilt.open_exposure == Decimal("75")
    assert rebuilt.reserved_notional == Decimal("20")
    assert rebuilt.total_exposure == Decimal("95")
    assert rebuilt.source_digest == "1" * 64
    stale = await store.load_order_reservation(INSTANCE_A, "stale-order")
    assert stale.state == "released"
    assert stale.reserved_notional == Decimal("0")

"""Bound budget orchestration using explicit synthetic account/cache sources."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from tests.engines.nautilus.test_local_risk_observation import (
    BTC,
    UNIT,
    quote,
    stamp,
)
from tests.engines.nautilus.test_local_risk_observation import (
    source_context as source_context,
)
from tests.test_local_reservations import local_risk as local_risk


def supervisor(context):
    blocker = context.risk.blocker
    blocker.until = 0
    blocker.digest = None
    blocker.armed = False

    def arm():
        blocker.armed = True
        blocker.until = 0

    def close(until):
        assert until == 0
        blocker.until = until

    def bind(digest, until):
        blocker.digest, blocker.until = digest, until

    blocker.require_account_observation = arm
    blocker.set_observation_valid_until_ns = close
    blocker.set_account_observation_valid_until_ns = bind
    module = importlib.import_module("custos.engines.nautilus.local_risk_supervisor")
    return module.PapertradeRiskSupervisor(source=context.source, poll_interval_ms=10)


def changed_cash(c, value):
    c.native.revision += 1
    c.native._save_revision()
    c.native.document.update(
        checkpoint_revision=c.native.revision,
        account_revision=c.native.document["account_revision"] + 1,
        account_snapshot_keccak256="0x" + "66" * 32,
        balance_raw=str(value * UNIT),
    )


def test_initial_native_gate_is_closed_until_a_flat_baseline_is_committed(source_context):
    c = source_context
    control = supervisor(c)
    assert c.risk.blocker.armed and c.risk.blocker.until == 0
    state = control.refresh()
    assert state.state == "ready"
    assert state.baseline_raw == 1000 * UNIT
    assert c.risk.blocker.until == state.ready_until_ns
    assert c.risk.blocker.digest == c.native.document["account_snapshot_keccak256"]


def test_startup_waits_for_quotes_without_inventing_a_baseline(source_context):
    c = source_context
    control = supervisor(c)
    c.quotes.clear()
    assert control.refresh().state == "uninitialized"
    assert c.risk.blocker.until == 0
    c.quotes[BTC] = quote(c)
    assert control.refresh().state == "ready"


def test_cash_loss_at_threshold_is_committed_before_cache_gap(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    changed_cash(c, 970)
    state = control.refresh()
    assert state.state == "latched"
    assert state.reason_code == "local_loss_limit_reached"
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0


def test_cash_loss_wins_over_a_malformed_mark(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    changed_cash(c, 970)
    c.cache.quote = lambda _: object()
    state = control.refresh()
    assert state.reason_code == "local_loss_limit_reached"


def test_pending_source_closes_only_increase_then_can_recover(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    c.native.revision += 1
    c.native._save_revision()
    c.native.document["pending_client_order_ids"] = ["queued"]
    assert control.refresh().state == "pending"
    assert c.risk.blocker.until == 0 and not c.risk.blocker.is_frozen()
    c.native.revision += 1
    c.native._save_revision()
    c.native.document["pending_client_order_ids"] = []
    assert control.refresh().state == "ready"


def test_silent_quote_expires_even_without_a_quote_callback(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    c.risk.clock[0] += timedelta(seconds=c.risk.manifest.data_policy.max_quote_age_seconds)
    state = control.refresh()
    assert state.state == "latched"
    assert c.risk.blocker.until == 0


def test_stop_closes_deadline_and_cannot_be_reused(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    control.close()
    assert c.risk.blocker.until == 0
    assert c.risk.blocker.is_frozen()
    with pytest.raises(RuntimeAuthorityError):
        control.refresh()


def test_original_owner_loss_closes_native_even_if_audit_is_unavailable(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    c.risk.lease.close()
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        control.refresh()
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0


def test_old_ready_observation_cannot_clear_a_loss_freeze(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    changed_cash(c, 970)
    control.refresh()
    changed_cash(c, 1000)
    c.account.last_event = stamp(c)
    assert control.refresh().state == "latched"
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0


@pytest.mark.asyncio
async def test_watchdog_detects_silence_without_a_strategy_or_quote_event(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    task = asyncio.create_task(control.run())
    await asyncio.sleep(0)
    c.risk.clock[0] += timedelta(seconds=c.risk.manifest.data_policy.max_account_age_seconds)
    for _ in range(100):
        if c.risk.blocker.is_frozen():
            break
        await asyncio.sleep(0.01)
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def gated_supervisor(c):
    from decimal import Decimal

    from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
    from custos.core.order_reservation_boundary import RunnerReservationBoundary
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate

    control = supervisor(c)
    c.port.bind_risk_supervisor(control)
    boundary = RunnerReservationBoundary(
        store=c.risk.store,
        runtime_id=c.risk.manifest.run_id,
        policy_id=c.risk.permit.permit_id,
        fallback_breaker=FallbackBreaker(FallbackBreakerConfig(Decimal("2000"), Decimal("10"))),
        trading_mode="live",
        semantics=c.semantics,
        control_guard=c.risk.lease,
        reduction_claims=c.port,
    )
    return control, RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)


def opening_order(identifier="risk-open"):
    from nautilus_trader.model import Quantity

    from tests.engines.nautilus.test_local_reduction_boundary import close_order

    order = close_order(identifier, reduce_only=False)
    order.quantity = Quantity.from_str("500")
    order.is_quote_quantity = True
    return order


def test_order_gate_refreshes_original_source_before_reserving(source_context):
    c = source_context
    control, gate = gated_supervisor(c)
    assert control.refresh().state == "ready"
    changed_cash(c, 970)
    sent = []
    assert gate.submit_order(lambda *a, **kw: sent.append(True), opening_order()) is False
    assert not sent
    with pytest.raises(RuntimeAuthorityError, match="local_order_reservation_missing"):
        c.risk.store.load_order_reservation_sync(c.risk.manifest.run_id, "risk-open")
    assert c.risk.blocker.is_frozen()


def test_order_gate_refreshes_again_after_preparing_and_compensates_only_unsent(
    source_context, monkeypatch
):
    c = source_context
    _, gate = gated_supervisor(c)
    prepared = c.port.record_prepared

    def add_pending(intent):
        prepared(intent)
        c.native.revision += 1
        c.native._save_revision()
        c.native.document["pending_client_order_ids"] = ["other-pending"]

    monkeypatch.setattr(c.port, "record_prepared", add_pending)
    sent = []
    assert gate.submit_order(lambda *a, **kw: sent.append(True), opening_order()) is False
    assert not sent
    assert c.port._dispatch.load("risk-open").state == "not_sent"
    assert (
        c.risk.store.load_order_reservation_sync(
            c.risk.manifest.run_id, "risk-open"
        ).reserved_notional
        == 0
    )
    assert not c.risk.blocker.is_frozen()


def test_current_prepared_missing_opening_does_not_block_its_own_final_refresh(source_context):
    c = source_context
    _, gate = gated_supervisor(c)
    sent = []
    assert gate.submit_order(lambda *a, **kw: sent.append(True), opening_order()) is True
    assert sent == [True]
    assert c.port._dispatch.load("risk-open").state == "dispatched"


def test_market_pending_does_not_block_targeted_full_close(source_context):
    from nautilus_trader.model import PositionId, Quantity

    from tests.engines.nautilus.test_local_reduction_boundary import close_order
    from tests.engines.nautilus.test_local_risk_observation import seed_position

    c = source_context
    control, gate = gated_supervisor(c)
    control.refresh()
    seed_position(c)
    c.quotes.clear()
    assert control.refresh().state == "pending"
    c.cache.positions_open = lambda **kwargs: list(c.positions)
    order = close_order("risk-close")
    order.quantity = Quantity.from_str("0.02")
    sent = []
    assert (
        gate.submit_order(lambda *a, **kw: sent.append(True), order, position_id=PositionId("PT-7"))
        is True
    )
    assert sent == [True]
    assert c.port._dispatch.load("risk-close").state == "dispatched"


def test_source_bound_supervisor_cannot_be_replaced_on_the_same_port(source_context):
    c = source_context
    control, _ = gated_supervisor(c)
    c.port.bind_risk_supervisor(control)
    other = supervisor(c)
    with pytest.raises(RuntimeAuthorityError):
        c.port.bind_risk_supervisor(other)


def test_startup_missing_quotes_has_a_bounded_deadline(source_context):
    c = source_context
    control = supervisor(c)
    c.quotes.clear()
    assert control.refresh().baseline_raw is None
    c.risk.clock[0] += timedelta(seconds=c.risk.manifest.data_policy.max_account_age_seconds)
    state = control.refresh()
    assert state.state == "latched" and state.baseline_raw is None
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0


def test_new_supervisor_cannot_reset_an_existing_cash_baseline(source_context):
    c = source_context
    first = supervisor(c)
    original = first.refresh()
    changed_cash(c, 990)
    c.account.last_event = stamp(c)
    replacement = supervisor(c)
    state = replacement.refresh()
    assert state.baseline_id == original.baseline_id
    assert state.baseline_raw == 1000 * UNIT
    assert state.loss == 10


def test_pending_deadline_survives_supervisor_recreation(source_context):
    c = source_context
    first = supervisor(c)
    first.refresh()
    c.native.revision += 1
    c.native._save_revision()
    c.native.document["pending_client_order_ids"] = ["pending"]
    original = first.refresh()
    second = supervisor(c)
    assert second.refresh().pending_since_ns == original.pending_since_ns
    c.risk.clock[0] += timedelta(seconds=c.risk.manifest.data_policy.max_account_age_seconds)
    changed_cash(c, 1000)
    c.native.document.update(
        pending_client_order_ids=[], source_timestamp=int(c.risk.clock[0].timestamp())
    )
    c.account.last_event = stamp(c)
    assert second.refresh().state == "latched"
    assert c.risk.blocker.until == 0


def test_clock_regression_closes_the_source_bound_native_deadline(source_context):
    c = source_context
    control = supervisor(c)
    control.refresh()
    c.risk.clock[0] -= timedelta(seconds=1)
    state = control.refresh()
    assert state.reason_code == "local_loss_clock_unavailable"
    assert c.risk.blocker.is_frozen() and c.risk.blocker.until == 0


def test_close_during_ready_publication_permanently_blocks_further_increase(source_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    c = source_context
    control = supervisor(c)
    control.refresh()
    entered, release = Event(), Event()
    original = c.risk.blocker.set_account_observation_valid_until_ns

    def paused(digest, until):
        entered.set()
        assert release.wait(5)
        original(digest, until)

    c.risk.blocker.set_account_observation_valid_until_ns = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(control.refresh)
        try:
            assert entered.wait(5)
            control.close()
        finally:
            release.set()
        future.result(timeout=5)
    assert c.risk.blocker.is_frozen()
    with pytest.raises(RuntimeAuthorityError):
        control.require_increase_ready()

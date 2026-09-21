"""A reservation is money set aside for quantity that has not traded yet.

The store used to give it back by notional -- ``reserved - fill`` -- so an order
whose whole quantity traded below the quoted price kept the difference reserved
forever. Nothing later releases it: a fully filled order never gets a cancel or a
reject, and closing the position only moves ``filled_exposure``. These tests pin
the reservation to unfilled *quantity* instead, which makes the leftover go to
zero on the last fill.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerStateAuthorityError
from tests.test_order_reservation import INSTANCE_A, POLICY_ID, _store


def test_a_whole_order_filled_below_the_quote_keeps_nothing_reserved(tmp_path: Path) -> None:
    store = _store(tmp_path / "improved.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )

    filled = store.record_order_fill_sync(
        event_id="fill",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("90"),
        fill_quantity=Decimal("1"),
        leaves_quantity=Decimal("0"),
    )

    assert filled.reserved_notional == Decimal("0")
    assert filled.filled_exposure == Decimal("90")
    assert filled.state == "filled"


def test_partial_fills_give_back_the_reservation_by_quantity(tmp_path: Path) -> None:
    store = _store(tmp_path / "scaled.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )

    # Quantity 4 quoted at 25 apiece. The first two units trade at 20.
    first = store.record_order_fill_sync(
        event_id="fill-1",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("40"),
        fill_quantity=Decimal("2"),
        leaves_quantity=Decimal("2"),
    )
    # Half the quantity is gone, so half the reservation is, regardless of price.
    assert first.reserved_notional == Decimal("50")
    assert first.filled_exposure == Decimal("40")
    assert first.state == "partially_filled"

    second = store.record_order_fill_sync(
        event_id="fill-2",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("60"),
        fill_quantity=Decimal("2"),
        leaves_quantity=Decimal("0"),
    )
    assert second.reserved_notional == Decimal("0")
    assert second.filled_exposure == Decimal("100")
    assert second.state == "filled"


def test_a_fill_that_cannot_report_leaves_keeps_reserving_and_says_so(
    tmp_path: Path,
) -> None:
    """The degraded path over-reserves -- safe for a cap, but it must not be silent."""
    store = _store(tmp_path / "unknown-leaves.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )

    with capture_logs() as events:
        filled = store.record_order_fill_sync(
            event_id="fill",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry",
            fill_notional=Decimal("90"),
            fill_quantity=Decimal("1"),
        )

    assert filled.reserved_notional == Decimal("10")
    retained = [
        event for event in events if event["event"] == "order_reservation_unfilled_quantity_unknown"
    ]
    assert retained == [
        {
            "event": "order_reservation_unfilled_quantity_unknown",
            "log_level": "warning",
            "client_order_id": "entry",
            "retained_notional": "10",
        }
    ]


@pytest.mark.asyncio
async def test_a_flat_account_gets_its_whole_cap_back(tmp_path: Path) -> None:
    """The reviewer's probe: six improved fills and closes must leave the cap intact."""
    store = _store(tmp_path / "cap-recovery.sqlite3")
    for index in range(6):
        order_id = f"entry-{index}"
        store.reserve_order_notional_sync(
            event_id=f"reserve-{index}",
            deployment_instance_id=INSTANCE_A,
            client_order_id=order_id,
            policy_id=POLICY_ID,
            requested_notional=Decimal("100"),
        )
        filled = store.record_order_fill_sync(
            event_id=f"fill-{index}",
            deployment_instance_id=INSTANCE_A,
            client_order_id=order_id,
            fill_notional=Decimal("90"),
            fill_quantity=Decimal("1"),
            position_id=f"position-{index}",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="buy",
            leaves_quantity=Decimal("0"),
        )
        assert filled.reserved_notional == Decimal("0")
        closed = store.record_position_reduction_fifo_sync(
            event_id=f"close-{index}",
            deployment_instance_id=INSTANCE_A,
            position_id=f"position-{index}",
            reduction_notional=Decimal("90"),
            reduction_quantity=Decimal("1"),
        )
        assert closed.filled_quantity == Decimal("0")
        assert closed.reserved_notional == Decimal("0")

    exposure = await store.load_runner_exposure(POLICY_ID)
    assert exposure.open_exposure == Decimal("0")
    assert exposure.reserved_notional == Decimal("0")

    # A flat account under a 150 cap must still be able to send a 100 order.
    store.reserve_order_notional_sync(
        event_id="next-reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="next",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_cancelling_the_rest_still_releases_only_what_was_unfilled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "cancel-rest.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )
    store.record_order_fill_sync(
        event_id="fill",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("20"),
        fill_quantity=Decimal("1"),
        leaves_quantity=Decimal("3"),
    )

    released = store.release_order_reservation_sync(
        event_id="cancel",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        reason="canceled",
    )

    assert released.reserved_notional == Decimal("0")
    assert released.filled_exposure == Decimal("20")
    exposure = await store.load_runner_exposure(POLICY_ID)
    assert exposure.reserved_notional == Decimal("0")
    assert exposure.open_exposure == Decimal("20")


def test_the_cap_still_refuses_an_order_that_really_is_over_it(tmp_path: Path) -> None:
    """Giving the spread back must not also give away the aggregate limit."""
    store = _store(tmp_path / "cap-guard.sqlite3")
    for index, notional in enumerate((Decimal("100"), Decimal("50"))):
        store.reserve_order_notional_sync(
            event_id=f"reserve-{index}",
            deployment_instance_id=INSTANCE_A,
            client_order_id=f"entry-{index}",
            policy_id=POLICY_ID,
            requested_notional=notional,
        )

    with pytest.raises(RunnerStateAuthorityError, match="aggregate cap"):
        store.reserve_order_notional_sync(
            event_id="reserve-over",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry-over",
            policy_id=POLICY_ID,
            requested_notional=Decimal("1"),
        )


class _EventSemantics:
    """Reports the unfilled quantity the way the Nautilus semantics does."""

    @staticmethod
    def event_is_risk_reducing(_event: object) -> bool:
        return False

    @staticmethod
    def event_exposure_source_order_id(_event: object) -> None:
        return None

    @staticmethod
    def event_position_id(_event: object) -> None:
        return None

    @staticmethod
    def event_instrument_id(_event: object) -> None:
        return None

    @staticmethod
    def event_side(_event: object) -> None:
        return None

    @staticmethod
    def fill_notional(event: object) -> Decimal:
        return Decimal(str(event.notional))  # type: ignore[attr-defined]

    @staticmethod
    def fill_quantity(event: object) -> Decimal:
        return Decimal(str(event.quantity))  # type: ignore[attr-defined]

    @staticmethod
    def fill_leaves_quantity(event: object) -> Decimal:
        return Decimal(str(event.leaves))  # type: ignore[attr-defined]


class OrderFilled:
    """The boundary dispatches on the class name, so this double has to wear it."""

    def __init__(self, *, notional: str, quantity: str, leaves: str, event_id: str) -> None:
        self.client_order_id = "entry"
        self.event_id = event_id
        self.notional = notional
        self.quantity = quantity
        self.leaves = leaves


def test_the_boundary_reports_the_unfilled_quantity_to_the_store(tmp_path: Path) -> None:
    """The store's arithmetic is only worth anything if production reaches it."""
    store = _store(tmp_path / "boundary-improvement.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )
    boundary = RunnerReservationBoundary(
        store=store,
        deployment_instance_id=INSTANCE_A,
        policy_id=POLICY_ID,
        fallback_breaker=FallbackBreaker(
            FallbackBreakerConfig(max_notional=Decimal("150"), max_drawdown_pct=Decimal("10"))
        ),
        trading_mode="sandbox",
        semantics=_EventSemantics(),
    )

    boundary.on_order_event(OrderFilled(notional="40", quantity="2", leaves="2", event_id="fill-1"))
    halfway = store.load_order_reservation_sync(INSTANCE_A, "entry")
    assert halfway.reserved_notional == Decimal("50")

    boundary.on_order_event(OrderFilled(notional="60", quantity="2", leaves="0", event_id="fill-2"))
    finished = store.load_order_reservation_sync(INSTANCE_A, "entry")

    assert finished.reserved_notional == Decimal("0")
    assert finished.filled_exposure == Decimal("100")

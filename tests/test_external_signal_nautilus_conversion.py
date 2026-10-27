"""A stored wire amount cannot silently become a different Nautilus unit."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from custos_toolkit.signals.types import SignalDirection

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_signal_inbox import StoredExternalSignal
from custos.engines.nautilus.external_signal import (
    ExternalSignalConversionError,
    to_toolkit_signal,
)
from custos.engines.nautilus.host import ExternalSignalRuntimeScope

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
SPEC_ID = UUID("20000000-0000-4000-8000-000000000001")
AUTHORITY = EngineLifecycleAuthority(
    deployment_instance_id=INSTANCE_ID,
    deployment_spec_id=SPEC_ID,
    deployment_spec_digest="a" * 64,
    generation=7,
    trading_mode="testnet",
)
SCOPE = ExternalSignalRuntimeScope(
    authority=AUTHORITY,
    allowed_instruments=frozenset({"ETH-USDT-SWAP"}),
    authorized_leverage=3,
)


def stored(
    *,
    action: str = "ENTER_LONG",
    investment_type: str = "margin",
    amount: str = "10.25",
    risk_action: str | None = None,
    generation: int = 7,
) -> StoredExternalSignal:
    return StoredExternalSignal(
        event_id="source:revision:ETH-USDT-SWAP:30:1793102400000:" + action + ":TRADE",
        source_id="source",
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=SPEC_ID,
        deployment_spec_digest="a" * 64,
        generation=generation,
        instrument="ETH-USDT-SWAP",
        bar_close_time=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        action=action,
        risk_action=risk_action,
        payload={
            "action": action,
            "instrument": "ETH-USDT-SWAP",
            "timestamp": "2026-10-27T12:00:15+00:00",
            "maxLag": 60,
            "orderType": "market",
            "investmentType": investment_type,
            "amount": amount,
            "requestedLeverage": 3,
        },
    )


def test_margin_becomes_exact_quote_notional_without_forwarding_token() -> None:
    signal = to_toolkit_signal(
        stored(), SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
    )

    assert signal.direction is SignalDirection.ENTER_LONG
    assert signal.amount == Decimal("30.75")
    assert signal.pair == "ETH-USDT"
    assert signal.timestamp == 1_793_102_415_000_000_000
    assert signal.max_lag == 60
    assert signal.signal_token is None


def test_full_position_exit_does_not_pass_percentage_as_quote_notional() -> None:
    event = stored(action="EXIT_LONG", investment_type="percentage_position", amount="100")

    signal = to_toolkit_signal(
        event, SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
    )

    assert signal.direction is SignalDirection.EXIT_LONG
    assert signal.amount is None


def test_wrong_generation_and_oversize_margin_fail_closed() -> None:
    with pytest.raises(ExternalSignalConversionError, match="runtime authority"):
        to_toolkit_signal(
            stored(generation=6), SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
        )
    with pytest.raises(ExternalSignalConversionError, match="order notional"):
        to_toolkit_signal(
            stored(amount="20"), SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
        )


def test_risk_flatten_requires_separate_containment_path() -> None:
    with pytest.raises(ExternalSignalConversionError, match="risk flatten"):
        to_toolkit_signal(
            stored(
                action="EXIT_LONG",
                investment_type="percentage_position",
                amount="100",
                risk_action="FLATTEN",
            ),
            SCOPE,
            max_order_notional=Decimal("40"),
            limit_currency="USDT",
        )


def test_entry_leverage_and_exit_percentage_cannot_change_wire_units() -> None:
    wrong_leverage = stored()
    wrong_leverage.payload["requestedLeverage"] = 4
    with pytest.raises(ExternalSignalConversionError, match="entry margin or leverage"):
        to_toolkit_signal(
            wrong_leverage, SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
        )

    partial_exit = stored(action="EXIT_LONG", investment_type="percentage_position", amount="50")
    with pytest.raises(ExternalSignalConversionError, match="full-position"):
        to_toolkit_signal(
            partial_exit, SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
        )


def test_non_decimal_margin_is_not_admitted_as_money() -> None:
    bad_margin = stored()
    bad_margin.payload["amount"] = 10.25
    with pytest.raises(ExternalSignalConversionError, match="exact decimal string"):
        to_toolkit_signal(
            bad_margin, SCOPE, max_order_notional=Decimal("40"), limit_currency="USDT"
        )


def test_order_limit_currency_must_match_instrument_quote() -> None:
    with pytest.raises(ExternalSignalConversionError, match="limit currency"):
        to_toolkit_signal(
            stored(),
            SCOPE,
            max_order_notional=Decimal("40"),
            limit_currency="USD",
        )

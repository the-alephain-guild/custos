"""One source target becomes independent, bounded subscriber directives."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from custos_toolkit.signals.types import SignalDirection

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.engines.nautilus.external_source_delivery import (
    ExternalSourceDeliveryError,
    SourceSubscriptionBinding,
    plan_target_delivery,
)
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FIRST = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000001"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)
SECOND = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000002"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002"),
    deployment_spec_digest="b" * 64,
    generation=1,
    trading_mode="testnet",
)


def source_event(*, target: str = "SHORT", risk_action: str | None = None):
    payload = {
        "schemaVersion": 1,
        "sourceId": "synthetic-supertrend",
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": 60,
        "target": target,
        "eventId": make_source_event_id(
            source_id="synthetic-supertrend",
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=BAR,
            target=target,
            risk_action=risk_action,
        ),
    }
    if risk_action:
        payload["riskAction"] = risk_action
    return parse_external_source_signal(
        payload,
        tenant_id="tenant-a",
        expected_source_id="synthetic-supertrend",
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=NOW,
    )


def binding(authority: EngineLifecycleAuthority) -> SourceSubscriptionBinding:
    return SourceSubscriptionBinding(
        tenant_id="tenant-a",
        source_id="synthetic-supertrend",
        source_symbol="ETH-USDT",
        target_pair="ETH-USDT",
        authority=authority,
    )


def scope(authority: EngineLifecycleAuthority, connector: str) -> ExternalSourceRuntimeScope:
    return ExternalSourceRuntimeScope(
        authority=authority,
        connector=connector,
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )


def test_two_venues_get_distinct_delivery_ids_without_source_sizing() -> None:
    event = source_event()
    first = plan_target_delivery(event, binding(FIRST), scope(FIRST, "okx_perpetual"), at=NOW)
    second = plan_target_delivery(
        event, binding(SECOND), scope(SECOND, "binance_perpetual"), at=NOW
    )
    assert first.delivery_id != second.delivery_id
    assert first.source_event_id == second.source_event_id == event.event_id
    assert first.tenant_id == second.tenant_id == "tenant-a"
    assert first.source_id == second.source_id == "synthetic-supertrend"
    assert first.bar_close_time == BAR
    assert first.emitted_at == BAR
    assert first.max_lag == 60
    assert first.expires_at == datetime(2026, 10, 27, 12, 1, tzinfo=UTC)
    for directive in (first, second):
        assert directive.signal is not None
        assert directive.signal.direction == SignalDirection.ENTER_SHORT
        assert directive.signal.pair == "ETH-USDT"
        assert directive.signal.amount is None
        assert directive.signal.signal_token is None
        assert directive.signal.metadata["source_event_id"] == event.event_id
        assert directive.signal.metadata["target_delivery_id"] == directive.delivery_id


def test_flat_and_risk_flat_are_containment_directives_without_entry_signal() -> None:
    normal = plan_target_delivery(
        source_event(target="FLAT"), binding(FIRST), scope(FIRST, "okx_perpetual"), at=NOW
    )
    risk = plan_target_delivery(
        source_event(target="FLAT", risk_action="FLATTEN"),
        binding(FIRST),
        scope(FIRST, "okx_perpetual"),
        at=NOW,
    )
    assert normal.signal is None and normal.risk_action is None
    assert risk.signal is None and risk.risk_action == "FLATTEN"
    assert normal.delivery_id != risk.delivery_id


def test_wrong_scope_and_expiry_are_rejected_before_queueing() -> None:
    event = source_event(target="LONG")
    with pytest.raises(ExternalSourceDeliveryError, match="authority"):
        plan_target_delivery(event, binding(FIRST), scope(SECOND, "binance_perpetual"), at=NOW)
    with pytest.raises(ExternalSourceDeliveryError, match="source"):
        plan_target_delivery(
            event,
            replace(binding(FIRST), source_id="another-source"),
            scope(FIRST, "okx_perpetual"),
            at=NOW,
        )
    with pytest.raises(ExternalSourceDeliveryError, match="pair"):
        plan_target_delivery(
            event,
            replace(binding(FIRST), target_pair="BTC-USDT"),
            scope(FIRST, "okx_perpetual"),
            at=NOW,
        )
    with pytest.raises(ExternalSourceDeliveryError, match="expired"):
        plan_target_delivery(
            event, binding(FIRST), scope(FIRST, "okx_perpetual"), at=NOW + timedelta(seconds=61)
        )


def test_fabricated_target_or_risk_shape_is_refused() -> None:
    event = source_event()
    with pytest.raises(ExternalSourceDeliveryError, match="target"):
        plan_target_delivery(
            replace(event, target="UNKNOWN"),
            binding(FIRST),
            scope(FIRST, "okx_perpetual"),
            at=NOW,
        )
    with pytest.raises(ExternalSourceDeliveryError, match="risk"):
        plan_target_delivery(
            replace(event, risk_action="FLATTEN"),
            binding(FIRST),
            scope(FIRST, "okx_perpetual"),
            at=NOW,
        )
    with pytest.raises(ExternalSourceDeliveryError, match="identity"):
        plan_target_delivery(
            replace(event, event_id="forged"),
            binding(FIRST),
            scope(FIRST, "okx_perpetual"),
            at=NOW,
        )

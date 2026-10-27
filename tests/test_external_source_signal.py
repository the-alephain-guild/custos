"""Source signals are target-neutral and cannot authorize a deployment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custos.core.external_source_signal import (
    ExternalSourceSignalError,
    make_source_event_id,
    parse_external_source_signal,
)

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR_CLOSE = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def source_payload(*, target: str = "SHORT", risk_action: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "sourceId": "synthetic-supertrend",
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": 60,
        "target": target,
    }
    if risk_action is not None:
        payload["riskAction"] = risk_action
    payload["eventId"] = make_source_event_id(
        source_id="synthetic-supertrend",
        strategy_revision="v0.5",
        symbol="ETH-USDT",
        timeframe="30",
        bar_close_time=BAR_CLOSE,
        target=target,
        risk_action=risk_action,
    )
    return payload


def parse(payload: object, *, now: datetime = NOW):
    return parse_external_source_signal(
        payload,
        tenant_id="tenant-a",
        expected_source_id="synthetic-supertrend",
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=now,
    )


def test_one_source_event_has_no_deployment_or_execution_size() -> None:
    signal = parse(source_payload())

    assert signal.tenant_id == "tenant-a"
    assert signal.target == "SHORT"
    assert signal.symbol == "ETH-USDT"
    assert signal.event_id == source_payload()["eventId"]
    assert not hasattr(signal, "deployment_instance_id")
    assert not hasattr(signal, "amount")
    assert not hasattr(signal, "signal_token")


@pytest.mark.parametrize(
    "field,value",
    [
        ("deploymentInstanceId", "10000000-0000-4000-8000-000000000001"),
        ("requestedLeverage", 5),
        ("amount", "10"),
        ("instrument", "ETH-USDT-SWAP"),
    ],
)
def test_source_rejects_target_or_order_fields(field: str, value: object) -> None:
    payload = source_payload()
    payload[field] = value
    with pytest.raises(ExternalSourceSignalError, match="fields"):
        parse(payload)


def test_bearer_token_is_accepted_as_wire_syntax_but_not_retained() -> None:
    payload = source_payload()
    payload["signalToken"] = "synthetic-dedicated-token-0123456789abcdef"
    signal = parse(payload)
    assert "synthetic-dedicated-token" not in repr(signal)
    assert not hasattr(signal, "signal_token")


@pytest.mark.parametrize(
    "target,risk_action",
    [
        ("LONG", "FLATTEN"),
        ("SHORT", "FLATTEN"),
        ("FLAT", "PAUSE"),
    ],
)
def test_risk_is_only_a_flat_target(target: str, risk_action: str) -> None:
    with pytest.raises(ExternalSourceSignalError):
        parse(source_payload(target=target, risk_action=risk_action))


def test_risk_flat_has_its_own_reproducible_identity() -> None:
    regular = parse(source_payload(target="FLAT"))
    risk = parse(source_payload(target="FLAT", risk_action="FLATTEN"))
    assert regular.event_id != risk.event_id
    assert risk.risk_action == "FLATTEN"


def test_id_conflict_and_source_mismatch_are_rejected() -> None:
    payload = source_payload()
    payload["target"] = "LONG"
    with pytest.raises(ExternalSourceSignalError, match="eventId"):
        parse(payload)
    payload = source_payload()
    payload["sourceId"] = "other-source"
    with pytest.raises(ExternalSourceSignalError, match="sourceId"):
        parse(payload)


def test_unhashable_target_is_a_contract_rejection() -> None:
    payload = source_payload()
    payload["target"] = ["LONG"]
    with pytest.raises(ExternalSourceSignalError, match="identity"):
        parse(payload)


def test_future_expired_and_misaligned_events_are_rejected() -> None:
    payload = source_payload()
    with pytest.raises(ExternalSourceSignalError, match="expired"):
        parse(payload, now=NOW + timedelta(seconds=61))
    with pytest.raises(ExternalSourceSignalError, match="future"):
        parse(payload, now=BAR_CLOSE - timedelta(seconds=1))
    payload["barCloseTime"] = "2026-10-27T12:01:00Z"
    with pytest.raises(ExternalSourceSignalError, match="aligned"):
        parse(payload)

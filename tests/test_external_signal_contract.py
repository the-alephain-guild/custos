"""Contract tests for OKX Section B and runner-local signal extensions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from custos.core.external_signal_contract import (
    ExternalSignalContractError,
    make_event_id,
    parse_custos_v1_alert,
    parse_okx_b_alert,
)

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")


def b_message(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "action": "ENTER_LONG",
        "instrument": "ETH-USDT-SWAP",
        "signalToken": "demo-token-do-not-log",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": "60",
        "orderType": "market",
        "investmentType": "margin",
        "amount": "10.25",
    }
    result.update(changes)
    return result


def custos_message(**changes: object) -> dict[str, object]:
    result = b_message()
    result.update(
        {
            "schemaVersion": 1,
            "sourceId": "independent-demo",
            "strategyRevision": "supertrend-v0.5",
            "deploymentInstanceId": str(INSTANCE_ID),
            "timeframe": "30",
            "barCloseTime": "2026-10-27T12:00:00Z",
            "requestedLeverage": 3,
        }
    )
    result["eventId"] = make_event_id(
        source_id="independent-demo",
        strategy_revision="supertrend-v0.5",
        instrument="ETH-USDT-SWAP",
        timeframe="30",
        bar_close_time=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        action=result["action"],
        risk_action=result.get("riskAction"),
    )
    result.update(changes)
    return result


def parse_custos(message: dict[str, object], leverage: int = 3):
    return parse_custos_v1_alert(
        message,
        now=NOW,
        allowed_instruments={"ETH-USDT-SWAP"},
        expected_deployment_instance_id=INSTANCE_ID,
        authorized_leverage=leverage,
    )


def test_okx_b_accepts_four_actions_and_exact_decimal_amount() -> None:
    for action in ("ENTER_LONG", "ENTER_SHORT", "EXIT_LONG", "EXIT_SHORT"):
        message = b_message(action=action)
        if action.startswith("EXIT"):
            message["investmentType"] = "percentage_position"
            message["amount"] = "100"
        parsed = parse_okx_b_alert(message, now=NOW, allowed_instruments={"ETH-USDT-SWAP"})
        assert parsed.action == action
        assert parsed.amount == Decimal(str(message["amount"]))
        assert "demo-token-do-not-log" not in repr(parsed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", None),
        ("action", "PAUSE"),
        ("instrument", "BTC-USDT-SWAP"),
        ("signalToken", ""),
        ("timestamp", "2026-10-27T12:00:00"),
        ("maxLag", 0),
        ("maxLag", 3601),
        pytest.param("maxLag", "9" * 10_000, id="oversized-max-lag"),
        ("amount", -1),
        ("amount", 0.1),
        pytest.param("amount", "9" * 10_000, id="oversized-amount"),
        ("investmentType", "percentage_position"),
    ],
)
def test_okx_b_rejects_invalid_or_ambiguous_fields(field: str, value: object) -> None:
    with pytest.raises(ExternalSignalContractError):
        parse_okx_b_alert(
            b_message(**{field: value}), now=NOW, allowed_instruments={"ETH-USDT-SWAP"}
        )


def test_okx_b_rejects_unknown_fields_and_expired_message() -> None:
    with pytest.raises(ExternalSignalContractError):
        parse_okx_b_alert(
            b_message(requestedLeverage=3), now=NOW, allowed_instruments={"ETH-USDT-SWAP"}
        )
    with pytest.raises(ExternalSignalContractError):
        parse_okx_b_alert(
            b_message(),
            now=NOW + timedelta(seconds=46),
            allowed_instruments={"ETH-USDT-SWAP"},
        )


def test_okx_b_limit_requires_offset() -> None:
    with pytest.raises(ExternalSignalContractError):
        parse_okx_b_alert(
            b_message(orderType="limit"), now=NOW, allowed_instruments={"ETH-USDT-SWAP"}
        )
    parsed = parse_okx_b_alert(
        b_message(orderType="limit", orderPriceOffset="0.5"),
        now=NOW,
        allowed_instruments={"ETH-USDT-SWAP"},
    )
    assert parsed.order_price_offset == Decimal("0.5")


def test_custos_v1_accepts_matching_entry_and_deterministic_id() -> None:
    parsed = parse_custos(custos_message())
    assert parsed.requested_leverage == 3
    assert parsed.deployment_instance_id == INSTANCE_ID
    assert parsed.event_id == custos_message()["eventId"]
    assert parsed.risk_action is None
    assert "demo-token-do-not-log" not in repr(parsed)


def test_custos_v1_rejects_wrong_leverage_for_entry_but_allows_risk_exit() -> None:
    with pytest.raises(ExternalSignalContractError):
        parse_custos(custos_message(requestedLeverage=4))

    risk = custos_message(action="EXIT_LONG", riskAction="FLATTEN", requestedLeverage=4)
    risk["investmentType"] = "percentage_position"
    risk["amount"] = "100"
    risk["eventId"] = make_event_id(
        source_id="independent-demo",
        strategy_revision="supertrend-v0.5",
        instrument="ETH-USDT-SWAP",
        timeframe="30",
        bar_close_time=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        action="EXIT_LONG",
        risk_action="FLATTEN",
    )
    parsed = parse_custos(risk)
    assert parsed.risk_action == "FLATTEN"


@pytest.mark.parametrize(
    "changes",
    [
        {"schemaVersion": 2},
        {"deploymentInstanceId": "20000000-0000-4000-8000-000000000002"},
        {"timeframe": "5"},
        {"barCloseTime": "2026-10-27T12:01:00Z"},
        {"riskAction": "PAUSE"},
        {"unknown": "field"},
        {"eventId": "forged"},
    ],
)
def test_custos_v1_rejects_invalid_extension(changes: dict[str, object]) -> None:
    with pytest.raises(ExternalSignalContractError):
        parse_custos(custos_message(**changes))


def test_event_id_differs_for_action_and_risk_reason() -> None:
    args = dict(
        source_id="independent-demo",
        strategy_revision="supertrend-v0.5",
        instrument="ETH-USDT-SWAP",
        timeframe="30",
        bar_close_time=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
    )
    assert make_event_id(**args, action="ENTER_LONG", risk_action=None) != make_event_id(
        **args, action="EXIT_LONG", risk_action="FLATTEN"
    )

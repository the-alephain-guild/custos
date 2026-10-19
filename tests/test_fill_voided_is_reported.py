"""A voided fill is reported, because nothing signed corrects it yet.

The engine can void part or all of a fill after it was signed -- a venue
correction, or reconciliation closing an order the venue considers gone. The
runner signs no correction fact for it, so the settlement and reconciliation
facts already sent keep counting the voided quantity. Until a correction fact
exists, the void must at least be visible: a structured warning for an order this
instance owns, and nothing at all for an order it does not.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from structlog.testing import capture_logs

from custos.core.runner_fact import RunnerFactEmitter, RunnerFactIdentity, RunnerFactOutbox
from custos.core.runner_fact_producer import RunnerFactEventBridge
from tests.test_strategy_signal_bridge import (
    OrderFilled,
    OrderInitialized,
    _deployment,
    _OrderEvent,
)

ORDER = "supertrend-entry-1"
INSTANCE = "20000000-0000-4000-8000-000000000001"
_REPORTED = "runner_fact_fill_voided_not_corrected"


class OrderFillVoided(_OrderEvent):
    def __init__(
        self,
        client_order_id: str = ORDER,
        *,
        trade_id: str = "trade-1",
        voided_qty: str = "0.003",
        is_reopened: bool = False,
    ) -> None:
        super().__init__(event_id="c0000000-0000-4000-8000-000000000001")
        self.values.update(
            {
                "client_order_id": client_order_id,
                "trade_id": trade_id,
                "correction_id": "correction-1",
                "voided_qty": voided_qty,
                "commission_voided": None,
                "is_reopened": is_reopened,
            }
        )


def _bridge(path: Path) -> tuple[RunnerFactEmitter, RunnerFactEventBridge]:
    emitter = RunnerFactEmitter(
        RunnerFactOutbox(path),
        RunnerFactIdentity(Ed25519PrivateKey.generate(), "fill-void-test-key"),
        lambda: None,
    )
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    bridge.bootstrap(
        SimpleNamespace(
            add_order_sink=lambda _name, _sink: None,
            add_position_sink=lambda _name, _sink: None,
        )
    )
    return emitter, bridge


@pytest.mark.asyncio
async def test_a_void_on_an_owned_order_is_reported_and_signs_nothing(tmp_path: Path) -> None:
    emitter, bridge = _bridge(tmp_path / "owned.sqlite3")
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderFilled(ORDER))
    before = len(await emitter._outbox.pending(limit=100))

    with capture_logs() as logs:
        bridge._on_order_event(OrderFillVoided())

    reported = [entry for entry in logs if entry["event"] == _REPORTED]
    assert len(reported) == 1
    entry = reported[0]
    assert entry["log_level"] == "warning"
    assert entry["deployment_instance_id"] == INSTANCE
    assert entry["client_order_id"] == ORDER
    assert entry["trade_id"] == "trade-1"
    assert entry["voided_quantity"] == "0.003"
    assert entry["is_reopened"] is False
    assert "not corrected" in entry["reason"]
    assert len(await emitter._outbox.pending(limit=100)) == before


def test_a_reopening_void_says_so(tmp_path: Path) -> None:
    _emitter, bridge = _bridge(tmp_path / "reopened.sqlite3")
    bridge._on_order_event(OrderInitialized())

    with capture_logs() as logs:
        bridge._on_order_event(OrderFillVoided(is_reopened=True))

    reported = [entry for entry in logs if entry["event"] == _REPORTED]
    assert [entry["is_reopened"] for entry in reported] == [True]


@pytest.mark.asyncio
async def test_a_void_on_an_order_this_instance_does_not_own_is_dropped(tmp_path: Path) -> None:
    emitter, bridge = _bridge(tmp_path / "foreign.sqlite3")

    with capture_logs() as logs:
        bridge._on_order_event(OrderFillVoided("someone-elses-order"))

    assert [entry for entry in logs if entry["event"] == _REPORTED] == []
    assert await emitter._outbox.pending(limit=100) == []

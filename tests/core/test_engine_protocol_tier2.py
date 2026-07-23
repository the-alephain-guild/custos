"""ExecutionEngineProtocol Tier-2 contract tests.

Both shipped hosts must still satisfy the protocol after each Tier-2 method is
added; a fake missing a Tier-2 method must fail isinstance (relaxed-double
proving the extended protocol stays a live guard, not a dead branch).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from custos.core.engine_protocol import (
    EngineStatus,
    ExecutionEngineProtocol,
    OrderSnapshot,
    PositionSnapshot,
)
from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost


def _nt_host() -> NtTradingNodeHost:
    return NtTradingNodeHost(tenant_id="t", runner_id="r")


async def test_get_open_notional_sandbox_simulation_host_zero() -> None:
    assert await SandboxSimulationHost().get_open_notional("spec-1") == Decimal("0")


async def test_get_open_notional_returns_decimal() -> None:
    noop_val = await SandboxSimulationHost().get_open_notional("spec-1")
    nt_val = await _nt_host().get_open_notional("spec-x")
    assert isinstance(noop_val, Decimal)
    assert isinstance(nt_val, Decimal)


class _MissingGetOpenNotional:
    """Full Tier-1 surface but no Tier-2 ``get_open_notional``."""

    async def deploy(self, spec: dict, credential: dict, artifact: object) -> str:
        return "cid"

    async def reconfigure(self, spec: dict) -> None: ...

    async def stop(self, spec_id: str) -> None: ...

    def supports_trading_mode(self, mode: str) -> bool:
        return False

    def supports_venue(self, venue: str) -> bool:
        return False


def test_both_hosts_still_isinstance_after_tier2() -> None:
    assert isinstance(SandboxSimulationHost(), ExecutionEngineProtocol)
    assert isinstance(_nt_host(), ExecutionEngineProtocol)
    # relaxed-double: dropping a Tier-2 method must break the isinstance check,
    # so the extended protocol is proven a live guard.
    assert not isinstance(_MissingGetOpenNotional(), ExecutionEngineProtocol)


# -- T2.1 snapshot Tier-2: get_positions / get_orders / get_engine_status --


async def test_get_positions_returns_decimal_money() -> None:
    noop_positions = await SandboxSimulationHost().get_positions("spec-1")
    assert noop_positions == []
    # A stub host has no positions; the shape contract (empty list) still holds.


async def test_get_orders_returns_decimal_money() -> None:
    noop_orders = await SandboxSimulationHost().get_orders("spec-1")
    assert noop_orders == []


async def test_get_engine_status_sandbox_simulation_host_zero_values() -> None:
    status = await SandboxSimulationHost().get_engine_status("spec-1")
    assert isinstance(status, EngineStatus)
    assert status.phase == "running"
    assert status.position_count == 0
    assert status.order_count == 0
    assert status.open_notional == Decimal("0")
    assert status.peak_equity == Decimal("0")
    assert status.current_equity == Decimal("0")
    assert status.drawdown_pct == Decimal("0")
    # Red line 0.4: every money field is Decimal.
    for field_name in ("open_notional", "peak_equity", "current_equity", "drawdown_pct"):
        assert isinstance(getattr(status, field_name), Decimal), field_name


async def test_engine_status_drawdown_is_decimal() -> None:
    status = await _nt_host().get_engine_status("unknown-spec")
    # An unknown spec collapses to zero-valued EngineStatus (host-agnostic
    # invariant) but every money field must still be Decimal end-to-end.
    assert isinstance(status.drawdown_pct, Decimal)
    assert isinstance(status.peak_equity, Decimal)
    assert isinstance(status.current_equity, Decimal)
    assert isinstance(status.open_notional, Decimal)


def test_snapshot_dataclasses_reject_float_money() -> None:
    # Money fields must reject float at construction time. Non-money fields
    # (instrument_id, side, status, epoch timestamp) accept their native types.
    with pytest.raises(TypeError):
        PositionSnapshot(
            instrument_id="BTCUSDT",
            quantity=1.0,  # float — should be Decimal
            avg_px=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            notional=Decimal("100"),
        )
    with pytest.raises(TypeError):
        OrderSnapshot(
            client_order_id="c1",
            instrument_id="BTCUSDT",
            side="BUY",
            quantity=Decimal("1"),
            price=100.0,  # float — should be Decimal
            status="ACCEPTED",
        )
    with pytest.raises(TypeError):
        EngineStatus(
            phase="running",
            position_count=0,
            order_count=0,
            open_notional=Decimal("0"),
            peak_equity=Decimal("0"),
            current_equity=Decimal("0"),
            drawdown_pct=1.5,  # float — should be Decimal
        )


class _MissingGetEngineStatus:
    """Full Tier-1 + Tier-2 minus ``get_engine_status`` — relaxed-double
    for T2.1's protocol expansion."""

    async def deploy(self, spec: dict, credential: dict, artifact: object) -> str:
        return "cid"

    async def reconfigure(self, spec: dict) -> None: ...

    async def stop(self, spec_id: str) -> None: ...

    def supports_trading_mode(self, mode: str) -> bool:
        return False

    def supports_venue(self, venue: str) -> bool:
        return False

    async def get_open_notional(self, spec_id: str) -> Decimal:
        return Decimal("0")

    async def check_engine_connected(self, spec_id: str):  # noqa: ANN201
        raise NotImplementedError

    async def flatten_positions(self, spec_id: str, reason: str) -> None: ...

    async def get_positions(self, spec_id: str) -> list:
        return []

    async def get_orders(self, spec_id: str) -> list:
        return []

    # Deliberately omit get_engine_status — the isinstance check must break.


def test_both_hosts_still_isinstance_after_snapshot_methods() -> None:
    assert isinstance(SandboxSimulationHost(), ExecutionEngineProtocol)
    assert isinstance(_nt_host(), ExecutionEngineProtocol)
    assert not isinstance(_MissingGetEngineStatus(), ExecutionEngineProtocol)

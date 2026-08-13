"""Assert deploy plumbs nautilus_config timeouts into the real TradingNodeConfig.

ps ``runner.py._create_node_config`` reads timeout_connection / timeout_reconciliation /
timeout_portfolio / timeout_disconnection + reconciliation_lookback_mins from the
strategy config and passes them to ``TradingNodeConfig`` / ``LiveExecEngineConfig``.
Custos ships the same knobs through the typed V1 execution config and passes a
verified activated strategy as a separate engine ABI input.

The assertion is done by intercepting ``TradingNode.__init__`` and inspecting the
config it receives — this drives the real host-side plumb without needing to bring
up a full sandbox lifecycle.

Venue admission is covered by the host capability and lifecycle suites; this
module is intentionally limited to Nautilus config assembly.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.live.node import TradingNode  # noqa: E402

from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from tests.fixtures.minimal_supertrend_strategy import create_strategy  # noqa: E402


def _deployment_instance_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-instance:{label}"))


def _deployment_spec_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-spec:{label}"))


def _spec(label: str = "cfg-1", **overrides: Any) -> dict:
    spec = {
        "deployment_spec_id": _deployment_spec_id(label),
        "deployment_instance_id": _deployment_instance_id(label),
        "deployment_spec_digest": "d" * 64,
        "generation": 1,
        "trading_mode": "sandbox",
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
        "credential_scope": {
            "scope_id": "c0000000-0000-4000-8000-00000000000c",
            "scope_digest": "c" * 64,
        },
    }
    spec.update(overrides)
    return spec


def _credential() -> dict:
    return {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "permission_scope": "trade_no_withdraw",
    }


def _artifact():
    return SimpleNamespace(
        activation_id="activation-cfg-1",
        strategy=create_strategy({}),
    )


async def _parked_run(self) -> None:
    await asyncio.get_running_loop().create_future()


async def _teardown(host: NtTradingNodeHost, deployment_instance_id: str) -> None:
    """Cancel the parked run task and forget the node without invoking a real
    NT shutdown — matching the pattern in test_nt_trading_node_host_integration."""
    entry = host._active_nodes.pop(deployment_instance_id, None)
    if entry is None:
        return
    _, task = entry
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class _ConfigCaptor:
    """Wrap TradingNode.__init__ so tests can inspect the assembled config
    without running a full lifecycle. Delegates back to the real __init__ so
    build() / add_*_client_factory still work. Each install() resets the
    per-test capture slot so class-level state can't bleed between tests."""

    captured: Any = None

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        cls.captured = None
        real_init = TradingNode.__init__

        def _capture(self, *, config, **kwargs):
            cls.captured = config
            real_init(self, config=config, **kwargs)

        monkeypatch.setattr(TradingNode, "__init__", _capture, raising=True)


@pytest.mark.asyncio
async def test_nautilus_config_timeouts_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec['nautilus_config'] timeout/reconciliation knobs reach TradingNodeConfig."""
    _ConfigCaptor.install(monkeypatch)
    monkeypatch.setattr(TradingNode, "run_async", _parked_run, raising=True)

    host = NtTradingNodeHost()
    spec = _spec(
        nautilus_config={
            "timeout_connection": 45.0,
            "timeout_reconciliation": 20.0,
            "timeout_portfolio": 15.0,
            "timeout_disconnection": 12.0,
            "reconciliation_lookback_mins": 720,
        },
    )
    try:
        await host.deploy(spec, _credential(), _artifact())
        cfg = _ConfigCaptor.captured
        assert cfg is not None, "TradingNodeConfig must have been assembled"
        assert cfg.timeout_connection == 45.0
        assert cfg.timeout_reconciliation == 20.0
        assert cfg.timeout_portfolio == 15.0
        assert cfg.timeout_disconnection == 12.0
        assert cfg.exec_engine.reconciliation_lookback_mins == 720
    finally:
        await _teardown(host, spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_nautilus_config_defaults_when_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No nautilus_config key => NT internal defaults, existing behaviour preserved."""
    _ConfigCaptor.install(monkeypatch)
    monkeypatch.setattr(TradingNode, "run_async", _parked_run, raising=True)

    host = NtTradingNodeHost()
    spec = _spec("cfg-def")
    try:
        await host.deploy(spec, _credential(), _artifact())
        cfg = _ConfigCaptor.captured
        # NT internal defaults (per TradingNodeConfig field defaults).
        assert cfg.timeout_connection == 60.0
        assert cfg.timeout_reconciliation == 30.0
        assert cfg.timeout_portfolio == 10.0
        assert cfg.timeout_disconnection == 10.0
        # reconciliation_lookback_mins defaults to None on LiveExecEngineConfig.
        assert cfg.exec_engine.reconciliation_lookback_mins is None
    finally:
        await _teardown(host, spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_nautilus_config_partial_dict_uses_defaults_for_missing_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial nautilus_config dict overrides only the keys it sets; the rest
    fall back to NT internal defaults, so downstream operators can bump one knob
    without having to restate the whole block."""
    _ConfigCaptor.install(monkeypatch)
    monkeypatch.setattr(TradingNode, "run_async", _parked_run, raising=True)

    host = NtTradingNodeHost()
    spec = _spec(
        "cfg-partial",
        nautilus_config={"timeout_reconciliation": 25.0},
    )
    try:
        await host.deploy(spec, _credential(), _artifact())
        cfg = _ConfigCaptor.captured
        assert cfg.timeout_reconciliation == 25.0  # overridden
        assert cfg.timeout_connection == 60.0  # NT default
        assert cfg.timeout_portfolio == 10.0  # NT default
        assert cfg.exec_engine.reconciliation_lookback_mins is None  # NT default
    finally:
        await _teardown(host, spec["deployment_instance_id"])


def test_real_venue_rejects_a_second_active_instance_on_the_same_credential_scope() -> None:
    host = NtTradingNodeHost()
    first = _spec("partition-a", trading_mode="testnet", sandbox=None)
    second = _spec("partition-b", trading_mode="testnet", sandbox=None)

    host._claim_execution_account_partition(first)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="credential scope already has an active"):
        host._claim_execution_account_partition(second)  # noqa: SLF001


def test_sandbox_instances_do_not_claim_a_real_venue_account_partition() -> None:
    host = NtTradingNodeHost()

    host._claim_execution_account_partition(_spec("sandbox-a"))  # noqa: SLF001
    host._claim_execution_account_partition(_spec("sandbox-b"))  # noqa: SLF001

    assert host._execution_account_partitions == {}  # noqa: SLF001

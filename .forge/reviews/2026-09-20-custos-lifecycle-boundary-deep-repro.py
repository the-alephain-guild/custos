"""Offline lifecycle/recovery probes; assertions describe current defects.

Engine and store failures are controlled; native bar ordering uses BacktestEngine.
No venue connection, keys, network traffic, or application source edits.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/toolkit")]

# ruff: noqa: E402 -- standalone review entry point needs checkout test fixtures.
from _strategy_harness import Harness
from custos_toolkit_nautilus.adapter.coordinators import OrderReconciler
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

from custos.core.engine_lifecycle import EngineLifecycleQuarantined
from custos.core.runner_fact import RunnerFactContractError
from custos.engines.nautilus.host import NtTradingNodeHost
from tests.test_engine_lifecycle import (
    _Artifact,
    _authority,
    _Engine,
    _ready,
    _Store,
    _supervisor,
    _verified,
)


def ready_commit_failure_leaves_running_quarantined_engine():
    async def exercise():
        class Store(_Store):
            failed = False

            async def commit_applied_and_enqueue_lifecycle(self, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("controlled ready-commit failure")
                return await super().commit_applied_and_enqueue_lifecycle(**kwargs)

        class Engine(_Engine):
            active = False

            async def deploy(self, spec, credential, artifact):
                if self.active:
                    # Match NtTradingNodeHost's existing-instance guard.
                    raise RuntimeError("instance already deployed; call stop first")
                self.active = True
                return await super().deploy(spec, credential, artifact)

            async def stop(self, instance):
                self.active = False
                await super().stop(instance)

        verified, store = _verified(), Store()
        engine = Engine([_ready(verified)])
        supervisor = _supervisor(store, engine, restart_budget=1)
        kwargs = dict(
            delivery_id="review-delivery",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )
        try:
            await supervisor.apply(**kwargs)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected injected commit failure")
        assert engine.active and engine.stop_calls == 0
        assert store.state.applied_generation is None
        try:
            await supervisor.apply(**kwargs)
        except EngineLifecycleQuarantined:
            pass
        else:
            raise AssertionError("expected deployment-conflict quarantine")
        assert store.state.desired_status == "quarantined"
        assert engine.active and engine.stop_calls == 0
        assert engine.deploy_calls == 1

    asyncio.run(exercise())
    print(
        "LB-1: one ready-commit failure leaves engine running; retry quarantines durable state without stopping it"
    )


def canceling_supervisor_restarts_healthy_engine():
    async def exercise():
        verified = _verified()
        authority = _authority(verified)
        store = _Store()
        await store.commit_applied_and_enqueue_lifecycle(
            verified=verified, engine_handle="healthy", observed_status="ready"
        )
        host = NtTradingNodeHost()
        instance = str(authority.deployment_instance_id)
        host._lifecycle_authorities[instance] = authority
        running = asyncio.create_task(asyncio.Event().wait())
        host._active_nodes[instance] = NS(task=running)
        entered = asyncio.Event()

        class Engine(_Engine):
            async def wait_terminal(self, value):
                entered.set()
                return await host.wait_terminal(value)

        engine = Engine([_ready(verified)])
        supervisor = _supervisor(store, engine)
        watcher = asyncio.create_task(
            supervisor.supervise_once(
                delivery_id="review-watcher",
                verified=verified,
                runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
                credential={},
                artifact=_Artifact(),
            )
        )
        try:
            await entered.wait()
            await asyncio.sleep(0)
            watcher.cancel()
            receipt = await asyncio.wait_for(watcher, timeout=1)
            assert receipt is not None
            assert not running.done(), "shielded node never stopped or failed"
            assert engine.stop_calls == 1 and engine.deploy_calls == 1
            assert store.state.restart_count == 1
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        # Control: a genuinely canceled node must still yield a terminal event.
        actual_node = asyncio.create_task(asyncio.Event().wait())
        host._active_nodes[instance] = NS(task=actual_node)
        legitimate_watcher = asyncio.create_task(host.wait_terminal(authority))
        await asyncio.sleep(0)
        actual_node.cancel()
        event = await legitimate_watcher
        assert event.reason_code == "engine_task_cancelled" and actual_node.cancelled()

    asyncio.run(exercise())
    print(
        "LB-2: canceling terminal watcher returns a fake node-cancel event, consumes restart budget and calls stop+deploy"
    )


def restart_uses_oldest_cached_bar():
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import AccountType, Bar, BarType, Currency, Money, OmsType, Venue
    from nautilus_trader.testkit.providers import TestInstrumentProvider

    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
    )
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    try:
        engine.add_venue(
            Venue("BINANCE"),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money.from_str("100000 USDT")],
            base_currency=Currency.from_str("USDT"),
        )
        engine.add_instrument(instrument)
        engine.add_data(
            [
                Bar(
                    bar_type,
                    instrument.make_price(price),
                    instrument.make_price(price + 1),
                    instrument.make_price(price - 1),
                    instrument.make_price(price),
                    instrument.make_qty(10),
                    n * 60 * 10**9,
                    n * 60 * 10**9,
                )
                for n, price in enumerate((101, 104), 1)
            ]
        )
        engine.run()
        actual_bars = engine.cache.bars(bar_type)
        assert [D(str(b.close)) for b in actual_bars] == [D("104"), D("101")]
        monitor = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=D(".02"),
            trailing_pct=D(".01"),
        )
        h = Harness(monitor=monitor)
        h.ctx.bar_type = bar_type
        h.ctx.instrument_id = instrument.id
        h.position.instrument_id = instrument.id
        h._contexts = {instrument.id: h.ctx}
        h.cache.bars = engine.cache.bars
        OrderReconciler(h).recover_from_existing_positions()
        assert monitor.peak_price == 101
        assert monitor.check(D("101.5")) is None
        control = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=D(".02"),
            trailing_pct=D(".01"),
        )
        control.init_position(D("100"), True, quantity=D("1"))
        control.observe(actual_bars[0].close.as_decimal())
        assert control.check(D("101.5")) is not None
    finally:
        engine.dispose()
    print(
        "LB-3: native cache returns newest104 then oldest101; recovery reads101 and misses trailing exit at101.5"
    )


def failed_fact_context_keeps_account_partition():
    import pytest

    from custos.engines.nautilus import host as host_module
    from tests.fixtures.fake_live_node import FakeLiveNode, FakeLiveNodeType
    from tests.test_nt_trading_node_host import (
        _Artifact as HostArtifact,
    )
    from tests.test_nt_trading_node_host import (
        _credential,
        _fact_host,
        _fact_spec,
        _StrategyDouble,
    )

    async def exercise():
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(host_module, "LiveNode", FakeLiveNodeType)
            host, _ = _fact_host()
            strategy = _StrategyDouble()
            strategy.config = NS(platforms=NS(nautilus=NS(bar_type="5-MINUTE")))
            artifact = HostArtifact(strategy=strategy)
            bad = _fact_spec(
                "bad-timeframe",
                "binance_perpetual",
                trading_mode="testnet",
                strategy_config={"timeframe": "1-MINUTE"},
            )
            try:
                await host.deploy(bad, _credential(), artifact)
            except RunnerFactContractError as exc:
                assert "timeframe differs" in str(exc)
            else:
                raise AssertionError("expected declared/runtime timeframe mismatch")
            node = FakeLiveNode.instances[-1]
            assert not node.disposed and not host._active_nodes
            assert bad["deployment_instance_id"] in host._execution_account_partitions
            await host.stop(bad["deployment_instance_id"])
            assert bad["deployment_instance_id"] in host._execution_account_partitions
            replacement = _fact_spec(
                "corrected-timeframe",
                "binance_perpetual",
                trading_mode="testnet",
                credential_scope=bad["credential_scope"],
                strategy_config={"timeframe": "5-MINUTE"},
            )
            try:
                await host.deploy(replacement, _credential(), artifact)
            except RuntimeError as exc:
                assert "already has an active" in str(exc)
            else:
                raise AssertionError("expected leaked account ownership to block replacement")
            # The corrected deployment can start in a fresh host with no leaked partition.
            clean, _ = _fact_host()
            await clean.deploy(replacement, _credential(), artifact)
            await clean.stop(replacement["deployment_instance_id"])

    asyncio.run(exercise())
    print(
        "LB-4: failed timeframe validation leaves account partition and undisposed node; corrected replacement is blocked even after stop"
    )


if __name__ == "__main__":
    for probe in (
        ready_commit_failure_leaves_running_quarantined_engine,
        canceling_supervisor_restarts_healthy_engine,
        restart_uses_oldest_cached_bar,
        failed_fact_context_keeps_account_partition,
    ):
        probe()

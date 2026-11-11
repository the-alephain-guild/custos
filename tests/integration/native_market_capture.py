"""Exercise the real native clients against the market fixture, without platform admission."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fixtures.binance_market_loopback import BinanceMarketLoopback, loopback_data_config


async def capture_case(payload: bytes, balances: list[str], index: int) -> dict:
    from nautilus_trader.adapters.binance import BinanceDataClientFactory
    from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
    from nautilus_trader.common import Environment
    from nautilus_trader.live import LiveNode, NodeState
    from nautilus_trader.model import ClientId, TraderId

    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    spec = {"connector": "binance", "trading_mode": "sandbox", "pairs": ["BTC-USDT"]}
    async with BinanceMarketLoopback(payload) as fixture:
        builder = LiveNode.builder(
            f"market-capture-{index}", TraderId(f"MARKET-CAPTURE-{index:03}"), Environment.SANDBOX
        )
        builder = (
            builder.with_reconciliation(False)
            .with_delay_post_stop_secs(0)
            .with_delay_shutdown_secs(0)
        )
        builder = builder.add_data_client(
            "BINANCE",
            BinanceDataClientFactory(),
            loopback_data_config(spec, fixture.http_url, fixture.websocket_url),
        )
        builder = builder.add_simulated_exec_client(
            "BINANCE",
            SandboxExecutionClientFactory(),
            build_exec_client_config_sandbox(spec, {}, balances),
        )
        node = builder.build()
        owner = node.account_capture_handle(ClientId("BINANCE"))
        source = json.loads(owner.source_json())
        handle, cache = node.handle(), node.cache
        task = asyncio.create_task(node.run_async())
        try:
            deadline = asyncio.get_running_loop().time() + 20
            while handle.state != NodeState.RUNNING:
                if fixture.violations:
                    raise AssertionError((fixture.requests, fixture.violations))
                if task.done():
                    await task
                    raise AssertionError("native node ended before reaching RUNNING")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("native clients did not become ready")
                await asyncio.sleep(0.01)
            captures = []
            for _ in range(2):
                response = json.loads(await owner.capture_json(timeout_ms=2000))
                assert response["status"] == "captured", response
                capture = response["capture"]
                assert capture["snapshot"]["source"] == source
                assert capture["snapshot"]["no_borrowing_model"] is True
                assert capture["snapshot"]["positions"] == []
                assert capture["snapshot"]["orders"] == []
                assert len(capture["frontier"]) == 9
                captures.append(capture)
            assert captures[1]["capture_sequence"] > captures[0]["capture_sequence"]
            assert captures[1]["snapshot"]["observed_at"] > captures[0]["snapshot"]["observed_at"]
            instruments = [str(instrument.id) for instrument in cache.instruments()]
            assert "BTCUSDT.BINANCE" in instruments, instruments
            assert cache.orders() == []
            observed = captures[-1]["snapshot"]["balances"]
            if balances == ["100 USDT"]:
                assert any(
                    row["asset_code"] == "USDT" and Decimal(row["total"]) == Decimal(100)
                    for row in observed
                )
            else:
                assert not any(row["asset_code"] == "USDT" for row in observed)
                assert all(Decimal(row["total"]) == 0 for row in observed)
        finally:
            handle.stop()
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
            node.dispose()
        retired = json.loads(await owner.capture_json(timeout_ms=1000))
        assert retired == {"status": "unavailable", "reason": "retired"}, retired
        assert fixture.violations == [], fixture.violations
        assert any(row["path"] == "/api/v3/exchangeInfo" for row in fixture.requests)
        assert any(row["path"] in {"/ws", "/stream"} for row in fixture.requests)
        return {
            "configured_balances": balances,
            "source": source,
            "captures": captures,
            "cached_instruments": instruments,
            "retired": retired,
            "market_requests": fixture.requests,
            "websocket_methods": fixture.websocket_methods,
            "violations": fixture.violations,
        }


async def main(args) -> None:
    if any(name.startswith("BINANCE_") for name in os.environ):
        raise RuntimeError(
            "the native market fixture requires a credential-free Binance environment"
        )
    from custos.core.native_runtime_bundle import measure_candidate_native_bundle

    identity = await measure_candidate_native_bundle(args.native_runtime_bundle)
    assert identity is not None
    payload = args.exchange_info.read_bytes()
    assert 0 < len(payload) <= 1024 * 1024
    cases = [
        await capture_case(payload, ["100 USDT"], 1),
        await capture_case(payload, ["0 BTC"], 2),
    ]
    assert (
        cases[0]["captures"][0]["node_instance_id"] != cases[1]["captures"][0]["node_instance_id"]
    )
    prefix = Path(sys.prefix).resolve()
    origins = {}
    for name in ["custos", "custos_toolkit", "nautilus_trader"]:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(prefix), (name, str(origin))
        origins[name] = str(origin)
    result = {
        "scope": "standalone_native_market_fixture",
        "platform_host_acceptance": False,
        "opening_release_qualified": False,
        "native_artifact_digest": identity.artifact_digest,
        "native_crate_version": identity.crate_version,
        "market_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "module_origins": origins,
        "cases": cases,
    }
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"scope": result["scope"], "cases_passed": len(cases)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-runtime-bundle", type=Path, required=True)
    parser.add_argument("--exchange-info", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))

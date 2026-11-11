"""The node double must release retained native factories on its owner thread."""

import gc
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory

from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox
from tests.fixtures.fake_live_node import FakeBuilder


def test_stopped_fake_node_cannot_leave_native_factory_destruction_to_worker_gc(monkeypatch):
    errors = []
    previous = sys.unraisablehook
    main_thread = threading.get_ident()

    def capture(error):
        errors.append((str(error.exc_value), threading.get_ident()))
        previous(error)

    monkeypatch.setattr(sys, "unraisablehook", capture)
    enabled = gc.isenabled()
    gc.disable()
    try:
        builder = FakeBuilder("native-lifetime", "CUSTOS-FAKE", "sandbox")
        builder.add_simulated_exec_client(
            "BINANCE",
            SandboxExecutionClientFactory(),
            build_exec_client_config_sandbox({"connector": "binance"}, {}, ["1 USDT"]),
        )
        node = builder.build()
        assert node._handle._node is node
        assert node.builder.simulated_exec_clients
        node.dispose()
        FakeBuilder.instances.clear()
        type(node).instances.clear()
        del node, builder

        def worker():
            assert threading.get_ident() != main_thread
            gc.collect()

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker).result()
        assert errors == []
    finally:
        if enabled:
            gc.enable()

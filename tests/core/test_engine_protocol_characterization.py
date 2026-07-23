"""Additive engine protocol boundary characterization."""

from __future__ import annotations

import inspect

from custos.core.engine_protocol import ExecutionEngineProtocol

_EXPECTED_METHODS = (
    "deploy",
    "reconfigure",
    "stop",
    "supports_trading_mode",
    "supports_venue",
    "get_open_notional",
    "check_engine_connected",
    "flatten_positions",
    "get_positions",
    "get_orders",
    "get_engine_status",
    "wait_ready",
    "wait_terminal",
)
_EXPECTED_SYNC_METHODS = frozenset({"supports_trading_mode", "supports_venue"})


def test_existing_execution_engine_protocol_method_set_is_frozen() -> None:
    methods = tuple(
        name
        for name, value in ExecutionEngineProtocol.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    )

    assert methods == _EXPECTED_METHODS


def test_existing_execution_engine_protocol_async_boundary_is_frozen() -> None:
    for method_name in _EXPECTED_METHODS:
        method = getattr(ExecutionEngineProtocol, method_name)
        assert inspect.iscoroutinefunction(method) is (method_name not in _EXPECTED_SYNC_METHODS)

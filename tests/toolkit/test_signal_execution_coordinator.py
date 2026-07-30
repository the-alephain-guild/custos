"""Guards for the SignalExecutionCoordinator extraction.

The bar-driven signal execution cluster (entry/exit order submission, position
management, pending-entry cancellation) lives in SignalExecutionCoordinator. The
``_process_bar`` pipeline stays on the strategy as the orchestration layer and
delegates steps 9/10 to the component. These guards lock the component API, the
delegation wiring, that the old private methods are gone (single address), and that
the per-bar pipeline + Signal-concern hook stay on the strategy.
"""

from __future__ import annotations

import inspect

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry points the strategy delegates to.
_PUBLIC_API = [
    "execute_entry_for_pair",
    "execute_exit_for_pair",
    "manage_positions_for_pair",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

    assert callable(getattr(SignalExecutionCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    assert len(_PUBLIC_API) == 3


# Component methods that are no longer on the Strategy class (the entry helper
# _cancel_pending_entry_order is a private helper inside the coordinator).
_MOVED_FROM_STRATEGY = [
    "_execute_entry_for_pair",
    "_execute_exit_for_pair",
    "_manage_positions_for_pair",
    "_cancel_pending_entry_order",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The signal execution logic must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) catches re-exposure via a
    parent/mixin too."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to SignalExecutionCoordinator "
            "(not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 4


@requires_nautilus
def test_pipeline_and_signal_hook_stay_on_strategy():
    """The per-bar pipeline orchestrator and the Signal-concern direction gate stay on
    the strategy class (orthogonal concern; the pipeline is the terminal orchestration
    layer)."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in ("_process_bar", "_is_direction_allowed"):
        assert hasattr(NautilusTradingStrategy, name), f"{name} must stay on the strategy class"


# _process_bar pipeline step -> delegated coordinator method.
_PIPELINE_DELEGATES = [
    "execute_entry_for_pair",
    "execute_exit_for_pair",
    "manage_positions_for_pair",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PIPELINE_DELEGATES)
def test_process_bar_delegates_to_signal_execution_coordinator(method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy._process_bar)
    assert f"_signal_execution_coordinator.{method}" in source, (
        f"_process_bar should delegate to self._signal_execution_coordinator.{method}"
    )


@requires_nautilus
def test_constructed_in_init_not_in_process_bar():
    """Built in __init__ (no pre-pipeline None window); never in _process_bar."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    process_bar_src = inspect.getsource(NautilusTradingStrategy._process_bar)
    assert "SignalExecutionCoordinator(self)" in init_src, (
        "SignalExecutionCoordinator must be built in __init__"
    )
    assert "SignalExecutionCoordinator(self)" not in process_bar_src, (
        "SignalExecutionCoordinator must not be built inside _process_bar"
    )


# ---------------------------------------------------------------------------
# Behavioral dispatch spy: getsource guards prove "delegates to the right component"
# but not the real call order / params. Drive the actual _process_bar pipeline against
# a stub and assert step 7/9/10 dispatch order + args -- assert real behavior, not a
# source string.
# ---------------------------------------------------------------------------


def _make_pipeline_stub(manager, signal):
    """Stub strategy for an unbound NautilusTradingStrategy._process_bar(stub, bar) run.

    The two coordinators are children of `manager` so cross-component call order is
    recorded in manager.mock_calls; pipeline gates (warmup/filter) are plain mocks
    that let the bar through to the signal-execution steps.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    ctx = SimpleNamespace(pair="BTC-USDT")

    manager.risk.check_risk_limits.return_value = True
    warmup = MagicMock()
    warmup.handle_warmup_gate.return_value = False
    filters = MagicMock()
    filters.handle_mtf_bar.return_value = False
    filters.check_global.return_value = True
    filters.check_pair.return_value = True

    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        _warmup_coordinator=warmup,
        on_pre_bar=MagicMock(),
        _filter_coordinator=filters,
        _risk_control_coordinator=manager.risk,
        _signal_execution_coordinator=manager.sig,
        calculate_signal=lambda _ctx, _bar: signal,
        log=MagicMock(),
        _event_publisher=SimpleNamespace(enabled=False),
        _is_direction_allowed=lambda _d: True,
        _equity_provider=SimpleNamespace(is_risk_equity_reliable=lambda: True),
        calculate_position_size=lambda _ctx, _sig: 100,
        on_post_bar=MagicMock(),
    )
    # Run the real entry gate (risk + filters) so order/short-circuit reflect production.
    stub._entry_gates_pass = lambda c, b, d: NautilusTradingStrategy._entry_gates_pass(
        stub, c, b, d
    )
    return stub, ctx


@requires_nautilus
def test_process_bar_entry_dispatch_order_and_args():
    from unittest.mock import MagicMock

    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    manager = MagicMock()
    signal = Signal.enter_long(price=100.0)
    stub, ctx = _make_pipeline_stub(manager, signal)
    bar = MagicMock()

    NautilusTradingStrategy._process_bar(stub, bar)

    manager.risk.check_risk_limits.assert_called_once_with(bar.ts_event)
    manager.sig.execute_entry_for_pair.assert_called_once_with(ctx, signal, 100, bar)
    manager.sig.manage_positions_for_pair.assert_called_once_with(ctx, bar)
    manager.sig.execute_exit_for_pair.assert_not_called()

    names = [c[0] for c in manager.mock_calls]
    assert (
        names.index("risk.check_risk_limits")
        < names.index("sig.execute_entry_for_pair")
        < names.index("sig.manage_positions_for_pair")
    ), "pipeline must run risk gate -> entry -> manage in order"


@requires_nautilus
def test_process_bar_exit_dispatch():
    from unittest.mock import MagicMock

    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    manager = MagicMock()
    signal = Signal.exit_long(price=100.0)
    stub, ctx = _make_pipeline_stub(manager, signal)
    bar = MagicMock()

    NautilusTradingStrategy._process_bar(stub, bar)

    manager.sig.execute_exit_for_pair.assert_called_once_with(ctx, signal, bar)
    manager.sig.execute_entry_for_pair.assert_not_called()
    manager.sig.manage_positions_for_pair.assert_called_once_with(ctx, bar)


@requires_nautilus
def test_process_bar_risk_gate_blocks_entry_but_keeps_position_management():
    """Risk gate False blocks the entry only; position management still runs so an
    open position can be de-risked/exited while the risk controller is paused."""
    from unittest.mock import MagicMock

    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    manager = MagicMock()
    signal = Signal.enter_long(price=100.0)
    stub, ctx = _make_pipeline_stub(manager, signal)
    manager.risk.check_risk_limits.return_value = False
    bar = MagicMock()

    NautilusTradingStrategy._process_bar(stub, bar)

    manager.sig.execute_entry_for_pair.assert_not_called()
    manager.sig.manage_positions_for_pair.assert_called_once_with(ctx, bar)

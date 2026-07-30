"""Guards for the WarmupCoordinator extraction.

The warmup lifecycle (manager init, historical request, per-bar gate, replay,
completion gate, historical-data callback body) lives in WarmupCoordinator. The
orchestration call sites stay on the strategy: ``on_start`` delegates init +
historical request, ``on_historical_data`` delegates the callback, ``_process_bar``
delegates the warmup gate. These guards lock the component API, the delegation
wiring, that the old private methods are gone (single address), and the new
consolidated ``handle_warmup_gate`` behavior (direct behavior tests, since it merges
logic rather than migrating 1:1).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry points the strategy delegates to.
_PUBLIC_API = [
    "init_manager",
    "request_historical_data",
    "handle_warmup_gate",
    "check_pair_warmup",
    "replay_buffered_bars",
    "maybe_mark_complete",
    "handle_historical_data",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import WarmupCoordinator

    assert callable(getattr(WarmupCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    assert len(_PUBLIC_API) == 7


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_init_warmup_manager",
    "_request_historical_data_for_pairs",
    "_replay_buffered_bars",
    "_check_pair_warmup",
    "_maybe_mark_warmup_complete",
    "_check_all_indicators_initialized",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The warmup lifecycle must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) catches re-exposure via a
    parent/mixin too."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to WarmupCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 6


@requires_nautilus
def test_hooks_and_shared_helpers_stay_on_strategy():
    """Hooks (subclass override points) and shared helpers stay on base.

    Snapshot restore (``_apply_loaded_snapshot``/``_warm_indicators_from_yaml``) lives
    in SnapshotCoordinator; ``_get_warmup_config`` stays on the strategy.
    """
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in (
        "on_warmup_complete",
        "on_indicator_update",
        "on_historical_data",
        "_get_warmup_config",
    ):
        assert hasattr(NautilusTradingStrategy, name), f"{name} must stay on the strategy class"
    # Snapshot restore helpers live in SnapshotCoordinator, not the strategy.
    for name in ("_apply_loaded_snapshot", "_warm_indicators_from_yaml"):
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to SnapshotCoordinator"
        )


# Orchestration call site -> delegated coordinator method.
_ON_START_DELEGATES = ["init_manager", "request_historical_data"]


@requires_nautilus
@pytest.mark.parametrize("method", _ON_START_DELEGATES)
def test_on_start_delegates_to_warmup_coordinator(method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_start)
    assert f"_warmup_coordinator.{method}" in source, (
        f"on_start should delegate to self._warmup_coordinator.{method}"
    )


@requires_nautilus
def test_on_historical_data_delegates():
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_historical_data)
    assert "_warmup_coordinator.handle_historical_data" in source, (
        "on_historical_data should delegate to the coordinator"
    )


@requires_nautilus
def test_process_bar_delegates_warmup_gate():
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy._process_bar)
    assert "_warmup_coordinator.handle_warmup_gate" in source, (
        "_process_bar should delegate the warmup gate to the coordinator"
    )


@requires_nautilus
def test_constructed_in_init_not_on_start():
    """Built in __init__ (no pre-on_start None window); never in on_start."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    on_start_src = inspect.getsource(NautilusTradingStrategy.on_start)
    assert "WarmupCoordinator(self)" in init_src, "WarmupCoordinator must be built in __init__"
    assert "WarmupCoordinator(self)" not in on_start_src, (
        "WarmupCoordinator must not be built in on_start"
    )


# --- handle_warmup_gate behavior (consolidated method -- needs direct tests) ---


def _gate_stub(warmup_manager):
    """Strategy stub exposing only what handle_warmup_gate reads."""
    return SimpleNamespace(
        _warmup_manager=warmup_manager,
        _warmup_complete_called=True,  # short-circuit maybe_mark_complete (not under test here)
        _contexts={},
        on_indicator_update=MagicMock(),
        log=MagicMock(),
    )


@requires_nautilus
def test_handle_warmup_gate_still_warming_buffers_and_short_circuits():
    """Pair not warmed + indicators not ready -> buffer the bar, return True."""
    from custos_toolkit_nautilus.adapter.coordinators import WarmupCoordinator

    wm = MagicMock()
    ctx = SimpleNamespace(
        warmed_up=False,
        pair="BTC-USDT",
        indicators={"i": SimpleNamespace(initialized=False)},
    )
    bar = MagicMock()

    result = WarmupCoordinator(_gate_stub(wm)).handle_warmup_gate(ctx, bar)

    assert result is True
    wm.buffer_bar.assert_called_once_with(bar)
    wm.mark_warmup_complete.assert_not_called()
    assert ctx.warmed_up is False


@requires_nautilus
def test_handle_warmup_gate_just_warmed_marks_and_replays():
    """Pair not warmed but indicators ready -> mark warmed, mark_warmup_complete,
    replay buffered bars for this pair; return False (pipeline continues)."""
    from custos_toolkit_nautilus.adapter.coordinators import WarmupCoordinator

    instrument_id = "BTCUSDT-PERP.BINANCE"
    # A buffered bar belonging to this pair so replay actually fires on_indicator_update
    # (guards the replay call, not just mark_warmup_complete -- codex LOW).
    buffered_bar = SimpleNamespace(bar_type=SimpleNamespace(instrument_id=instrument_id))
    wm = MagicMock()
    wm.peek_buffered_bars.return_value = [buffered_bar]
    ctx = SimpleNamespace(
        warmed_up=False,
        pair="BTC-USDT",
        indicators={"i": SimpleNamespace(initialized=True)},
        instrument_id=instrument_id,
    )
    stub = _gate_stub(wm)
    bar = MagicMock()

    result = WarmupCoordinator(stub).handle_warmup_gate(ctx, bar)

    assert result is False
    assert ctx.warmed_up is True
    wm.mark_warmup_complete.assert_called_once()
    wm.buffer_bar.assert_not_called()
    # replay observable side-effect: the buffered bar was replayed via the hook
    stub.on_indicator_update.assert_called_once_with(ctx, buffered_bar)


@requires_nautilus
def test_handle_warmup_gate_already_warmed_continues():
    """Pair already warmed -> skip warmup block, return False (pipeline continues)."""
    from custos_toolkit_nautilus.adapter.coordinators import WarmupCoordinator

    wm = MagicMock()
    ctx = SimpleNamespace(warmed_up=True, pair="BTC-USDT", indicators={})
    bar = MagicMock()

    result = WarmupCoordinator(_gate_stub(wm)).handle_warmup_gate(ctx, bar)

    assert result is False
    wm.buffer_bar.assert_not_called()
    wm.mark_warmup_complete.assert_not_called()

"""The contracts NautilusStrategyCore keeps, and a regression against the dead code removed.

Kept:
- the core persists and restores its global state through the framework's Actor.on_save
  and on_load, which replaced an earlier snapshot helper;

Removed: a dead template layer — the on_start / on_bar / on_stop templates, the _on_core_*
hooks, the ready-file reads and writes, _log_info and an orphaned STRATEGY_READY_FILE
constant. Every subclass overrode them, so none ever ran. This guards both halves: the

NautilusStrategyCore inherits from the engine's Strategy, a Cython class that cannot be
instantiated through object.__new__ and whose log, cache and clock cannot be assigned from
Python. So tests touching instance methods bind the unbound method onto a SimpleNamespace.
"""

from __future__ import annotations

import inspect
import types

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("msgspec")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402


def _make_stub():
    """Binds the core's pure-Python methods onto a plain object.

    Sidesteps the Cython limit: rather than constructing a real NautilusStrategyCore, its
    methods are bound onto a SimpleNamespace with MethodType.
    """
    stub = SimpleNamespace()

    stub.log = MagicMock()
    stub.cache = MagicMock()
    stub.clock = MagicMock()
    stub.clock.timestamp_ns = MagicMock(return_value=123_456_789)

    for name in ("on_save", "on_load", "_log_warning", "_log_error"):
        method = getattr(NautilusStrategyCore, name)
        setattr(stub, name, types.MethodType(method, stub))

    return stub


# =============================================================================
# The dead template is gone, and the live contracts orthogonal to it are still here
# =============================================================================

# After removal the core no longer defines these itself:
# - the on_start / on_bar / on_stop templates: all four subclasses overrode them without
#   calling super(), so they never ran, and removal falls back to the engine's Strategy
# - the _on_core_* hooks: no production implementation anywhere in the repository
# - the ready-file writers and _log_info: orphaned, their only caller being the dead template
_DEAD_SYMBOLS = (
    "on_start",
    "on_stop",
    "_on_core_start",
    "_on_core_stop",
    "_write_ready_file",
    "_remove_ready_file",
    "_log_info",
)

# The orthogonal live contracts that must survive the removal.
_RETAINED_SYMBOLS = (
    "get_indicator_history",
    "get_snapshot_state",
    "get_snapshot_indicators",
    "restore_from_snapshot",
    "on_save",
    "on_load",
    "_log_warning",
    "_log_error",
    "on_bar",
    "on_trade_tick",
    "on_quote_tick",
    "on_core_bar",
    "on_core_trade_tick",
    "on_core_quote_tick",
    "_on_bar_risk_hygiene",
    "pause",
    "resume",
    "is_paused",
)


def test_dead_template_removed():
    """The dead template layer is gone from the core's own __dict__."""
    present = [s for s in _DEAD_SYMBOLS if s in NautilusStrategyCore.__dict__]
    assert not present, f"these should have been removed from NautilusStrategyCore: {present}"


def test_retained_contract():
    """The orthogonal live contracts survive the removal."""
    missing = [s for s in _RETAINED_SYMBOLS if s not in NautilusStrategyCore.__dict__]
    assert not missing, f"a kept contract went missing: {missing}"

    assert getattr(NautilusStrategyCore.get_indicator_history, "__isabstractmethod__", False), (
        "get_indicator_history should stay an abstractmethod"
    )

    init_src = inspect.getsource(NautilusStrategyCore.__init__)
    assert "self._paused" in init_src, "__init__ should still set up the soft-pause flag"


def test_ready_file_orphan_constant_removed():
    """The orphaned STRATEGY_READY_FILE constant is gone from both modules that held it.

    The ready file really comes from the instance attribute self._ready_file, which the core
    reads from the environment in __init__, and the adapter reads the same variable directly.
    No module-level constant is needed. One copy was removed and its twin in the other module
    was missed, mistaken for a live constant while being the same unreferenced orphan. This
    """
    import custos_toolkit_nautilus.adapter.strategy_core as sc_module
    import custos_toolkit_nautilus.adapter.trading_strategy as ts_module

    assert not hasattr(sc_module, "STRATEGY_READY_FILE"), (
        "the orphaned STRATEGY_READY_FILE constant should be gone from strategy_core"
    )
    assert not hasattr(ts_module, "STRATEGY_READY_FILE"), (
        "the orphaned constant should be gone from trading_strategy too — see self._ready_file"
    )


# =============================================================================
# State persistence through the framework: on_save and on_load replaced the snapshot helper
# =============================================================================


class TestFrameworkHooks:
    def test_on_save_encodes_global_state(self):
        """on_save serialises get_snapshot_state() into dict[str, bytes]."""
        from custos_toolkit_nautilus.adapter.state_persistence import decode_snapshot

        stub = _make_stub()
        stub.get_snapshot_state = MagicMock(return_value={"prev_trend": 1})

        state = stub.on_save()

        assert isinstance(state, dict)
        assert isinstance(state["state"], bytes)
        snap = decode_snapshot(state)
        assert snap["global_state"] == {"prev_trend": 1}
        assert snap["pairs"] == {}, "Core has no per-pair contexts"

    def test_on_load_restores_global_state(self):
        """on_load deserialises and restores the global state through restore_from_snapshot."""
        from custos_toolkit_nautilus.adapter.state_persistence import (
            build_snapshot,
            encode_snapshot,
        )

        stub = _make_stub()
        stub.restore_from_snapshot = MagicMock(return_value=True)

        state = encode_snapshot(build_snapshot({}, {"prev_trend": 7}, "S", 1))
        stub.on_load(state)

        stub.restore_from_snapshot.assert_called_once_with({"state": {"prev_trend": 7}})

    def test_on_load_empty_state_is_noop(self):
        """With no prior state, on_load does not call restore_from_snapshot."""
        stub = _make_stub()
        stub.restore_from_snapshot = MagicMock()

        stub.on_load({})

        stub.restore_from_snapshot.assert_not_called()


# =============================================================================
# emergency_close: a best-effort reduce_only flatten that is also fail-safe
# =============================================================================


def _make_emergency_stub():
    """Binds emergency_close and _log_warning onto a plain object, with the flatten API mocked.

    Sidesteps the Cython limit the same way _make_stub does.
    """
    stub = SimpleNamespace()
    stub.log = MagicMock()
    stub.cache = MagicMock()
    stub.cancel_all_orders = MagicMock()
    stub.close_position = MagicMock()
    for name in ("emergency_close", "_log_warning"):
        method = getattr(NautilusStrategyCore, name)
        setattr(stub, name, types.MethodType(method, stub))
    return stub


class TestEmergencyClose:
    """emergency_close() flattens every position it can, as best it can.

    The contract: walk cache.positions_open() and, for each position, cancel_all_orders first
    so a resting reduce_only order cannot consume capacity and provoke a rejection, then
    close_position(reduce_only=True, IOC). Best-effort and fail-safe: one failure does not
    stop the rest, and the method never raises — stopping the container is the backstop.
    """

    def test_emergency_close_flattens_all(self):
        """Each open position gets cancel_all_orders then close_position(reduce_only, IOC)."""
        from nautilus_trader.model.enums import TimeInForce

        stub = _make_emergency_stub()
        pos1 = SimpleNamespace(instrument_id="BTCUSDT-PERP.BINANCE")
        pos2 = SimpleNamespace(instrument_id="ETHUSDT-PERP.BINANCE")
        stub.cache.positions_open.return_value = [pos1, pos2]

        stub.emergency_close()

        assert stub.cancel_all_orders.call_count == 2
        stub.cancel_all_orders.assert_any_call(pos1.instrument_id)
        stub.cancel_all_orders.assert_any_call(pos2.instrument_id)
        assert stub.close_position.call_count == 2
        stub.close_position.assert_any_call(pos1, reduce_only=True, time_in_force=TimeInForce.IOC)
        stub.close_position.assert_any_call(pos2, reduce_only=True, time_in_force=TimeInForce.IOC)

    def test_emergency_close_failsafe_continues_on_error(self):
        """One position failing to close stops neither the others nor the method."""
        stub = _make_emergency_stub()
        pos1 = SimpleNamespace(instrument_id="BTCUSDT-PERP.BINANCE")
        pos2 = SimpleNamespace(instrument_id="ETHUSDT-PERP.BINANCE")
        stub.cache.positions_open.return_value = [pos1, pos2]
        # The first close is rejected, which must not block the second
        stub.close_position.side_effect = [RuntimeError("ReduceOnly rejected -2022"), None]

        stub.emergency_close()  # must not raise

        assert stub.close_position.call_count == 2, "the second position must still be closed"
        stub.log.warning.assert_called()  # and the failure is recorded

    def test_emergency_close_failsafe_when_positions_open_raises(self):
        """The method survives positions_open() itself raising — the very first acquisition."""
        stub = _make_emergency_stub()
        stub.cache.positions_open.side_effect = RuntimeError("cache boom")

        stub.emergency_close()  # must not raise

        stub.cancel_all_orders.assert_not_called()
        stub.close_position.assert_not_called()
        stub.log.warning.assert_called()

    def test_emergency_close_no_positions_is_noop(self):
        """With nothing open, no flatten call is made at all."""
        stub = _make_emergency_stub()
        stub.cache.positions_open.return_value = []

        stub.emergency_close()

        stub.cancel_all_orders.assert_not_called()
        stub.close_position.assert_not_called()

    def test_emergency_close_cancel_failure_still_closes(self):
        """A failed cancel_all_orders must not skip close_position.

        Cancelling is a best-effort step that lowers the chance of a rejection. Failing it
        still has to attempt the close; it cannot become a reason to skip flattening.
        """
        stub = _make_emergency_stub()
        pos1 = SimpleNamespace(instrument_id="BTCUSDT-PERP.BINANCE")
        stub.cache.positions_open.return_value = [pos1]
        stub.cancel_all_orders.side_effect = RuntimeError("cancel failed")

        stub.emergency_close()  # must not raise

        # The cancel failed and the close was attempted anyway
        stub.close_position.assert_called_once()
        stub.log.warning.assert_called()

    def test_emergency_close_failsafe_when_iteration_raises(self):
        """positions_open() returns a lazy iterator, and iterating it may raise.

        Materialising the list sits inside the outer try, so an iteration error cannot escape
        the method — acquisition is fail-safe for its whole duration.
        """

        def _raising_iter():
            yield SimpleNamespace(instrument_id="X")
            raise RuntimeError("lazy iteration boom")

        stub = _make_emergency_stub()
        stub.cache.positions_open.return_value = _raising_iter()

        stub.emergency_close()  # must not raise

        stub.log.warning.assert_called()

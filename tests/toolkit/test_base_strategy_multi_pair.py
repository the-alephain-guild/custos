# tests/test_base_strategy_multi_pair.py
"""Tests for multi-pair properties in BaseStrategy."""

import inspect

import pytest

pytest.importorskip("msgspec")


class TestMultiPairProperties:
    """Tests for multi-pair properties."""

    def test_pairs_derived_from_config_not_cached(self):
        """The _pairs cache is gone — the pairs come from config.trading.pairs,
        which PairContextCoordinator.setup_pairs walks."""
        from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        init_src = inspect.getsource(NautilusTradingStrategy.__init__)
        assert "self._pairs" not in init_src, "the _pairs cache should be gone"
        setup_src = inspect.getsource(PairContextCoordinator.setup_pairs)
        assert "config.trading.pairs" in setup_src

    def test_has_contexts_property(self):
        """Test that strategy has _contexts property."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        source = inspect.getsource(NautilusTradingStrategy.__init__)
        assert "_contexts" in source

    def test_contexts_keyed_by_instrument_id(self):
        """_contexts is keyed by InstrumentId, so one pair on two venues cannot collide.
        Built in PairContextCoordinator.setup_pairs; looked up on the strategy."""
        from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        setup_src = inspect.getsource(PairContextCoordinator.setup_pairs)
        assert "s._contexts[ctx.instrument_id]" in setup_src, (
            "build should key on ctx.instrument_id"
        )
        assert "s._contexts[pair]" not in setup_src, "the pair string should no longer be the key"
        getctx_src = inspect.getsource(NautilusTradingStrategy._get_context_from_instrument)
        assert "self._contexts.get(" in getctx_src, "reverse lookup should be a dict.get"
        assert "for ctx in self._contexts" not in getctx_src, (
            "reverse lookup should not scan values"
        )

    def test_has_capital_allocator_property(self):
        """Test that strategy has _capital_allocator property."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        source = inspect.getsource(NautilusTradingStrategy.__init__)
        assert "_capital_allocator" in source


class TestMultiPairLifecycle:
    """Tests for multi-pair lifecycle methods."""

    def test_create_context_method_exists(self):
        """create_context moved to PairContextCoordinator (no longer on the strategy)."""
        from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(PairContextCoordinator, "create_context")
        assert not hasattr(NautilusTradingStrategy, "_create_pair_context")

    def test_derive_instrument_id_for_pair_method_exists(self):
        """Test that _derive_instrument_id_for_pair method exists."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_derive_instrument_id_for_pair")

    def test_derive_bar_type_for_pair_method_exists(self):
        """Test that _derive_bar_type_for_pair method exists."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_derive_bar_type_for_pair")

    def test_derive_bar_type_for_instrument_method_exists(self):
        """Deriving a BarType straight from an InstrumentId, for callers already holding one."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_derive_bar_type_for_instrument")

    def test_create_context_reuses_instrument_id_for_bar_type(self):
        """Once create_context has derived the instrument_id, bar_type reuses it rather
        than deriving a second one internally. Behaviour is unchanged: bar_type is still
        derive_bar_type(platforms, <that same instrument_id>) — the second derivation was
        redundant, not different."""
        from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator

        src = inspect.getsource(PairContextCoordinator.create_context)
        assert "_derive_bar_type_for_instrument(instrument_id)" in src, (
            "bar_type should reuse the instrument_id already derived"
        )
        assert "_derive_bar_type_for_pair" not in src, "should not derive the instrument_id twice"


class TestMultiPairBarHandling:
    """Tests for multi-pair bar handling."""

    def test_get_pair_from_instrument_method_exists(self):
        """Test _get_pair_from_instrument method exists."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_get_pair_from_instrument")

    def test_get_context_method_exists(self):
        """Test _get_context method exists for retrieving pair context."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_get_context")


class TestMultiPairHooks:
    """Tests for multi-pair hook methods.

    The per-bar subclass hooks take ctx: PairContext rather than pair: str, and ctx
    carries both .pair and .instrument_id. Subclasses build Signals from ctx.pair and
    address instruments through ctx.instrument_id.
    """

    def test_on_pre_bar_takes_ctx_not_pair(self):
        """on_pre_bar takes ctx as its first argument, not a pair string."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(inspect.signature(NautilusTradingStrategy.on_pre_bar).parameters.keys())
        assert "ctx" in params and "pair" not in params

    def test_on_post_bar_takes_ctx_not_pair(self):
        """on_post_bar takes ctx as its first argument, not a pair string."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(inspect.signature(NautilusTradingStrategy.on_post_bar).parameters.keys())
        assert "ctx" in params and "pair" not in params

    def test_calculate_signal_takes_ctx_not_pair(self):
        """calculate_signal takes ctx as its first argument, not a pair string."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(inspect.signature(NautilusTradingStrategy.calculate_signal).parameters.keys())
        assert "ctx" in params and "pair" not in params

    def test_on_trade_closed_takes_ctx_not_pair(self):
        """on_trade_closed takes ctx, not a pair string."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(inspect.signature(NautilusTradingStrategy.on_trade_closed).parameters.keys())
        assert "ctx" in params and "pair" not in params

    def test_calculate_position_size_takes_ctx(self):
        """calculate_position_size takes ctx and signal, so nothing re-derives the context."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(
            inspect.signature(NautilusTradingStrategy.calculate_position_size).parameters.keys()
        )
        assert "ctx" in params and "signal" in params

    def test_on_indicator_update_drops_redundant_pair(self):
        """on_indicator_update drops the pair that ctx already carries: it takes ctx and bar."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        params = list(
            inspect.signature(NautilusTradingStrategy.on_indicator_update).parameters.keys()
        )
        assert "ctx" in params and "pair" not in params

    def test_process_bar_passes_ctx_to_position_size(self):
        """The pipeline sizes against the same ctx: _process_bar passes it into
        calculate_position_size, so sizing no longer re-derives one from signal.pair."""
        from custos_toolkit_nautilus.adapter.coordinators import SizingCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        proc_src = inspect.getsource(NautilusTradingStrategy._process_bar)
        assert "self.calculate_position_size(ctx, signal)" in proc_src, (
            "_process_bar should hand the ctx it already has to calculate_position_size"
        )
        for hook in ("default_position_size", "_fixed_risk_position_size"):
            sig_params = list(inspect.signature(getattr(SizingCoordinator, hook)).parameters)
            assert "ctx" in sig_params, (
                f"{hook} should take ctx, so nothing re-derives it from signal.pair"
            )
        sizing_src = inspect.getsource(SizingCoordinator._fixed_risk_position_size)
        assert "_get_context(signal.pair)" not in sizing_src, (
            "the sizing path should not re-derive a context from signal.pair"
        )


class TestMultiPairSignalProcessing:
    """Tests for multi-pair signal processing with capital allocation."""

    def test_init_capital_allocator_method_exists(self):
        """Test _init_capital_allocator method exists."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert hasattr(NautilusTradingStrategy, "_init_capital_allocator")

    def test_init_capital_allocator_reuses_context_instrument_id(self):
        """Registering the pairs reuses the ``ctx.instrument_id`` each context already
        carries, rather than deriving one again from the config string. ``on_start``
        builds ``self._contexts`` first, and every ctx in it already holds one.

        ``self._contexts`` is inserted in ``config.trading.pairs`` order and dicts keep
        insertion order, so walking ``values()`` visits them in the same order the config
        """
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        src = inspect.getsource(NautilusTradingStrategy._init_capital_allocator)
        assert "self._contexts.values()" in src, (
            "should walk the built contexts, not config strings"
        )
        assert "ctx.instrument_id" in src, "should reuse the instrument_id the context derived"
        assert "_derive_instrument_id_for_pair" not in src, (
            "should not derive the instrument_id again"
        )

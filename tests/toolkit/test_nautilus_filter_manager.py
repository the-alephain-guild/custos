"""The filter manager's own coverage, held in the repository that owns the toolkit.

These assertions were written and lived in philosophers-stone, where they guarded a
copy of this code that philosophers-stone could edit and this repository could not.
That arrangement let this repository change the filter manager with a green suite.

The subject is reached by import, never by path. A filter built from the wrong
package satisfies every behavioural assertion here, which is why the two module
origin checks exist and why the path form was a real defect rather than a style
preference.
"""

from decimal import Decimal

import pytest

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit_nautilus.adapter import filter_manager as _fm_module  # noqa: E402

SubscriptionRequest = _fm_module.SubscriptionRequest
FilterResult = _fm_module.FilterResult


class TestSubscriptionRequest:
    """Tests for SubscriptionRequest."""

    def test_bar_subscription(self):
        """Test bar subscription request."""
        req = SubscriptionRequest(
            type="bars",
            bar_type="BTC-USDT.BINANCE-1-HOUR-LAST-EXTERNAL",
        )
        assert req.type == "bars"
        assert req.bar_type == "BTC-USDT.BINANCE-1-HOUR-LAST-EXTERNAL"

    def test_tick_subscription(self):
        """Test tick subscription request."""
        req = SubscriptionRequest(
            type="ticks",
            instrument_id="BTC-USDT.BINANCE",
        )
        assert req.type == "ticks"
        assert req.instrument_id == "BTC-USDT.BINANCE"

    def test_defaults(self):
        """Test default values."""
        req = SubscriptionRequest(type="bars")
        assert req.bar_type is None
        assert req.instrument_id is None

    def test_all_fields(self):
        """Test with all fields specified."""
        req = SubscriptionRequest(
            type="bars",
            bar_type="ETH-USDT.BINANCE-5-MINUTE-LAST-EXTERNAL",
            instrument_id="ETH-USDT.BINANCE",
        )
        assert req.type == "bars"
        assert req.bar_type == "ETH-USDT.BINANCE-5-MINUTE-LAST-EXTERNAL"
        assert req.instrument_id == "ETH-USDT.BINANCE"


class TestFilterResult:
    """Tests for FilterResult."""

    def test_passed_result(self):
        """Test passed filter result."""
        result = FilterResult(
            passed=True,
            failed_filters=[],
            passed_filters=["time", "volatility"],
        )
        assert result.passed is True
        assert len(result.passed_filters) == 2
        assert "time" in result.passed_filters
        assert "volatility" in result.passed_filters

    def test_failed_result(self):
        """Test failed filter result."""
        result = FilterResult(
            passed=False,
            failed_filters=["adx"],
            passed_filters=["time"],
        )
        assert result.passed is False
        assert "adx" in result.failed_filters
        assert "time" in result.passed_filters

    def test_defaults(self):
        """Test default values."""
        result = FilterResult(passed=True, failed_filters=[], passed_filters=[])
        assert result.size_factor == Decimal("1.0")
        assert result.delay_until == 0

    def test_size_factor_reduction(self):
        """Test with reduced size factor."""
        result = FilterResult(
            passed=True,
            failed_filters=[],
            passed_filters=["time"],
            size_factor=Decimal("0.5"),
        )
        assert result.size_factor == Decimal("0.5")

    def test_delay_until(self):
        """Test with delay_until timestamp."""
        result = FilterResult(
            passed=False,
            failed_filters=["cooldown"],
            passed_filters=[],
            delay_until=1706500000000,
        )
        assert result.delay_until == 1706500000000

    def test_multiple_failed_filters(self):
        """Test with multiple failed filters."""
        result = FilterResult(
            passed=False,
            failed_filters=["adx", "volatility", "volume"],
            passed_filters=["time"],
        )
        assert len(result.failed_filters) == 3
        assert all(f in result.failed_filters for f in ["adx", "volatility", "volume"])

    def test_combined_size_and_delay(self):
        """Test with both size reduction and delay."""
        result = FilterResult(
            passed=True,
            failed_filters=[],
            passed_filters=["time", "volatility"],
            size_factor=Decimal("0.75"),
            delay_until=1706500000000,
        )
        assert result.size_factor == Decimal("0.75")
        assert result.delay_until == 1706500000000
        assert result.passed is True


# Tests for FilterManager
from unittest.mock import Mock  # noqa: E402

FilterManager = _fm_module.FilterManager


class TestFilterManager:
    """Tests for FilterManager class."""

    def test_init_empty_config(self):
        """Test initialization with no filters configured."""
        manager = FilterManager(config=None, instrument_id=Mock())
        assert manager.filter_count == 0
        assert manager.is_initialized is False

    def test_initialize_no_config_returns_empty(self):
        """Test initialize with None config."""
        manager = FilterManager(config=None, instrument_id=Mock())
        subs = manager.initialize()
        assert len(subs) == 0
        assert manager.is_initialized is True

    def test_check_no_filters_passes(self):
        """Test check passes when no filters configured."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager.initialize()
        result = manager.check(Mock())
        assert result.passed is True

    def test_check_all_pass(self):
        """Test check when all filters pass."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        filter1 = Mock()
        filter1.name = "time"
        filter1.check.return_value = Mock(passed=True)
        manager._filters = [filter1]

        result = manager.check(Mock())
        assert result.passed is True
        assert "time" in result.passed_filters

    def test_check_one_fails(self):
        """Test check when one filter fails."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        filter1 = Mock()
        filter1.name = "adx"
        filter1.check.return_value = Mock(passed=False)
        manager._filters = [filter1]

        result = manager.check(Mock())
        assert result.passed is False
        assert "adx" in result.failed_filters

    def test_check_mixed_results(self):
        """Test check with mixed pass/fail results."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        filter1 = Mock()
        filter1.name = "time"
        filter1.check.return_value = Mock(passed=True)

        filter2 = Mock()
        filter2.name = "adx"
        filter2.check.return_value = Mock(passed=False)

        filter3 = Mock()
        filter3.name = "volume"
        filter3.check.return_value = Mock(passed=True)

        manager._filters = [filter1, filter2, filter3]

        result = manager.check(Mock())
        assert result.passed is False
        assert "time" in result.passed_filters
        assert "volume" in result.passed_filters
        assert "adx" in result.failed_filters

    def test_update_calls_filters(self):
        """Test update calls update on all filters."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        filter1 = Mock()
        manager._filters = [filter1]

        bar = Mock()
        manager.update(bar)
        filter1.update.assert_called_once_with(bar)

    def test_update_handles_missing_update_method(self):
        """Test update handles filters without update method gracefully."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        # Create a filter mock that raises AttributeError on update
        filter1 = Mock(spec=[])  # Empty spec means no methods
        filter1.name = "test"
        manager._filters = [filter1]

        bar = Mock()
        # Should not raise
        manager.update(bar)

    def test_is_mtf_bar(self):
        """Test MTF bar detection."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._mtf_bar_type = "BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL"

        bar = Mock()
        bar.bar_type = Mock()
        bar.bar_type.__str__ = Mock(return_value="BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL")

        assert manager.is_mtf_bar(bar) is True

        bar.bar_type.__str__ = Mock(return_value="BTCUSDT.BINANCE-5-MINUTE-LAST-EXTERNAL")
        assert manager.is_mtf_bar(bar) is False

    def test_is_mtf_bar_none(self):
        """Test MTF bar detection when no MTF configured."""
        manager = FilterManager(config=None, instrument_id=Mock())
        # _mtf_bar_type is None by default

        bar = Mock()
        bar.bar_type = Mock()
        bar.bar_type.__str__ = Mock(return_value="BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL")

        assert manager.is_mtf_bar(bar) is False

    def test_check_not_initialized_passes(self):
        """Test check returns passed=True when not initialized."""
        manager = FilterManager(config=None, instrument_id=Mock())
        # Don't call initialize()
        result = manager.check(Mock())
        assert result.passed is True

    def test_check_handles_exception_in_filter(self):
        """Test check handles exceptions in filter.check gracefully."""
        manager = FilterManager(config=None, instrument_id=Mock())
        manager._initialized = True

        filter1 = Mock()
        filter1.name = "bad_filter"
        filter1.check.side_effect = Exception("Filter error")
        manager._filters = [filter1]

        bar = Mock()
        result = manager.check(bar)
        # Should fail gracefully, treating exception as failure
        assert result.passed is False
        assert "bad_filter" in result.failed_filters


class TestFilterManagerScopeFilter:
    """Tests for FilterManager scope_filter parameter."""

    def test_init_with_scope_filter_global(self):
        """Test initialization with scope_filter='global'."""
        manager = FilterManager(config=None, instrument_id=Mock(), scope_filter="global")
        assert manager._scope_filter == "global"

    def test_init_with_scope_filter_per_pair(self):
        """Test initialization with scope_filter='per_pair'."""
        manager = FilterManager(config=None, instrument_id=Mock(), scope_filter="per_pair")
        assert manager._scope_filter == "per_pair"

    def test_init_default_scope_filter_is_all(self):
        """Test default scope_filter is 'all'."""
        manager = FilterManager(config=None, instrument_id=Mock())
        assert manager._scope_filter == "all"


class TestFilterManagerScopeFiltering:
    """Tests for scope-based filter creation in initialize()."""

    def _scoped_config(self):
        """Real FiltersConfig with a global TimeFilter and a per_pair VolatilityFilter.

        All cooldown windows are zero so no cooldown filter is created, keeping the
        scope assertions focused on time (global) vs volatility (per_pair).
        """
        from custos_toolkit_nautilus.adapter.config.filters import (
            AdxFilterConfig,
            CooldownConfig,
            FiltersConfig,
            MomentumFilterConfig,
            MtfFilterConfig,
            RegimeFilterConfig,
            TimeFilterConfig,
            VolatilityFilterConfig,
            VolumeFilterConfig,
        )

        return FiltersConfig(
            time_filter=TimeFilterConfig(enabled=True, scope="global", trading_hours="00:00-23:59"),
            volatility_filter=VolatilityFilterConfig(
                enabled=True, scope="per_pair", min_atr_pct=0.003, max_atr_pct=0.05, atr_lookback=14
            ),
            adx_filter=AdxFilterConfig(enabled=False),
            volume_filter=VolumeFilterConfig(enabled=False),
            momentum_filter=MomentumFilterConfig(enabled=False),
            regime_filter=RegimeFilterConfig(enabled=False),
            mtf_filter=MtfFilterConfig(enabled=False),
            cooldown=CooldownConfig(after_exit=0, after_stop_loss=0, after_take_profit=0),
        )

    def test_scope_filter_global_only_creates_global_filters(self):
        """Test scope_filter='global' only creates global-scope filters."""
        config = self._scoped_config()

        manager = FilterManager(
            config=config,
            instrument_id=Mock(),
            scope_filter="global",
        )
        manager.initialize()

        # Should only have TimeFilter (scope=global)
        assert manager.filter_count == 1
        assert any(f.name == "time" for f in manager._filters)

    def test_scope_filter_per_pair_only_creates_per_pair_filters(self):
        """Test scope_filter='per_pair' only creates per_pair-scope filters."""
        config = self._scoped_config()

        manager = FilterManager(
            config=config,
            instrument_id=Mock(),
            scope_filter="per_pair",
        )
        manager.initialize()

        # Should only have VolatilityFilter (scope=per_pair)
        assert manager.filter_count == 1
        assert any(f.name == "volatility" for f in manager._filters)

    def test_scope_filter_all_creates_all_filters(self):
        """Test scope_filter='all' creates all enabled filters."""
        config = self._scoped_config()

        manager = FilterManager(
            config=config,
            instrument_id=Mock(),
            scope_filter="all",
        )
        manager.initialize()

        # Should have both TimeFilter and VolatilityFilter
        assert manager.filter_count == 2


class TestFilterManagerUsesNautilusBackedIndicatorFilters:
    """Indicator filters come from the engine-backed package; time and cooldown do not.

    Indicator filters wrap Nautilus indicators and so belong to the engine adapter.
    Time and cooldown need no engine and stay in the platform-neutral package. The
    split is asserted by module origin, since a filter created from the wrong package
    still satisfies every behavioural test.
    """

    def _full_config(self):
        from custos_toolkit_nautilus.adapter.config.filters import (
            AdxFilterConfig,
            CooldownConfig,
            FiltersConfig,
            MomentumFilterConfig,
            MtfFilterConfig,
            RegimeFilterConfig,
            TimeFilterConfig,
            VolatilityFilterConfig,
            VolumeFilterConfig,
        )

        return FiltersConfig(
            volatility_filter=VolatilityFilterConfig(
                enabled=True, min_atr_pct=0.003, max_atr_pct=0.05, atr_lookback=14
            ),
            adx_filter=AdxFilterConfig(enabled=True, period=14, threshold=25),
            volume_filter=VolumeFilterConfig(enabled=True, ma_period=20, threshold=1.2),
            regime_filter=RegimeFilterConfig(
                enabled=True, method="efficiency_ratio", lookback=20, trending_threshold=0.5
            ),
            momentum_filter=MomentumFilterConfig(enabled=True, indicator="rsi"),
            # time filter (global) -> platform-neutral package
            time_filter=TimeFilterConfig(enabled=True, trading_hours="00:00-23:59", scope="global"),
            # cooldown -> platform-neutral package
            cooldown=CooldownConfig(
                after_exit=0, after_stop_loss=300, after_take_profit=0, min_holding_time=0
            ),
            mtf_filter=MtfFilterConfig(enabled=False),
        )

    def test_indicator_filters_from_nautilus_package(self):
        manager = FilterManager(config=self._full_config(), instrument_id=Mock())
        manager.initialize()

        by_name = {f.name: f for f in manager._filters}
        for name in ("volatility", "adx", "momentum", "volume", "regime"):
            assert name in by_name, f"{name} filter not created"
            mod = type(by_name[name]).__module__
            assert mod.startswith("custos_toolkit_nautilus.adapter.filters"), (
                f"{name} from {mod}, expected the engine adapter package"
            )

    def test_time_and_cooldown_come_from_the_platform_neutral_package(self):
        manager = FilterManager(config=self._full_config(), instrument_id=Mock())
        manager.initialize()

        by_name = {f.name: f for f in manager._filters}
        for name in ("time", "cooldown"):
            assert name in by_name, f"{name} filter not created"
            mod = type(by_name[name]).__module__
            assert mod.startswith("custos_toolkit.filters"), (
                f"{name} from {mod}, expected the platform-neutral filter package"
            )

    def test_cooldown_and_mtf_config_projected_faithfully(self):
        """The fields a platform-neutral filter receives come from the typed config."""
        from custos_toolkit_nautilus.adapter.config.filters import (
            CooldownConfig,
            FiltersConfig,
            MtfFilterConfig,
        )

        config = FiltersConfig(
            cooldown=CooldownConfig(after_exit=0, after_stop_loss=420, after_take_profit=0),
            mtf_filter=MtfFilterConfig(
                enabled=True, higher_timeframe="2h", alignment_mode="not_against"
            ),
        )
        manager = FilterManager(config=config, instrument_id=Mock())
        subs = manager.initialize()

        by_name = {f.name: f for f in manager._filters}
        assert by_name["cooldown"].after_stop_loss == 420
        assert by_name["mtf"].alignment_mode == "not_against"
        assert by_name["mtf"].higher_timeframe == "2h"
        # higher_timeframe drives the HTF subscription bar type (2h -> 2-HOUR).
        assert manager._mtf_bar_type is not None and "2-HOUR" in manager._mtf_bar_type
        assert any(s.bar_type == manager._mtf_bar_type for s in subs)


class TestFilterManagerTimeFilterDriftCorrection:
    """Golden: TimeFilterConfig (trading_hours / excluded_days) must actually drive
    the TimeFilter through the nautilus FilterManager path.

    The config is fed straight to TimeFilter via msgspec.structs.asdict, so the
    TimeFilterConfig fields (trading_hours, excluded_days, excluded_dates) reach the
    filter verbatim with no intermediate projection.
    """

    def test_build_time_filter_dict_removed(self):
        """The hand-built projection is gone — the config dict is fed directly."""
        assert not hasattr(FilterManager, "_build_time_filter_dict")

    def _time_filter(self, **time_kwargs):
        from custos_toolkit_nautilus.adapter.config.filters import FiltersConfig, TimeFilterConfig

        config = FiltersConfig(
            time_filter=TimeFilterConfig(enabled=True, scope="global", **time_kwargs)
        )
        manager = FilterManager(config=config, instrument_id=Mock(), scope_filter="all")
        manager.initialize()
        return next(f for f in manager._filters if f.name == "time")

    @staticmethod
    def _bar_at(dt):
        return Mock(timestamp=int(dt.timestamp() * 1e9))

    def test_trading_hours_drive_hour_window(self):
        from datetime import UTC, datetime

        tf = self._time_filter(trading_hours="09:00-17:00")
        # 12:00 inside window → allow; 18:00 outside → block (old path allowed both).
        assert tf.check(self._bar_at(datetime(2026, 6, 15, 12, 0, tzinfo=UTC))).passed is True
        assert tf.check(self._bar_at(datetime(2026, 6, 15, 18, 0, tzinfo=UTC))).passed is False

    def test_excluded_days_drive_weekday_filter(self):
        from datetime import UTC, datetime, timedelta

        # excluded_days=(2,) → Wednesday blocked, Saturday allowed.
        # Old path (allowed_days defaulted Mon-Fri) did the opposite.
        tf = self._time_filter(excluded_days=(2,))
        base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        wednesday = base + timedelta(days=(2 - base.weekday()) % 7)
        saturday = base + timedelta(days=(5 - base.weekday()) % 7)
        assert wednesday.weekday() == 2 and saturday.weekday() == 5
        assert tf.check(self._bar_at(wednesday)).passed is False
        assert tf.check(self._bar_at(saturday)).passed is True

    def test_excluded_dates_passthrough(self):
        from datetime import UTC, datetime

        tf = self._time_filter(excluded_dates=("2026-06-17",))
        blocked = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
        allowed = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
        assert tf.check(self._bar_at(blocked)).passed is False
        assert tf.check(self._bar_at(allowed)).passed is True

    def test_malformed_trading_hours_fails_fast(self):
        """A malformed window is refused at initialize, not waved through as all-day."""
        import pytest
        from custos_toolkit_nautilus.adapter.config.filters import FiltersConfig, TimeFilterConfig

        config = FiltersConfig(
            time_filter=TimeFilterConfig(enabled=True, scope="global", trading_hours="garbage")
        )
        manager = FilterManager(config=config, instrument_id=Mock(), scope_filter="all")
        with pytest.raises(ValueError):
            manager.initialize()


class TestFilterManagerCooldownMinHoldingTime:
    """Pins a decision: the config keeps min_holding_time, the filter never reads it.

    Removing the field everywhere it is declared is separate work. Until then this
    guards the only property that matters — that carrying it changes no behaviour.
    """

    def _cooldown_filter(self, min_holding):
        from custos_toolkit_nautilus.adapter.config.filters import CooldownConfig, FiltersConfig

        config = FiltersConfig(
            cooldown=CooldownConfig(after_stop_loss=300, min_holding_time=min_holding)
        )
        manager = FilterManager(config=config, instrument_id=Mock())
        manager.initialize()
        return next(f for f in manager._filters if f.name == "cooldown")

    def test_min_holding_time_does_not_affect_filter(self):
        f0 = self._cooldown_filter(0)
        f999 = self._cooldown_filter(999)
        # The field reaches nothing the filter reads, so behaviour cannot track it.
        assert f0.after_stop_loss == f999.after_stop_loss == 300
        assert not hasattr(f0, "min_holding_time")


class TestFilterManagerNoConfigGetattr:
    """The config types are strict structs, so reading them defensively is forbidden.

    Only the config is covered. The instrument id, the filter objects, FilterResult
    and the bar are duck-typed, and reading those defensively is legitimate.
    """

    def test_no_config_getattr_defense(self):
        import inspect
        import re

        # Read through the module object, not a repo-relative path: the path form
        # inspected this repo's copy instead of the one under test.
        src = inspect.getsource(_fm_module)
        offenders = [
            f"{i}: {ln.strip()}"
            for i, ln in enumerate(src.splitlines(), 1)
            if re.search(r"getattr\(\s*(self\._config|\w*_config)\b", ln)
        ]
        assert not offenders, "defensive reads left on a strict config type:\n" + "\n".join(
            offenders
        )

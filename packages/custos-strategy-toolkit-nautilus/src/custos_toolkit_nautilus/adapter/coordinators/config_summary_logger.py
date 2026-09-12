"""Config logging component.

Holds the two startup config-logging passes (expected config before init / active
config after init). Injects a strategy reference and reaches ``config``, the equity
getters (``_get_effective_capital`` /
``_get_risk_equity``), the runtime components (``_risk_controller`` /
``_global_filter_manager`` / ``_contexts``) and ``log`` through it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.common import LogColor

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class ConfigSummaryLogger:
    """Format and print the strategy config summary.

    ``log_config_summary`` prints the expected config before init (CYAN);
    ``log_active_config`` prints the effective config after init (GREEN).
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def log_config_summary(self) -> None:
        """Log expected configuration before initialization."""
        s = self._strategy
        # Trading config
        trading = s.config.trading
        direction = "both"
        if trading.enable_long and not trading.enable_short:
            direction = "long"
        elif trading.enable_short and not trading.enable_long:
            direction = "short"
        s.log.info(
            f"[CONFIG] Trading: pairs={list(trading.pairs)}, connector={trading.connector}, "
            f"leverage={trading.leverage}, direction={direction}, order_type={trading.order_type}",
            color=LogColor.CYAN,
        )

        # Position config
        pos = s.config.position
        s.log.info(
            f"[CONFIG] Position: size_type={pos.size_type}, size_value={pos.size_value}, "
            f"capital_mode={pos.capital_mode}, initial_capital={pos.initial_capital}, "
            f"base_size_factor={pos.base_size_factor}",
            color=LogColor.CYAN,
        )

        # Risk Global config
        gr = s.config.risk.global_risk
        s.log.info(
            f"[CONFIG] Risk.Global: max_daily_loss={gr.max_daily_loss * 100:.1f}%, "
            f"max_drawdown={gr.max_drawdown * 100:.1f}%, "
            f"consecutive_loss_pause={gr.consecutive_loss_pause}",
            color=LogColor.CYAN,
        )

        # Risk Trade config
        tr = s.config.risk.trade
        sl_str = "disabled"
        if tr.stop_loss.method == "atr":
            sl_str = f"atr({tr.stop_loss.atr.multiplier}x)"
        elif tr.stop_loss.method == "fixed":
            sl_str = f"fixed({tr.stop_loss.fixed.value * 100:.1f}%)"
        else:
            sl_str = tr.stop_loss.method

        tp_str = "disabled"
        if tr.take_profit.method == "atr":
            tp_str = f"atr({tr.take_profit.atr.multiplier}x)"
        elif tr.take_profit.method == "fixed":
            tp_str = f"fixed({tr.take_profit.fixed.value * 100:.1f}%)"
        elif tr.take_profit.method == "scaled":
            tp_str = f"scaled({tr.take_profit.scaled.levels} levels)"
        else:
            tp_str = tr.take_profit.method

        trailing_str = "disabled"
        if tr.stop_loss.trailing.enabled:
            trailing_str = "enabled"

        s.log.info(
            f"[CONFIG] Risk.Trade: sl_tp_mode={tr.sl_tp_mode}, stop_loss={sl_str}, "
            f"take_profit={tp_str}, atr_period={tr.atr_period}, trailing={trailing_str}",
            color=LogColor.CYAN,
        )

        # Filters config
        enabled_filters = []
        filter_params = []

        if s.config.filters.adx_filter.enabled:
            enabled_filters.append("adx")
            filter_params.append(f"adx_threshold={s.config.filters.adx_filter.threshold}")

        if s.config.filters.volatility_filter.enabled:
            enabled_filters.append("volatility")
            filter_params.append(
                f"volatility_min={s.config.filters.volatility_filter.min_atr_pct * 100:.1f}%"
            )

        if s.config.filters.volume_filter.enabled:
            enabled_filters.append("volume")

        if s.config.filters.time_filter.enabled:
            enabled_filters.append("time")
            filter_params.append(f"time_range={s.config.filters.time_filter.trading_hours}")

        # Cooldown is enabled if any cooldown time is > 0
        cd = s.config.filters.cooldown
        if cd.after_exit > 0 or cd.after_stop_loss > 0 or cd.after_take_profit > 0:
            enabled_filters.append("cooldown")

        if enabled_filters:
            params_str = ", ".join(filter_params) if filter_params else ""
            s.log.info(
                f"[CONFIG] Filters: enabled={enabled_filters}"
                + (f", {params_str}" if params_str else ""),
                color=LogColor.CYAN,
            )
        else:
            s.log.info("[CONFIG] Filters: none configured", color=LogColor.CYAN)

        # Warmup config
        warmup_config = s._get_warmup_config()
        if warmup_config:
            if warmup_config.mode == "none":
                s.log.info(
                    "[CONFIG] Warmup: mode=none (no historical data warmup)",
                    color=LogColor.CYAN,
                )
            elif warmup_config.mode == "warmup":
                s.log.info(
                    f"[CONFIG] Warmup: mode=warmup, min_bars={warmup_config.min_bars}, "
                    f"preferred_bars={warmup_config.preferred_bars}",
                    color=LogColor.CYAN,
                )
            elif warmup_config.mode == "snapshot":
                snapshot_enabled = s.config.snapshot.enabled
                s.log.info(
                    f"[CONFIG] Warmup: mode=snapshot, snapshot={snapshot_enabled}",
                    color=LogColor.CYAN,
                )
        else:
            s.log.info("[CONFIG] Warmup: not configured", color=LogColor.CYAN)

    def log_active_config(self) -> None:
        """Log actual active configuration after initialization."""
        s = self._strategy
        # Trading - actual instruments
        if s._contexts:
            first_ctx = next(iter(s._contexts.values()))
            s.log.info(
                f"[ACTIVE] Trading: instrument={first_ctx.instrument_id}, "
                f"bar_type={first_ctx.bar_type}, venue={first_ctx.instrument_id.venue}",
                color=LogColor.GREEN,
            )

        # Position - effective capital
        effective_capital = s._get_effective_capital()
        s.log.info(
            f"[ACTIVE] Position: effective_capital={effective_capital:.2f} USDT",
            color=LogColor.GREEN,
        )

        # Risk Global - current state
        if s._risk_controller:
            current_equity = s._get_risk_equity()
            peak = s._risk_controller.peak_equity
            drawdown = ((peak - current_equity) / peak * 100) if peak > 0 else Decimal("0")
            s.log.info(
                f"[ACTIVE] Risk.Global: peak_equity={peak:.2f}, current_drawdown={drawdown:.2f}%",
                color=LogColor.GREEN,
            )

        # Risk Trade
        tr = s.config.risk.trade
        # max_loss_pct is a decimal (0.02 = 2%); render as a percent for the human log.
        s.log.info(
            f"[ACTIVE] Risk.Trade: sl_tp_mode={s._mode.value}, "
            f"max_loss_pct={tr.max_loss_pct * 100:.2f}%",
            color=LogColor.GREEN,
        )

        # Filters - active count
        global_count = s._global_filter_manager.filter_count if s._global_filter_manager else 0
        pair_count = sum(
            ctx.filter_manager.filter_count if ctx.filter_manager else 0
            for ctx in s._contexts.values()
        )
        s.log.info(
            f"[ACTIVE] Filters: global={global_count}, per_pair={pair_count}, "
            f"total={global_count + pair_count} filters active",
            color=LogColor.GREEN,
        )

        # Warmup - status
        warmup_config = s._get_warmup_config()
        if warmup_config and warmup_config.mode == "warmup":
            s.log.info(
                f"[ACTIVE] Warmup: requested={warmup_config.preferred_bars} bars",
                color=LogColor.GREEN,
            )
        elif warmup_config and warmup_config.mode == "snapshot":
            restored = s._snapshot_restored
            s.log.info(
                f"[ACTIVE] Warmup: restored_from_snapshot={restored}",
                color=LogColor.GREEN,
            )
        else:
            s.log.info(
                "[ACTIVE] Warmup: cold_start=true, indicators will warm up from live data",
                color=LogColor.GREEN,
            )

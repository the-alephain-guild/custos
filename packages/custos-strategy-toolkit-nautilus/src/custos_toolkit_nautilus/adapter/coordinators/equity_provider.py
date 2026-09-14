"""Equity lookup component.

Holds the *lookup* responsibility (reading Portfolio / cache / account balance);
the platform-neutral *compute* (fail-safe fallback decision) lives in
``custos_toolkit.risk.equity.resolve_risk_equity``. Injects a strategy reference and reaches
``portfolio`` / ``cache`` / ``_derive_instrument_id_for_pair`` / ``config`` / ``log``
through it. Lookups only need the first pair's InstrumentId (derived directly, same
source as ``PairContextCoordinator.create_context``), so they skip the pair->context
indirection and don't depend on ``_pairs`` / ``_contexts``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from custos_toolkit.risk import resolve_risk_equity

if TYPE_CHECKING:
    from nautilus_trader.model import InstrumentId

    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class EquityProvider:
    """Equity lookup.

    Position sizing uses the available balance (``get_effective_capital`` -> free);
    risk control uses mark-to-market equity (``get_risk_equity``). Any unreliable
    lookup fail-safes back to the free balance with a WARN (never silent).
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy
        # Most recent reliable mark-to-market risk equity, used as a conservative floor
        # when a later tick can't be priced (so the optimistic free balance can't relax
        # the risk thresholds).
        self._last_good_risk_equity: Decimal | None = None
        # Whether the most recent get_risk_equity() produced a reliable mark (fully
        # priced, finite, > 0). False on any free/fallback path. Entry gating reads
        # this to fail-closed: an unreliable risk equity blocks new entries.
        self._risk_equity_reliable: bool = False

    def get_effective_capital(self) -> Decimal:
        """Resolve position-sizing capital by capital_mode.

        fixed_capital mode uses the configured initial_capital directly; the rest
        (compound, etc.) use the actual balance.
        """
        pos_config = self._strategy.config.position
        if pos_config.capital_mode == "fixed_capital":
            return Decimal(str(pos_config.initial_capital))
        return self.get_actual_balance()

    def _primary_instrument_id(self) -> InstrumentId | None:
        """The first configured pair's InstrumentId (lookups only need venue + quote currency).

        Derived directly -- same source as ``PairContextCoordinator.create_context``
        (which also uses ``_derive_instrument_id_for_pair``), so it's equivalent to
        ``ctx.instrument_id`` without the pair->context indirection. Returns None when
        there are no pairs.
        """
        s = self._strategy
        pairs = s.config.trading.pairs
        return s._derive_instrument_id_for_pair(pairs[0]) if pairs else None

    def get_actual_balance(self) -> Decimal:
        """Read the available balance in the first pair's quote currency."""
        s = self._strategy
        instrument_id = self._primary_instrument_id()
        if instrument_id is None:
            return self.fallback_capital()

        account = s.portfolio.account(instrument_id.venue)
        balances = account.balances() if account else None
        if balances:
            # Read the quote currency from the instrument metadata.
            instrument = s.cache.instrument(instrument_id)
            if instrument is None:
                s.log.warning(f"Instrument not found: {instrument_id}")
                return self.fallback_capital()

            quote_currency = instrument.quote_currency

            if quote_currency in balances:
                return Decimal(str(balances[quote_currency].free.as_decimal()))

            s.log.warning(f"Quote currency {quote_currency} not found in balances")

        return self.fallback_capital()

    def fallback_capital(self) -> Decimal:
        """The configured initial_capital as a fallback."""
        return Decimal(str(self._strategy.config.position.initial_capital))

    def get_risk_equity(self) -> Decimal:
        """Mark-to-market equity for risk control (drawdown / daily-loss / peak).

        Uses ``Portfolio.equity()`` (account total + unrealized PnL) so risk thresholds
        reflect true economic equity rather than available margin. Position sizing still
        uses ``get_effective_capital`` (free) to avoid procyclical leverage. Any
        unreliable equity -- empty dict / missing quote currency / understated (unpriced
        positions) / non-finite / <= 0 -- falls back to the free balance with a WARN
        (fail-safe, never silent).
        """
        s = self._strategy
        # Default to unreliable; only the fully-priced resolve path below marks it
        # reliable. Every free/fallback early return therefore stays unreliable.
        self._risk_equity_reliable = False
        # Even the free-balance lookup must not crash this getter -- it feeds the risk /
        # order-submission callbacks (a protection mechanism's own failure path must be
        # fail-safe). If free is unavailable, degrade to fallback capital.
        try:
            free = self.get_actual_balance()
        except Exception as exc:
            s.log.warning(
                f"[RISK_EQUITY] free balance lookup failed ({exc}); using fallback capital"
            )
            return self.fallback_capital()

        # The portfolio lookup itself must never crash the risk / order callbacks it
        # feeds; any unexpected error degrades to the free balance. The instrument_id
        # derive is inside the try too (the protection mechanism is itself fail-safe).
        try:
            instrument_id = self._primary_instrument_id()
            if instrument_id is None:
                s.log.warning("[RISK_EQUITY] no trading pair available; fell back to free balance")
                return free
            instrument = s.cache.instrument(instrument_id)
            if instrument is None:
                s.log.warning(
                    f"[RISK_EQUITY] instrument unavailable for {instrument_id}; "
                    f"fell back to free balance"
                )
                return free

            venue = instrument_id.venue
            equity_dict = s.portfolio.equity(venue)
            understated = bool(s.portfolio.missing_price_instruments(venue))
            money = equity_dict.get(instrument.quote_currency) if equity_dict else None
            equity_value = money.as_decimal() if money is not None else None
        except Exception as exc:
            s.log.warning(f"[RISK_EQUITY] equity lookup failed ({exc}); fell back to free balance")
            return free

        resolved, warn = resolve_risk_equity(
            equity_value, understated, free, self._last_good_risk_equity
        )
        if warn:
            s.log.warning(f"[RISK_EQUITY] {warn}")
        else:
            # Reliable mark — remember it as the conservative floor for later ticks.
            self._last_good_risk_equity = resolved
            self._risk_equity_reliable = True
        return resolved

    def is_risk_equity_reliable(self) -> bool:
        """Whether the most recent ``get_risk_equity()`` was a reliable mark. Entry
        gating blocks new entries when this is False (fail-closed); exits/management
        are unaffected. ``get_risk_equity`` is called every bar by the risk gate, so
        this reflects the current bar once ``check_risk_limits`` has run."""
        return self._risk_equity_reliable

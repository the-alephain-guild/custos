"""Reliable Nautilus portfolio snapshots for runtime safety decisions.

This module is the only Custos adapter allowed to translate Nautilus portfolio
objects into engine status, breaker inputs, and RunnerFact risk rows.  Keeping
the translation here prevents those consumers from silently disagreeing about
equity, marks, or unrealized PnL.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from custos.core.engine_protocol import PositionSnapshot


@dataclass(frozen=True, slots=True)
class NautilusPortfolioPosition:
    """A position valued from one trusted Nautilus mark."""

    instrument_id: str
    settlement_currency: str
    quantity: Decimal
    avg_px: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    notional: Decimal

    def engine_snapshot(self) -> PositionSnapshot:
        """Return the engine-protocol representation of this position."""

        return PositionSnapshot(
            instrument_id=self.instrument_id,
            quantity=self.quantity,
            avg_px=self.avg_px,
            unrealized_pnl=self.unrealized_pnl,
            notional=self.notional,
        )

    def runner_fact_row(self) -> dict[str, str]:
        """Return the canonical RunnerFact risk row."""

        return {
            "instrument": self.instrument_id,
            "quantity": str(self.quantity),
            "mark_price": str(self.mark_price),
            "currency": self.settlement_currency,
        }

    def valuation_row(self) -> dict[str, str]:
        """Return cost basis and the original mark for common-mark revaluation."""

        return {
            **self.runner_fact_row(),
            "avg_entry_price": str(self.avg_px),
        }


@dataclass(frozen=True, slots=True)
class NautilusPortfolioSnapshot:
    """One coherent portfolio valuation or a typed unreliable result."""

    venue: str | None
    currency: str | None
    equity: Decimal
    positions: tuple[NautilusPortfolioPosition, ...]
    reliable: bool
    unreliable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.reliable:
            if self.venue is None or self.currency is None:
                raise ValueError("a reliable portfolio snapshot needs venue and currency")
            if self.unreliable_reason is not None:
                raise ValueError("a reliable portfolio snapshot cannot have a failure reason")
        elif not self.unreliable_reason:
            raise ValueError("an unreliable portfolio snapshot needs a failure reason")

    @classmethod
    def unreliable(cls, reason: str) -> NautilusPortfolioSnapshot:
        """Build a typed fail-closed result without guessed financial values."""

        return cls(
            venue=None,
            currency=None,
            equity=Decimal("0"),
            positions=(),
            reliable=False,
            unreliable_reason=reason,
        )

    @property
    def open_notional(self) -> Decimal:
        return sum((position.notional for position in self.positions), Decimal("0"))

    def engine_positions(self) -> list[PositionSnapshot]:
        return [position.engine_snapshot() for position in self.positions]

    def runner_fact_rows(self) -> list[dict[str, str]]:
        return [position.runner_fact_row() for position in self.positions]

    def valuation_rows(self) -> list[dict[str, str]]:
        return [position.valuation_row() for position in self.positions]


class NautilusPortfolioSnapshotProvider:
    """Translate live Nautilus portfolio state without proxy calculations."""

    def __init__(self, *, price_type_mid: object | None = None) -> None:
        self._price_type_mid = price_type_mid

    def snapshot(
        self,
        runtime: Any,
        currency: str | None = None,
    ) -> NautilusPortfolioSnapshot:
        """Read equity, trusted marks, and PnL as one coherent snapshot.

        Takes the host's captured runtime rather than the node: 2.0's ``run_async``
        owns the node while it runs, so the cache and the portfolio have to be the
        ones captured before the run started.
        """

        try:
            cache = runtime.cache
            portfolio = runtime.portfolio
            positions = tuple(cache.positions_open())

            venue = self._resolve_venue(cache, positions)
            if venue is None:
                return NautilusPortfolioSnapshot.unreliable("venue_unavailable")

            # Equity first, then the verdict on whether it could be computed.
            #
            # `missing_price_instruments` reports what the last valuation found; it
            # does not perform one. `equity` is the valuation, and it updates that
            # record as it goes — entries go in for positions it cannot price and
            # come out once it can. Reading the record before running the valuation
            # therefore answers with the previous round's finding.
            #
            # That is not a subtle staleness. Reconciliation hands an open position
            # to the portfolio at startup, before any price has arrived, and that
            # first failed valuation is the finding every later read returns:
            # measured here as unpriced at 05:21:01.680, priced at 05:21:02.132, and
            # still reported missing two minutes and 347 mark prices later.
            equity_by_currency = portfolio.equity(venue)

            missing_prices = portfolio.missing_price_instruments(venue)
            if missing_prices:
                # Name them. "prices missing" alone cannot be acted on: whether the
                # feed is down, the subscription never landed, or a position is held
                # in an instrument nothing subscribed to are three different
                # problems, and the instrument that is missing says which.
                named = ",".join(sorted(str(instrument) for instrument in missing_prices))
                return NautilusPortfolioSnapshot.unreliable(f"portfolio_prices_missing:{named}")

            resolved_currency, equity = self._resolve_equity(equity_by_currency, currency)
            if resolved_currency is None or equity is None:
                reason = (
                    f"portfolio_equity_missing:{currency}"
                    if currency is not None
                    else "portfolio_equity_ambiguous"
                )
                return NautilusPortfolioSnapshot.unreliable(reason)

            converted: list[NautilusPortfolioPosition] = []
            for position in positions:
                instrument_id = position.instrument_id
                # The two sources do not return the same thing. ``mark_price`` returns a
                # ``MarkPriceUpdate`` -- an event carrying the price alongside its
                # timestamps -- while ``price`` returns the ``Price`` itself. Everything
                # downstream wants the price, and ``Position.unrealized_pnl`` refuses
                # anything else.
                #
                # This only became reachable when mark prices were first subscribed:
                # before that the call always returned None and the fallback below,
                # which does return a ``Price``, was the only branch that ever ran.
                mark = cache.mark_price(instrument_id)
                mark = getattr(mark, "value", mark)
                if mark is None and self._price_type_mid is not None:
                    mark = cache.price(instrument_id, self._price_type_mid)
                if mark is None:
                    return NautilusPortfolioSnapshot.unreliable(
                        f"mark_price_unavailable:{instrument_id}"
                    )

                quantity = _decimal(position.quantity) * _decimal(
                    getattr(position, "multiplier", 1)
                )
                if bool(getattr(position, "is_inverse", False)):
                    return NautilusPortfolioSnapshot.unreliable("inverse_position_not_supported")
                if bool(getattr(position, "is_short", False)) and quantity > 0:
                    quantity = -quantity
                average_price = _position_average_price(position)
                settlement_currency = str(
                    getattr(position, "settlement_currency", resolved_currency)
                ).upper()
                unrealized_pnl = _decimal(position.unrealized_pnl(mark))

                converted.append(
                    NautilusPortfolioPosition(
                        instrument_id=str(instrument_id),
                        settlement_currency=settlement_currency,
                        quantity=quantity,
                        avg_px=average_price,
                        mark_price=_decimal(mark),
                        unrealized_pnl=unrealized_pnl,
                        notional=abs(quantity) * _decimal(mark),
                    )
                )

            return NautilusPortfolioSnapshot(
                venue=str(venue),
                currency=resolved_currency,
                equity=equity,
                positions=tuple(converted),
                reliable=True,
            )
        except (ArithmeticError, AttributeError, InvalidOperation, TypeError, ValueError) as exc:
            return NautilusPortfolioSnapshot.unreliable(
                f"portfolio_snapshot_invalid:{type(exc).__name__}"
            )

    @staticmethod
    def _resolve_venue(cache: Any, positions: tuple[Any, ...]) -> object | None:
        if positions:
            instrument_id = positions[0].instrument_id
            return getattr(instrument_id, "venue", None)

        instrument_ids = tuple(cache.instrument_ids())
        if not instrument_ids:
            return None
        first = min(instrument_ids, key=str)
        return getattr(first, "venue", None)

    @staticmethod
    def _resolve_equity(
        equity_by_currency: object,
        requested_currency: str | None,
    ) -> tuple[str | None, Decimal | None]:
        if isinstance(equity_by_currency, dict):
            entries = tuple(equity_by_currency.items())
        elif hasattr(equity_by_currency, "items"):
            entries = tuple(cast(Any, equity_by_currency).items())
        else:
            entries = ((requested_currency, equity_by_currency),)

        if requested_currency is not None:
            for raw_currency, raw_equity in entries:
                if str(raw_currency).upper() == requested_currency.upper():
                    return requested_currency, _decimal(raw_equity)
            return None, None

        if len(entries) != 1:
            return None, None
        raw_currency, raw_equity = entries[0]
        if raw_currency is None:
            return None, None
        return str(raw_currency).upper(), _decimal(raw_equity)


def _position_average_price(position: object) -> Decimal:
    for attribute in ("avg_px_open", "avg_px"):
        value = getattr(position, attribute, None)
        if value is not None:
            return _decimal(value)
    return Decimal("0")


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    as_decimal = getattr(value, "as_decimal", None)
    if callable(as_decimal):
        return _decimal(as_decimal())
    nested_value = getattr(value, "value", None)
    if nested_value is not None and nested_value is not value:
        return _decimal(nested_value)
    return Decimal(str(value))

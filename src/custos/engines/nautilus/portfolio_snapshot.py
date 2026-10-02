"""Reliable Nautilus portfolio snapshots for runtime safety decisions.

This module is the only Custos adapter allowed to translate Nautilus portfolio
objects into engine status, breaker inputs, and RunnerFact risk rows.  Keeping
the translation here prevents those consumers from silently disagreeing about
equity, marks, or unrealized PnL.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from custos.core.engine_protocol import PositionSnapshot

# Set only while a stop boundary is read: there a value that arrives as a binary
# float cannot prove it never passed through one, so the read is unreliable.
_REFUSE_FLOAT: ContextVar[bool] = ContextVar("portfolio_refuse_float", default=False)


class _FloatSource(ValueError):
    """A portfolio input arrived as a binary float while floats are refused."""


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
    cash_inventory: tuple[dict[str, str], ...] | None = None
    # The oldest source time, in nanoseconds, of every price this valuation used;
    # None when it used none. Complete is False when some price had no source time.
    marks_oldest_ns: int | None = None
    price_watermarks_complete: bool = True

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

    def __init__(
        self,
        *,
        price_type_mid: object | None = None,
        price_type_last: object | None = None,
    ) -> None:
        self._price_type_mid = price_type_mid
        self._price_type_last = price_type_last

    def _price(self, cache: Any, instrument_id: object) -> object | None:
        """The trusted price of one instrument: mark, then mid, then last trade.

        The mark is what a perpetual's unrealised PnL and liquidation are marked
        against, so it comes first. A spot venue publishes none, and one that also
        publishes no order book (SoDEX spot) has no mid either; its last trade is
        then the only price there is. Without that last step every valuation on
        such a venue ended unpriced, and the fallback breaker stopped the
        deployment on its first fill.

        ``Cache.mark_price`` returns a ``MarkPriceUpdate`` -- the price with its
        timestamps -- while ``Cache.price`` returns the ``Price`` itself;
        everything downstream wants the price.
        """
        return self._priced(cache, instrument_id)[0]

    def _priced(self, cache: Any, instrument_id: object) -> tuple[object | None, int | None]:
        """The trusted price and the source time it carries, when it carries one.

        A mark keeps its own ``ts_event``. ``Cache.price`` returns a bare
        ``Price``, so a mid or last price takes its time from the quote or the
        trade it was derived from; when that cannot be read the time is unknown.
        """
        update = cache.mark_price(instrument_id)
        mark = getattr(update, "value", update)
        if mark is not None:
            return mark, _source_time(update)
        sources = ((self._price_type_mid, "quote"), (self._price_type_last, "trade"))
        for price_type, source in sources:
            if price_type is None:
                continue
            price = cache.price(instrument_id, price_type)
            if price is not None:
                reader = getattr(cache, source, None)
                tick = reader(instrument_id) if callable(reader) else None
                return price, _source_time(tick)
        return None, None

    def snapshot(
        self,
        runtime: Any,
        currency: str | None = None,
        *,
        refuse_float: bool = False,
    ) -> NautilusPortfolioSnapshot:
        """Read equity, trusted marks, and PnL as one coherent snapshot.

        Takes the host's captured runtime rather than the node: 2.0's ``run_async``
        owns the node while it runs, so the cache and the portfolio have to be the
        ones captured before the run started.

        ``refuse_float`` makes any input that arrives as a binary float an
        unreliable read rather than a converted one.
        """

        token = _REFUSE_FLOAT.set(refuse_float)
        try:
            return self._snapshot(runtime, currency)
        except _FloatSource:
            return NautilusPortfolioSnapshot.unreliable("portfolio_float_source")
        finally:
            _REFUSE_FLOAT.reset(token)

    def _snapshot(self, runtime: Any, currency: str | None) -> NautilusPortfolioSnapshot:
        watermarks: list[int | None] = []
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

            resolved_currency, equity, cash_inventory = self._equity_in_currency(
                cache, venue, equity_by_currency, currency, watermarks
            )
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
                mark, mark_time = self._priced(cache, instrument_id)
                watermarks.append(mark_time)
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

            known = [watermark for watermark in watermarks if watermark is not None]
            return NautilusPortfolioSnapshot(
                venue=str(venue),
                currency=resolved_currency,
                equity=equity,
                positions=tuple(converted),
                reliable=True,
                cash_inventory=cash_inventory,
                marks_oldest_ns=min(known) if known else None,
                price_watermarks_complete=len(known) == len(watermarks),
            )
        except _FloatSource:
            raise
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

    def _equity_in_currency(self, cache, venue, values, requested_currency, watermarks):
        if requested_currency is None or not hasattr(values, "items") or not values:
            return (*self._resolve_equity(values, requested_currency), None)
        target = requested_currency.upper()
        account_reader = getattr(cache, "account_for_venue", None)
        account = account_reader(venue) if callable(account_reader) else None
        if account is None or str(account.account_type) != "CASH":
            if account is None and any(
                str(key).upper() != target and _decimal(value) for key, value in values.items()
            ):
                raise ValueError("account type unavailable for multi-currency equity")
            return (*self._resolve_equity(values, requested_currency), None)
        inventory = []
        total = Decimal("0")
        for raw_currency, raw_amount in values.items():
            amount = _decimal(raw_amount)
            if not amount.is_finite() or amount < 0:
                raise ValueError("nonfinite portfolio equity")
            source = str(raw_currency).upper()
            if source == target:
                total += amount
                inventory.append({"asset": source, "quantity": str(amount), "mark_price": "1"})
                continue
            if not amount:
                continue
            rates = []
            for instrument_id in cache.instrument_ids():
                if str(instrument_id.venue) != str(venue):
                    continue
                instrument = cache.instrument(instrument_id)
                if instrument is None or str(instrument.instrument_class) != "SPOT":
                    continue
                base, quote = (
                    str(instrument.base_currency).upper(),
                    str(instrument.quote_currency).upper(),
                )
                if (base, quote) not in {(source, target), (target, source)}:
                    continue
                mark, mark_time = self._priced(cache, instrument_id)
                if mark is None:
                    continue
                watermarks.append(mark_time)
                rate = _decimal(mark)
                if not rate.is_finite() or rate <= 0:
                    raise ValueError("invalid asset conversion price")
                rates.append(rate if base == source else Decimal("1") / rate)
            if len(rates) != 1:
                raise ValueError(f"asset conversion unavailable or ambiguous: {source}/{target}")
            total += amount * rates[0]
            inventory.append(
                {"asset": source, "quantity": str(amount), "mark_price": str(rates[0])}
            )
        if not any(row["asset"] == target for row in inventory):
            inventory.append({"asset": target, "quantity": "0", "mark_price": "1"})
        return target, total, tuple(sorted(inventory, key=lambda row: row["asset"]))

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


def _source_time(value: object) -> int | None:
    """The source time a Nautilus market-data object carries, in nanoseconds."""

    ts_event = getattr(value, "ts_event", None)
    if type(ts_event) is not int or ts_event <= 0:
        return None
    return ts_event


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float) and _REFUSE_FLOAT.get():
        raise _FloatSource("portfolio input arrived as a binary float")
    as_decimal = getattr(value, "as_decimal", None)
    if callable(as_decimal):
        return _decimal(as_decimal())
    nested_value = getattr(value, "value", None)
    if nested_value is not None and nested_value is not value:
        return _decimal(nested_value)
    return Decimal(str(value))

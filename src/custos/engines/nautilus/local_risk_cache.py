"""Capture immutable values from the one Native cache bound to the local port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model import AccountId, InstrumentId

from custos.core.runtime_authority import RuntimeStorageUnavailable
from custos.local.loss_state import uint


class CacheRiskUnavailable(RuntimeStorageUnavailable):
    def __init__(self, reason: Literal["cohort", "marks"], message: str) -> None:
        super().__init__(message)
        self.reason = reason


def exact(value: Any, *, positive: bool = True) -> Decimal:
    number = value.as_decimal()
    if (
        type(number) is not Decimal
        or not number.is_finite()
        or number < 0
        or (positive and number == 0)
    ):
        raise ValueError("cache risk values require exact finite Decimal amounts")
    return number


@dataclass(frozen=True)
class CacheRiskPosition:
    position_id: str
    account_id: str
    instrument_id: str
    is_long: bool
    quantity: Decimal
    opening_client_ids: tuple[str, ...]


@dataclass(frozen=True)
class CacheRiskMarket:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    tick: Decimal
    price_precision: int
    size_precision: int
    ts_event: int
    ts_init: int


@dataclass(frozen=True)
class CacheRiskFrame:
    account_stamp: tuple[object, ...] | None
    positions: tuple[CacheRiskPosition, ...]
    markets: tuple[CacheRiskMarket, ...]

    def economic_identity(self) -> tuple[object, ...]:
        return self.account_stamp, self.positions


def cache_risk_frame(
    cache: Any, *, account_id: str, instrument_ids: tuple[str, ...]
) -> CacheRiskFrame:
    try:
        account = cache.account(AccountId(account_id))
        stamp = None
        if account is not None:
            event = account.last_event
            if str(event.account_id) != account_id:
                raise ValueError("cache account belongs to another execution account")
            info = event.info
            fields = (
                "papertrade_journal_id",
                "papertrade_checkpoint_revision",
                "papertrade_account_snapshot_keccak256",
                "papertrade_account_material_keccak256",
                "papertrade_source_epoch",
            )
            if isinstance(info, Mapping) and all(field in info for field in fields):
                stamp = (*(info[field] for field in fields), uint(event.ts_event))
        rows = tuple(cache.positions_open(account_id=AccountId(account_id)))
        if len(rows) > 64:
            raise ValueError("cache risk positions exceed the supported bound")
        positions = []
        for position in rows:
            if (
                str(position.account_id) != account_id
                or position.is_closed is not False
                or type(position.is_long) is not bool
                or type(position.is_short) is not bool
                or position.is_long == position.is_short
            ):
                raise ValueError("cache position account or direction is invalid")
            events = tuple(position.events())
            if len(events) > 256:
                raise ValueError("cache position events exceed the supported bound")
            positions.append(
                CacheRiskPosition(
                    str(position.id),
                    account_id,
                    str(position.instrument_id),
                    position.is_long,
                    exact(position.quantity),
                    tuple(sorted({str(event.client_order_id) for event in events})),
                )
            )
        if len({row.position_id for row in positions}) != len(positions):
            raise ValueError("cache contains duplicate risk positions")
    except Exception as exc:
        raise CacheRiskUnavailable(
            "cohort", "original cache economic cohort is unavailable"
        ) from exc
    markets = []
    try:
        for identifier in instrument_ids:
            native_id = InstrumentId.from_str(identifier)
            instrument, quote = cache.instrument(native_id), cache.quote(native_id)
            if instrument is None or quote is None:
                continue
            if str(instrument.id) != identifier or str(quote.instrument_id) != identifier:
                raise ValueError("cache market belongs to another instrument")
            precision = uint(instrument.price_precision, maximum=18)
            size_precision = uint(instrument.size_precision, maximum=18)
            tick = exact(instrument.price_increment)
            if tick != Decimal(f"1e-{precision}") or exact(instrument.size_increment) != Decimal(
                f"1e-{size_precision}"
            ):
                raise ValueError("cache market increments do not match the Native precision")
            bid, ask = exact(quote.bid_price), exact(quote.ask_price)
            if bid > ask:
                raise ValueError("cache quote is crossed")
            markets.append(
                CacheRiskMarket(
                    identifier,
                    bid,
                    ask,
                    tick,
                    precision,
                    size_precision,
                    uint(quote.ts_event),
                    uint(quote.ts_init),
                )
            )
    except Exception as exc:
        raise CacheRiskUnavailable("marks", "original cache risk marks are unavailable") from exc
    return CacheRiskFrame(
        stamp, tuple(sorted(positions, key=lambda row: row.position_id)), tuple(markets)
    )

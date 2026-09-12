"""Structural contracts for the Nautilus runtime objects used by the adapter."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from nautilus_trader.model import (
    Bar,
    BarType,
    ClientOrderId,
    Currency,
    InstrumentId,
    Money,
    OrderSide,
    OrderType,
    PositionSide,
    Price,
    Quantity,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
    Venue,
)
from nautilus_trader.model.instruments import Instrument


class Order(Protocol):
    @property
    def client_order_id(self) -> ClientOrderId: ...
    @property
    def instrument_id(self) -> InstrumentId: ...
    @property
    def is_closed(self) -> bool: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def is_reduce_only(self) -> bool: ...
    @property
    def order_type(self) -> OrderType: ...
    @property
    def price(self) -> Price | None: ...
    @property
    def quantity(self) -> Quantity: ...
    @property
    def side(self) -> OrderSide: ...
    @property
    def tags(self) -> list[str] | None: ...
    @property
    def trigger_price(self) -> Price | None: ...
    @property
    def ts_init(self) -> int: ...


class Position(Protocol):
    @property
    def avg_px_open(self) -> float: ...
    @property
    def instrument_id(self) -> InstrumentId: ...
    @property
    def is_closed(self) -> bool: ...
    @property
    def is_long(self) -> bool: ...
    @property
    def is_short(self) -> bool: ...
    @property
    def quantity(self) -> Quantity: ...
    @property
    def side(self) -> PositionSide: ...


class Cache(Protocol):
    def bars(self, bar_type: BarType) -> list[Bar]: ...
    def instrument(self, instrument_id: InstrumentId) -> Instrument | None: ...
    def order(self, client_order_id: ClientOrderId) -> Order | None: ...
    def orders_open(self, *, instrument_id: InstrumentId | None = ...) -> list[Order]: ...
    def position(self, instrument_id: InstrumentId) -> Position | None: ...
    def positions_open(self, *, instrument_id: InstrumentId | None = ...) -> list[Position]: ...


class Clock(Protocol):
    def timestamp_ns(self) -> int: ...


class Logger(Protocol):
    def debug(self, message: str, *, color: object = ...) -> None: ...
    def error(self, message: str, *, color: object = ...) -> None: ...
    def info(self, message: str, *, color: object = ...) -> None: ...
    def warning(self, message: str, *, color: object = ...) -> None: ...


class MessageBus(Protocol):
    def publish(self, topic: str, payload: bytes) -> None: ...


class OrderFactory(Protocol):
    def market(
        self,
        *,
        instrument_id: InstrumentId,
        order_side: OrderSide,
        quantity: Quantity,
        time_in_force: TimeInForce,
        reduce_only: bool = ...,
        tags: list[str] | None = ...,
    ) -> Order: ...
    def limit(
        self,
        *,
        instrument_id: InstrumentId,
        order_side: OrderSide,
        quantity: Quantity,
        price: Price,
        time_in_force: TimeInForce,
        reduce_only: bool = ...,
        tags: list[str] | None = ...,
    ) -> Order: ...
    def stop_market(
        self,
        *,
        instrument_id: InstrumentId,
        order_side: OrderSide,
        quantity: Quantity,
        trigger_price: Price,
        time_in_force: TimeInForce,
        reduce_only: bool = ...,
        tags: list[str] | None = ...,
    ) -> Order: ...
    def trailing_stop_market(
        self,
        *,
        instrument_id: InstrumentId,
        order_side: OrderSide,
        quantity: Quantity,
        trailing_offset: Decimal,
        activation_price: Price,
        trigger_type: TriggerType,
        trailing_offset_type: TrailingOffsetType,
        time_in_force: TimeInForce,
        reduce_only: bool = ...,
        tags: list[str] | None = ...,
    ) -> Order: ...


class Balance(Protocol):
    @property
    def free(self) -> Money: ...


class Account(Protocol):
    def balances(self) -> Mapping[Currency, Balance]: ...


class Portfolio(Protocol):
    def account(self, venue: Venue) -> Account | None: ...
    def equity(self, venue: Venue) -> Mapping[Currency, Money]: ...
    def missing_price_instruments(self, venue: Venue) -> set[InstrumentId]: ...

"""Non-bypass runner notional enforcement at the Nautilus execution boundary."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from nautilus_trader.core.rust.model import PriceType
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveExecClientFactory

from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.engines.nautilus.venue_binance import BINANCE_CLIENT_ORDER_ID_LEN_LIMIT

_POLICY_REJECTION_REASON = "custos_runner_notional_policy_rejected"
_CLIENT_ORDER_ID_REJECTION_REASON = "custos_runner_client_order_id_too_long_for_venue"


def _decimal(value: Any, *, field: str) -> Decimal:
    text = str(value).strip().replace("_", "")
    if " " in text:
        text = text.partition(" ")[0]
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"{field} is not a decimal") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"{field} must be a finite non-negative decimal")
    return result


def _truthy_attr(value: Any, name: str) -> bool:
    result = getattr(value, name, False)
    return bool(result() if callable(result) else result)


class NautilusCachedOrderSemantics:
    """Calculate venue-aware notionals from the canonical Nautilus cache."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    def order_notional(self, order: Any) -> Decimal:
        if _truthy_attr(order, "is_quote_quantity"):
            return _decimal(order.quantity, field="quote order quantity")
        return self._instrument_notional(
            order.instrument_id,
            order.quantity,
            self._order_price(order),
        )

    def modified_order_notional(self, command: Any) -> Decimal:
        order = self._cache.order(command.client_order_id)
        if order is None:
            raise RuntimeError("modified order is absent from the canonical Nautilus cache")
        quantity = getattr(command, "quantity", None) or order.quantity
        if _truthy_attr(order, "is_quote_quantity"):
            return _decimal(quantity, field="modified quote order quantity")
        price = (
            getattr(command, "price", None)
            or getattr(order, "price", None)
            or getattr(order, "trigger_price", None)
            or self._cache.price(order.instrument_id, PriceType.MID)
        )
        if price is None:
            raise RuntimeError("modified order has no reliable price")
        return self._instrument_notional(order.instrument_id, quantity, price)

    def fill_notional(self, event: Any) -> Decimal:
        return self._instrument_notional(
            event.instrument_id,
            event.last_qty,
            event.last_px,
        )

    def order_is_risk_reducing(self, order: Any) -> bool:
        return _truthy_attr(order, "is_reduce_only")

    def event_is_risk_reducing(self, event: Any) -> bool:
        order = self._cache.order(event.client_order_id)
        return order is not None and self.order_is_risk_reducing(order)

    def _order_price(self, order: Any) -> Any:
        price = (
            getattr(order, "price", None)
            or getattr(order, "trigger_price", None)
            or self._cache.price(order.instrument_id, PriceType.MID)
        )
        if price is None:
            raise RuntimeError("order has no reliable price")
        return price

    def _instrument_notional(
        self,
        instrument_id: Any,
        quantity: Any,
        price: Any,
    ) -> Decimal:
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError("order instrument is absent from the canonical Nautilus cache")
        return _decimal(
            instrument.notional_value(quantity, price),
            field="instrument notional",
        )


class RunnerSafetyExecutionDispatch:
    """Synchronous command interceptor shared by the NT client facade and tests."""

    def __init__(
        self,
        *,
        inner: Any,
        boundary: RunnerReservationBoundary,
        timestamp_ns: Callable[[], int],
    ) -> None:
        self._inner = inner
        self._boundary = boundary
        self._timestamp_ns = timestamp_ns

    def submit_order(self, command: Any) -> None:
        if self._client_order_id_too_long(command.order):
            self._reject_order(command.order, _CLIENT_ORDER_ID_REJECTION_REASON)
            return
        try:
            reservations = self._boundary.before_submit_order(command)
        except Exception:
            self._reject_order(command.order)
            return
        try:
            self._inner.submit_order(command)
        except Exception:
            self._boundary.rollback_submit(
                reservations,
                command_id=command.command_id,
            )
            raise

    def submit_order_list(self, command: Any) -> None:
        orders = tuple(command.order_list.orders)
        if any(self._client_order_id_too_long(order) for order in orders):
            # One unusable id fails the list: the venue would refuse that leg and leave
            # the rest as an unintended partial structure.
            for order in orders:
                self._reject_order(order, _CLIENT_ORDER_ID_REJECTION_REASON)
            return
        try:
            reservations = self._boundary.before_submit_order_list(command)
        except Exception:
            for order in orders:
                self._reject_order(order)
            return
        try:
            self._inner.submit_order_list(command)
        except Exception:
            self._boundary.rollback_submit(
                reservations,
                command_id=command.command_id,
            )
            raise

    def modify_order(self, command: Any) -> None:
        try:
            modification = self._boundary.before_modify_order(command)
        except Exception:
            self._inner.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                _POLICY_REJECTION_REASON,
                self._timestamp_ns(),
            )
            return
        try:
            self._inner.modify_order(command)
        except Exception:
            self._boundary.rollback_modify(
                modification,
                event_id=command.command_id,
            )
            raise

    def cancel_order(self, command: Any) -> None:
        self._inner.cancel_order(command)

    def cancel_all_orders(self, command: Any) -> None:
        self._inner.cancel_all_orders(command)

    def batch_cancel_orders(self, command: Any) -> None:
        self._inner.batch_cancel_orders(command)

    @staticmethod
    def _client_order_id_too_long(order: Any) -> bool:
        """Refuse an id the venue will refuse, before it costs a round trip.

        The id's shape is chosen where the strategy config is built, and nothing after
        construction can change it — the flags that decide it are read-only on the
        strategy. That makes the config the only place it can be got right, and a
        convention rather than an invariant: a signed artifact whose adapter builds its
        own config, or a strategy passing an explicit client_order_id, reaches the venue
        without ever consulting that builder. Both would reproduce the -4015 rejection of
        every order while every test about the builder stayed green.

        So the length is enforced here as well, at the boundary that owns venue
        interaction. Enforcing costs nothing when the id is already short, and turns a
        silent venue-side failure into a local rejection naming its own reason.

        Binance's limit applies to every order because Binance is the only venue this
        runner assembles an execution config for — `venue_binance._binance_exchange_type`
        refuses any other connector before an order can exist. Wiring a second venue
        means giving this a per-venue limit rather than leaving it to guess.
        """

        return len(str(order.client_order_id)) >= BINANCE_CLIENT_ORDER_ID_LEN_LIMIT

    def _reject_order(self, order: Any, reason: str = _POLICY_REJECTION_REASON) -> None:
        self._inner.generate_order_rejected(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            reason,
            self._timestamp_ns(),
        )


class GuardedLiveExecutionClient(LiveExecutionClient):
    """Typed facade which keeps the venue client behind the reservation gate."""

    def __init__(
        self,
        *,
        inner: LiveExecutionClient,
        boundary: RunnerReservationBoundary,
        loop: Any,
        msgbus: Any,
        cache: Any,
        clock: Any,
        config: Any,
    ) -> None:
        instrument_provider = getattr(inner, "_instrument_provider", None)
        if instrument_provider is None:
            raise RuntimeError("Nautilus execution client lacks its pinned instrument provider ABI")
        super().__init__(
            loop=loop,
            client_id=inner.id,
            venue=inner.venue,
            oms_type=inner.oms_type,
            account_type=inner.account_type,
            base_currency=inner.base_currency,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._inner = inner
        self._dispatch = RunnerSafetyExecutionDispatch(
            inner=inner,
            boundary=boundary,
            timestamp_ns=clock.timestamp_ns,
        )

    @property
    def account_id(self):
        return self._inner.account_id

    @property
    def is_connected(self) -> bool:
        return bool(self._inner.is_connected)

    def connect(self) -> None:
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def submit_order(self, command: Any) -> None:
        self._dispatch.submit_order(command)

    def submit_order_list(self, command: Any) -> None:
        self._dispatch.submit_order_list(command)

    def modify_order(self, command: Any) -> None:
        self._dispatch.modify_order(command)

    def cancel_order(self, command: Any) -> None:
        self._dispatch.cancel_order(command)

    def cancel_all_orders(self, command: Any) -> None:
        self._dispatch.cancel_all_orders(command)

    def batch_cancel_orders(self, command: Any) -> None:
        self._dispatch.batch_cancel_orders(command)

    def query_account(self, command: Any) -> None:
        self._inner.query_account(command)

    def query_order(self, command: Any) -> None:
        self._inner.query_order(command)

    async def generate_order_status_report(self, command: Any):
        return await self._inner.generate_order_status_report(command)

    async def generate_order_status_reports(self, command: Any):
        return await self._inner.generate_order_status_reports(command)

    async def generate_fill_reports(self, command: Any):
        return await self._inner.generate_fill_reports(command)

    async def generate_position_status_reports(self, command: Any):
        return await self._inner.generate_position_status_reports(command)

    async def generate_mass_status(self, lookback_mins: int | None = None):
        return await self._inner.generate_mass_status(lookback_mins)


def guarded_exec_client_factory(
    upstream_factory: type[LiveExecClientFactory],
    boundary: RunnerReservationBoundary,
) -> type[LiveExecClientFactory]:
    """Return a public NT factory subclass which creates the guarded client facade."""

    class _GuardedExecClientFactory(LiveExecClientFactory):
        @staticmethod
        def create(**kwargs: Any) -> GuardedLiveExecutionClient:
            inner = upstream_factory.create(**kwargs)
            boundary.bind_runtime(semantics=NautilusCachedOrderSemantics(kwargs["cache"]))
            return GuardedLiveExecutionClient(
                inner=inner,
                boundary=boundary,
                loop=kwargs["loop"],
                msgbus=kwargs["msgbus"],
                cache=kwargs["cache"],
                clock=kwargs["clock"],
                config=kwargs["config"],
            )

    # NT 1.230.0 injects Sandbox's portfolio argument by factory class name.
    # Preserve the upstream name without mutating the upstream class itself.
    _GuardedExecClientFactory.__name__ = upstream_factory.__name__
    _GuardedExecClientFactory.__qualname__ = f"CustosGuarded{upstream_factory.__name__}"
    return _GuardedExecClientFactory

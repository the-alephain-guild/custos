"""Map fixed-lot strategy signals to Native Papertrade orders before the safety gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nautilus_trader.model import AccountId, OrderSide, OrderType, Quantity, TimeInForce

from custos.core.log import get_logger

_log = get_logger("custos.local_papertrade_orders")


class PapertradeOrderRouting:
    def __init__(
        self,
        strategy: Any,
        cache: Any,
        account_id: AccountId,
        instruments: tuple[str, ...],
        quote_lot: str,
    ) -> None:
        self._strategy = strategy
        self._cache = cache
        self._account_id = account_id
        self._instruments = instruments
        self._quote_lot = Quantity.from_str(quote_lot)

    def wrap(self, submit: Callable[..., bool]) -> Callable[[Any], bool]:
        def route(order: Any) -> bool:
            if (
                order.order_type != OrderType.MARKET
                or order.time_in_force != TimeInForce.IOC
                or order.exec_algorithm_id is not None
                or order.is_quote_quantity
                or str(order.instrument_id) not in self._instruments
                or order.side not in (OrderSide.BUY, OrderSide.SELL)
                or order.quantity.as_decimal() <= 0
            ):
                _log.warning("local_strategy_order_shape_refused")
                return False
            position_id = None
            quantity = self._quote_lot
            if order.is_reduce_only:
                positions = [
                    position
                    for position in self._cache.positions_open(
                        instrument_id=order.instrument_id, account_id=self._account_id
                    )
                    if position.account_id == self._account_id
                    and position.instrument_id == order.instrument_id
                    and position.is_long == (order.side == OrderSide.SELL)
                    and position.quantity.as_decimal() == order.quantity.as_decimal()
                ]
                if len(positions) != 1:
                    _log.warning("local_strategy_close_target_unconfirmed")
                    return False
                position = positions[0]
                position_id, quantity = position.id, position.quantity
            mapped = self._strategy.order_factory.market(
                instrument_id=order.instrument_id,
                order_side=order.side,
                quantity=quantity,
                time_in_force=TimeInForce.GTC,
                reduce_only=order.is_reduce_only,
                quote_quantity=not order.is_reduce_only,
                tags=order.tags,
                client_order_id=order.client_order_id,
            )
            return (
                submit(mapped) if position_id is None else submit(mapped, position_id=position_id)
            )

        return route

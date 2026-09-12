"""Non-bypass runner notional enforcement at the strategy's outbound edge.

1.x put this behind a wrapped execution client -- the last thing an order passed
before the venue, and therefore a seat every command had to cross whatever sent
it. 2.0 has no such seat for python: ``nautilus_trader.execution`` exposes no
execution-client base to subclass, and ``add_exec_client`` resolves its factory
through a rust registry keyed by the factory's own name, so a python-supplied
factory is refused rather than wrapped. The only interception point left is the
strategy's own submit methods.

That is closer to the source than to the venue, which makes the gate's coverage
an argument rather than a structural fact. Three internal paths reach the
execution engine without passing through here; all three are shut or seen:

* the order manager submits contingent and emulated orders itself, which happens
  only when one of ``manage_contingent_orders`` / ``manage_gtd_expiry`` /
  ``manage_stop`` is on. All three default off, and the host refuses a strategy
  whose config turns one on.
* an order carrying ``emulation_trigger`` or ``exec_algorithm_id`` is routed away
  from the ordinary path -- but it still passes here first, so it is refused here.
* ``market_exit()`` hands the exit to nautilus. It is a public method on the
  strategy, so the gate wraps it too and refuses it.

Refusing means the order is simply not submitted. 2.0 adds an order to the cache
and publishes its initialized event *inside* submit, so an order refused here
never existed -- there is no rejected event to generate and no dangling state to
close. The cost is that nautilus will not say it happened, which is why every
refusal is reported to the runner's own fact sink instead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from nautilus_trader.model import PriceType

from custos.core.log import get_logger
from custos.core.order_reservation_boundary import (
    RunnerReservationBoundary,
    RunnerRiskIncreaseFrozenError,
    runner_command_id,
)
from custos.core.runner_fact import RunnerStateAuthorityError
from custos.engines.nautilus.strategy_hooks import StrategyHookUnsupported, install_hook

_log = get_logger("custos.runner_safety")
_POLICY_REJECTION_REASON = "custos_runner_notional_policy_rejected"
_FALLBACK_BREAKER_REJECTION_REASON = "custos_runner_fallback_breaker_frozen"
_SAFETY_BOUNDARY_REJECTION_REASON = "custos_runner_safety_boundary_unavailable"
_CLIENT_ORDER_ID_REJECTION_REASON = "custos_runner_client_order_id_too_long_for_venue"
_ROUTED_AWAY_REJECTION_REASON = "custos_runner_order_would_bypass_the_gate"
_MARKET_EXIT_REJECTION_REASON = "custos_runner_market_exit_bypasses_the_gate"

# The strategy methods the gate wraps. submit_order / submit_order_list / modify_order
# carry risk and are decided on; market_exit is refused outright because nautilus
# performs that exit itself, past this point.
SUBMIT_ORDER = "submit_order"
SUBMIT_ORDER_LIST = "submit_order_list"
MODIFY_ORDER = "modify_order"
MARKET_EXIT = "market_exit"

# Config switches that let the order manager submit on the strategy's behalf, which
# would go around this gate entirely. All three default to False in 2.0.
BYPASS_CONFIG_SWITCHES = (
    "manage_contingent_orders",
    "manage_gtd_expiry",
    "manage_stop",
)


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


def _positive_decimal(value: Any, *, field: str) -> Decimal:
    """A money-path price, which cannot be zero.

    ``_decimal`` accepts zero, which is right for a quantity and wrong for a price:
    a zero price makes every notional zero, and a reservation of zero passes any
    cap there is. Whatever produced it was broken, and reserving against it would
    hide that behind an order that looks free.
    """
    result = _decimal(value, field=field)
    if result <= 0:
        raise RuntimeError(f"{field} must be a positive decimal")
    return result


class NautilusCachedOrderSemantics:
    """Calculate venue-aware notionals from the canonical Nautilus cache."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    def order_notional(self, order: Any) -> Decimal:
        if order.is_quote_quantity:
            return _decimal(order.quantity, field="quote order quantity")
        return self._instrument_notional(
            order.instrument_id,
            order.quantity,
            self._order_price(order),
        )

    def modified_order_notional(self, intent: Any) -> Decimal:
        """The notional a modification would leave in force.

        The requested quantity and price come from the intent and the rest from the
        cached order. Each is taken by asking whether it was given, not by falling
        through a chain of ``or`` -- a modification to quantity zero is a mistake
        worth surfacing, and ``or`` would silently keep the old quantity instead.
        """
        order = self._cache.order(intent.client_order_id)
        if order is None:
            raise RuntimeError("modified order is absent from the canonical Nautilus cache")
        quantity = intent.quantity if intent.quantity is not None else order.quantity
        if order.is_quote_quantity:
            return _decimal(quantity, field="modified quote order quantity")
        if intent.price is not None:
            price = intent.price
        elif intent.trigger_price is not None:
            price = intent.trigger_price
        else:
            price = self._order_price(order)
        return self._instrument_notional(order.instrument_id, quantity, price)

    def fill_notional(self, event: Any) -> Decimal:
        return self._instrument_notional(
            event.instrument_id,
            event.last_qty,
            event.last_px,
        )

    def fill_quantity(self, event: Any) -> Decimal:
        return _decimal(event.last_qty, field="fill quantity")

    def order_is_risk_reducing(self, order: Any) -> bool:
        return bool(order.is_reduce_only)

    def event_is_risk_reducing(self, event: Any) -> bool:
        order = self._cache.order(event.client_order_id)
        return order is not None and self.order_is_risk_reducing(order)

    def event_exposure_source_order_id(self, event: Any) -> str | None:
        position_id = getattr(event, "position_id", None)
        position_reader = getattr(self._cache, "position", None)
        position = (
            position_reader(position_id)
            if position_id is not None and callable(position_reader)
            else None
        )
        opening_order_id = getattr(position, "opening_order_id", None)
        return str(opening_order_id) if opening_order_id is not None else None

    def _order_price(self, order: Any) -> Any:
        """The price to value this order at, from the first source that has one.

        ``getattr`` here is not defensive: 2.0's order types genuinely differ --
        a market order has neither ``price`` nor ``trigger_price``, a limit order has
        only the first, a stop-market only the second (measured, not assumed). What
        is deliberate is testing each source for presence rather than for truth: a
        price of zero is a broken price, and falling through to the next source would
        value the order off something the venue is not using.
        """
        for source in ("price", "trigger_price"):
            declared = getattr(order, source, None)
            if declared is not None:
                return _positive_decimal(declared, field=f"order {source}")

        mark_reader = getattr(self._cache, "mark_price", None)
        mark = mark_reader(order.instrument_id) if callable(mark_reader) else None
        marked = getattr(mark, "value", mark)
        if marked is not None:
            return _positive_decimal(marked, field="order mark price")

        mid = self._cache.price(order.instrument_id, PriceType.MID)
        if mid is None:
            raise RuntimeError("order has no reliable price")
        return _positive_decimal(mid, field="order mid price")

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


@dataclass(frozen=True, slots=True)
class OrderRefusal:
    """One order the gate did not let out, and why.

    Nautilus produces no event for these -- a refused order is never submitted and
    therefore never existed as far as it is concerned -- so this is the only record
    that the refusal happened.
    """

    client_order_id: str
    instrument_id: str
    side: str
    reason_code: str


class _SubmitIntent:
    """One order the strategy is trying to send, in the shape the boundary reads.

    The reservation boundary was written against nautilus command objects. At the
    strategy edge there is no command yet -- it is built inside submit, past this
    point -- so this carries the same three things the boundary asks of one:
    the order, an identity to key the reservation by, and nothing else.
    """

    __slots__ = ("id", "order")

    def __init__(self, order: Any) -> None:
        self.order = order
        self.id = uuid4()


class _SubmitListIntent:
    __slots__ = ("id", "order_list")

    def __init__(self, order_list: Any) -> None:
        self.order_list = order_list
        self.id = uuid4()


class _ModifyIntent:
    __slots__ = ("client_order_id", "id", "price", "quantity", "trigger_price")

    def __init__(self, order: Any, quantity: Any, price: Any, trigger_price: Any) -> None:
        self.client_order_id = order.client_order_id
        self.quantity = quantity
        self.price = price
        self.trigger_price = trigger_price
        self.id = uuid4()


class RunnerSafetyOrderGate:
    """Decides whether an order the strategy is sending may leave.

    Refusal is a return, not an exception: the strategy called a nautilus method
    that returns None, and raising into it would break strategies that are doing
    nothing wrong. What makes a refusal visible is the fact sink, not the call.
    """

    def __init__(
        self,
        *,
        boundary: RunnerReservationBoundary,
        client_order_id_len_limit: int | None,
        on_refusal: Callable[[OrderRefusal], None] | None = None,
    ) -> None:
        self._boundary = boundary
        # Required rather than defaulted: the cap belongs to the venue this deployment
        # trades on, and both possible defaults are a claim about a venue -- one venue's
        # measured number applied to another, or no cap at all.
        self._client_order_id_len_limit = client_order_id_len_limit
        self._on_refusal = on_refusal

    def submit_order(self, submit: Callable[..., None], order: Any, *args: Any, **kwargs: Any):
        refusal = self._pre_trade_refusal(order)
        if refusal is not None:
            self._refuse((order,), refusal)
            return None
        intent = _SubmitIntent(order)
        try:
            reservations = self._boundary.before_submit_order(intent)
        except Exception as exc:  # noqa: BLE001 - every refusal reason is reported below
            self._refuse((order,), self._reservation_refusal_reason(exc), exc=exc)
            return None
        try:
            return submit(order, *args, **kwargs)
        except Exception:
            self._boundary.rollback_submit(reservations, command_id=runner_command_id(intent))
            raise

    def submit_order_list(
        self,
        submit: Callable[..., None],
        order_list: Any,
        *args: Any,
        **kwargs: Any,
    ):
        orders = tuple(order_list.orders)
        # One unusable leg fails the list: the venue would refuse that leg and leave
        # the rest as an unintended partial structure.
        for order in orders:
            refusal = self._pre_trade_refusal(order)
            if refusal is not None:
                self._refuse(orders, refusal)
                return None
        intent = _SubmitListIntent(order_list)
        try:
            reservations = self._boundary.before_submit_order_list(intent)
        except Exception as exc:  # noqa: BLE001 - every refusal reason is reported below
            self._refuse(orders, self._reservation_refusal_reason(exc), exc=exc)
            return None
        try:
            return submit(order_list, *args, **kwargs)
        except Exception:
            self._boundary.rollback_submit(reservations, command_id=runner_command_id(intent))
            raise

    def modify_order(
        self,
        modify: Callable[..., None],
        order: Any,
        quantity: Any = None,
        price: Any = None,
        trigger_price: Any = None,
        *args: Any,
        **kwargs: Any,
    ):
        intent = _ModifyIntent(order, quantity, price, trigger_price)
        try:
            modification = self._boundary.before_modify_order(intent)
        except Exception as exc:  # noqa: BLE001 - every refusal reason is reported below
            # Symmetric with the submit path, which has always reported its refusals;
            # this one used to fall through to a nautilus rejection event and say
            # nothing itself, and there is no such event any more.
            self._refuse((order,), self._reservation_refusal_reason(exc), exc=exc)
            return None
        try:
            return modify(order, quantity, price, trigger_price, *args, **kwargs)
        except Exception:
            self._boundary.rollback_modify(modification, event_id=runner_command_id(intent))
            raise

    def market_exit(self, _exit: Callable[..., None], *_args: Any, **_kwargs: Any):
        """Refuse the exit nautilus would perform on the strategy's behalf.

        Everything it submits is built past this gate, so allowing the call would
        put orders on the venue that no reservation covers. Flattening goes through
        the host's shutdown policy, which submits through the ordinary path.
        """
        _log.warning(
            "runner_market_exit_refused",
            reason_code=_MARKET_EXIT_REJECTION_REASON,
        )
        self._report(
            OrderRefusal(
                client_order_id="",
                instrument_id="",
                side="",
                reason_code=_MARKET_EXIT_REJECTION_REASON,
            )
        )
        return None

    def _pre_trade_refusal(self, order: Any) -> str | None:
        """The reasons that can be read off the order alone, before any reservation."""
        if self._client_order_id_too_long(order):
            return _CLIENT_ORDER_ID_REJECTION_REASON
        if self._would_be_routed_away(order):
            return _ROUTED_AWAY_REJECTION_REASON
        return None

    @staticmethod
    def _would_be_routed_away(order: Any) -> bool:
        """Whether nautilus would hand this order to the emulator or an algorithm.

        Either one resubmits it from inside the engine, where this gate is not, so
        the reservation taken here would stop describing what is actually working.
        """
        return (
            getattr(order, "emulation_trigger", None) is not None
            or getattr(order, "exec_algorithm_id", None) is not None
        )

    def _client_order_id_too_long(self, order: Any) -> bool:
        """Refuse an id the venue will refuse, before it costs a round trip.

        The id's shape is chosen where the strategy config is built, and nothing after
        construction can change it. That makes the config a convention rather than an
        invariant: a signed artifact whose adapter builds its own config, or a strategy
        passing an explicit client_order_id, reaches the venue without ever consulting
        that builder. Both would reproduce the -4015 rejection of every order while
        every test about the builder stayed green.

        The cap comes from the venue module for the connector this deployment trades
        on. A venue that has none measured against it declares ``None``, and this
        refuses nothing there: a cap carried over from another exchange would be a
        claim about a venue nobody has asked.
        """
        if self._client_order_id_len_limit is None:
            return False
        return len(str(order.client_order_id)) >= self._client_order_id_len_limit

    def _refuse(self, orders: tuple, reason_code: str, *, exc: Exception | None = None) -> None:
        _log.warning(
            "runner_order_refused",
            reason_code=reason_code,
            order_count=len(orders),
            error_type=type(exc).__name__ if exc is not None else None,
        )
        for order in orders:
            self._report(
                OrderRefusal(
                    client_order_id=str(getattr(order, "client_order_id", "")),
                    instrument_id=str(getattr(order, "instrument_id", "")),
                    side=_order_side(order),
                    reason_code=reason_code,
                )
            )

    def _report(self, refusal: OrderRefusal) -> None:
        if self._on_refusal is None:
            return
        try:
            self._on_refusal(refusal)
        except Exception:  # noqa: BLE001 - a refusal must not be undone by its own reporting
            _log.error(
                "runner_order_refusal_unreported",
                reason_code=refusal.reason_code,
            )

    @staticmethod
    def _reservation_refusal_reason(exc: Exception) -> str:
        if isinstance(exc, RunnerStateAuthorityError):
            return _POLICY_REJECTION_REASON
        if isinstance(exc, RunnerRiskIncreaseFrozenError):
            return _FALLBACK_BREAKER_REJECTION_REASON
        return _SAFETY_BOUNDARY_REJECTION_REASON


def _order_side(order: Any) -> str:
    raw = str(getattr(order, "side", "") or getattr(order, "order_side", "")).lower()
    return raw.rpartition(".")[2]


def require_no_bypass_config(strategy: Any) -> None:
    """Refuse a strategy configured to let nautilus submit on its behalf.

    With any of these on, the order manager submits contingent or emulated orders
    from inside the engine, which never passes the gate. They default off, so this
    is a check rather than a restriction -- but it is the check that lets the gate's
    coverage be stated at all.
    """
    config = getattr(strategy, "config", None)
    enabled = [switch for switch in BYPASS_CONFIG_SWITCHES if bool(getattr(config, switch, False))]
    if enabled:
        raise StrategyHookUnsupported(
            f"strategy {type(strategy).__name__} enables {', '.join(enabled)}, which lets "
            "nautilus submit orders past the runner's safety gate"
        )


def install_order_gate(strategy: Any, gate: RunnerSafetyOrderGate) -> None:
    """Put the gate in front of every way this strategy can reach the venue."""
    require_no_bypass_config(strategy)
    for method_name, decide in (
        (SUBMIT_ORDER, gate.submit_order),
        (SUBMIT_ORDER_LIST, gate.submit_order_list),
        (MODIFY_ORDER, gate.modify_order),
        (MARKET_EXIT, gate.market_exit),
    ):
        install_hook(
            strategy,
            method_name,
            lambda original, _decide=decide: (
                lambda *args, **kwargs: _decide(original, *args, **kwargs)
            ),
        )
    _log.info(
        "runner_order_gate_installed",
        strategy=type(strategy).__name__,
        methods=[SUBMIT_ORDER, SUBMIT_ORDER_LIST, MODIFY_ORDER, MARKET_EXIT],
    )

"""Give a strategy double the trading scope a real strategy config carries.

The host refuses a strategy that does not declare what it trades, so doubles that
stand in for a deployable strategy declare it the way the toolkit's config does:
``config.trading`` with connector, pairs and leverage, and
``config.external_order_instrument_ids`` with the instruments it claims.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace


def declare_trading_scope(
    strategy: object,
    *,
    connector: str,
    pairs: Iterable[str],
    leverage: int,
    instrument_ids: Iterable[str],
) -> object:
    from nautilus_trader.model import InstrumentId

    strategy.config = SimpleNamespace(  # type: ignore[attr-defined]
        trading=SimpleNamespace(connector=connector, pairs=tuple(pairs), leverage=leverage),
        external_order_instrument_ids=[InstrumentId.from_str(i) for i in instrument_ids],
    )
    return strategy


def declare_trading_scope_of(strategy: object, spec: dict) -> object:
    """Declare exactly the scope ``spec`` authorizes, as the venue module derives it."""
    from custos.engines.nautilus.host import _venue_module_for

    venue = _venue_module_for(str(spec["connector"]))
    return declare_trading_scope(
        strategy,
        connector=str(spec["connector"]),
        pairs=spec["pairs"],
        leverage=int(spec["leverage"]),
        instrument_ids=venue.build_instrument_id_strings(spec),
    )

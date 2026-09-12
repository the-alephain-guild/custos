"""Which venue a connector trades on, and which module builds its client configs.

Deliberately free of NautilusTrader imports. Three callers need these answers on a
base install where the nautilus extra is absent: execution admission asks whether a
connector is supported at all, the sandbox RunnerFact host has to name the venue its
facts are about, and the host resolves the venue module before importing it.

The venue names duplicate constants the adapters define (``BINANCE_VENUE``,
``SODEX_SPOT``, ``SODEX_PERPS``). That duplication is the price of staying
importable without NautilusTrader, and it is held in place by a drift-guard test
rather than by care -- see ``tests/test_nt_venue_wiring.py``.
"""

from __future__ import annotations

__all__ = [
    "VENUE_BY_CONNECTOR",
    "VENUE_MODULE_BY_CONNECTOR",
    "venue_for_connector",
    "venue_module_name_for",
]

# connector (spec.connector) -> the venue it trades on. This is the name that reaches
# the wire: signed RunnerFacts carry it, and the fill event ids are scoped under it,
# so it is a claim about where the order went and not a display label.
VENUE_BY_CONNECTOR: dict[str, str] = {
    "binance": "BINANCE",
    "binance_perpetual": "BINANCE",
    # Spot and perps are separate venues on SoDEX rather than one venue with a
    # product type: they differ in signing domain, API key set, balances and
    # reference price, and the adapter models them apart for that reason.
    "sodex": "SODEX_SPOT",
    "sodex_perpetual": "SODEX_PERPS",
}

# connector -> the module that turns a spec into that venue's NT client configs.
VENUE_MODULE_BY_CONNECTOR: dict[str, str] = {
    "binance": "venue_binance",
    "binance_perpetual": "venue_binance",
    "sodex": "venue_sodex",
    "sodex_perpetual": "venue_sodex",
}


def venue_module_name_for(connector: str) -> str:
    """The venue module for a connector, rejecting one this runner has no wiring for."""
    module_name = VENUE_MODULE_BY_CONNECTOR.get(connector.lower())
    if module_name is None:
        raise NotImplementedError(
            f"connector {connector!r} has no venue wiring in this runner "
            f"(wired: {', '.join(sorted(VENUE_MODULE_BY_CONNECTOR))})"
        )
    return module_name


def venue_for_connector(connector: str) -> str:
    """The venue a connector trades on, rejecting one this runner has no wiring for."""
    venue = VENUE_BY_CONNECTOR.get(connector.lower())
    if venue is None:
        raise NotImplementedError(
            f"connector {connector!r} has no venue wiring in this runner "
            f"(wired: {', '.join(sorted(VENUE_BY_CONNECTOR))})"
        )
    return venue

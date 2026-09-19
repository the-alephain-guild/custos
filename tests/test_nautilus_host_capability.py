"""Host capability declarations used by execution admission.

SandboxSimulationHost accepts only sandbox while preserving the canonical
connector identity it simulates. The real NtTradingNodeHost declares the same
Binance connectors it actually wires.

No NautilusTrader dependency: capability answers are static declarations, so
they are unit-testable on a base install. A separate NT-gated drift guard
asserts the declared venue set stays in sync with the venue-config module.
"""

from __future__ import annotations

from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost


def test_sandbox_simulation_host_rejects_live_capability() -> None:
    # Fail-safe: the stub must never claim live capability, or the gate would
    # let a paper stub silently swallow live orders.
    assert SandboxSimulationHost().supports_trading_mode("live") is False


def test_sandbox_simulation_host_accepts_sandbox_capability() -> None:
    assert SandboxSimulationHost().supports_trading_mode("sandbox") is True


def test_sandbox_simulation_host_accepts_canonical_connectors_without_pseudo_venue() -> None:
    host = SandboxSimulationHost()
    assert host.supports_venue("SIM", "sandbox") is False
    assert host.supports_venue("binance", "sandbox") is True
    assert host.supports_venue("binance_perpetual", "sandbox") is True
    assert host.supports_venue("unsupported", "sandbox") is False


def test_ntlivehost_declares_live() -> None:
    assert NtTradingNodeHost().supports_trading_mode("live") is True


def test_ntlivehost_venue_binance_supported() -> None:
    host = NtTradingNodeHost()
    assert host.supports_venue("binance", "live") is True
    assert host.supports_venue("binance_perpetual", "live") is True


def test_ntlivehost_venue_case_insensitive() -> None:
    # Connector strings arrive from the wire; capability check is case-folded so
    # "BINANCE" and "binance" both resolve (mirrors the gate's mode folding).
    assert NtTradingNodeHost().supports_venue("BINANCE", "live") is True


def test_ntlivehost_venue_unknown_rejected() -> None:
    host = NtTradingNodeHost()
    assert host.supports_venue("unsupported", "live") is False
    assert host.supports_venue("unsupported_perpetual", "live") is False


def test_sodex_runs_in_sandbox_and_testnet() -> None:
    host = NtTradingNodeHost()
    for mode in ("sandbox", "testnet"):
        assert host.supports_venue("sodex", mode) is True
        assert host.supports_venue("sodex_perpetual", mode) is True


def test_all_six_venue_products_have_explicit_live_host_capability() -> None:
    host = NtTradingNodeHost()
    for connector in (
        "binance",
        "binance_perpetual",
        "sodex",
        "sodex_perpetual",
        "okx",
        "okx_perpetual",
    ):
        assert host.supports_venue(connector, "live") is True


def test_the_simulation_host_refuses_every_real_venue_mode_at_the_mode_gate() -> None:
    """Red line 0.2: only NtTradingNodeHost may reach a real venue.

    The simulation host shares the venue allow-list, so asking it about a venue in
    live says yes -- and that is fine, because admission asks about the mode first
    (``engine_lifecycle._require_authorized_runtime``) and this host answers no to
    everything but sandbox. Asserted here on the gate that actually holds, rather
    than on the one that happens to be shared.
    """
    host = SandboxSimulationHost()
    assert host.supports_trading_mode("sandbox") is True
    assert host.supports_trading_mode("testnet") is False
    assert host.supports_trading_mode("live") is False


def test_the_live_set_did_not_shrink() -> None:
    """Adding SoDEX must not cost Binance anything."""
    from custos.engines.nautilus.host import _LIVE_VENUES

    assert {"binance", "binance_perpetual"} <= _LIVE_VENUES

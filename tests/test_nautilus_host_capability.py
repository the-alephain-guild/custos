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
    assert host.supports_venue("SIM") is False
    assert host.supports_venue("binance") is True
    assert host.supports_venue("binance_perpetual") is True
    assert host.supports_venue("okx") is False


def test_ntlivehost_declares_live() -> None:
    assert NtTradingNodeHost().supports_trading_mode("live") is True


def test_ntlivehost_venue_binance_supported() -> None:
    host = NtTradingNodeHost()
    assert host.supports_venue("binance") is True
    assert host.supports_venue("binance_perpetual") is True


def test_ntlivehost_venue_case_insensitive() -> None:
    # Connector strings arrive from the wire; capability check is case-folded so
    # "BINANCE" and "binance" both resolve (mirrors the gate's mode folding).
    assert NtTradingNodeHost().supports_venue("BINANCE") is True


def test_ntlivehost_venue_unknown_rejected() -> None:
    host = NtTradingNodeHost()
    assert host.supports_venue("okx") is False
    assert host.supports_venue("okx_perpetual") is False

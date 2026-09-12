"""Engine dispatch through ``_daemon._build_host``.

Post-Plan-11 the flat parser is gone; the reconciler wire lives in
``custos.cli._daemon``. These tests exercise ``_build_host``'s engine
selection directly without a legacy parser hop.
"""

from __future__ import annotations

import argparse

import pytest

from custos.cli._daemon import _build_host
from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost


def _ns(engine: str = "nautilus") -> argparse.Namespace:
    return argparse.Namespace(
        tenant_id="t",
        runner_id="r",
        engine=engine,
    )


def test_cli_engine_defaults_to_nautilus() -> None:
    args = _ns()
    assert args.engine == "nautilus"
    host = _build_host(args)
    assert isinstance(host, NtTradingNodeHost)


def test_the_daemon_keeps_its_own_signal_handlers() -> None:
    """The host used to be handed the daemon's stop callback to reinstall.

    1.x's TradingNode installed its own SIGINT/SIGTERM handlers during
    construction, displacing the daemon's, so the host put them back. 2.0's
    ``run_async`` runs the node hosted and installs no signal handlers at all, so
    the daemon's own handlers -- registered on the loop before any node exists --
    stay in force and the host has no part in it.
    """
    import inspect

    from custos.engines.nautilus.host import NtTradingNodeHost as _Host

    assert "process_shutdown_requested" not in inspect.signature(_Host).parameters
    assert "process_shutdown_requested" not in inspect.signature(_build_host).parameters


def test_cli_engine_noop_is_explicit() -> None:
    assert isinstance(_build_host(_ns(engine="sandbox-sim")), SandboxSimulationHost)


def test_cli_engine_unknown_rejected() -> None:
    args = _ns(engine="hummingbot")
    with pytest.raises(SystemExit, match="not available"):
        _build_host(args)

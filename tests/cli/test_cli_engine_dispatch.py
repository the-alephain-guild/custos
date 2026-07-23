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


def test_cli_engine_noop_is_explicit() -> None:
    assert isinstance(_build_host(_ns(engine="sandbox-sim")), SandboxSimulationHost)


def test_cli_engine_unknown_rejected() -> None:
    args = _ns(engine="hummingbot")
    with pytest.raises(SystemExit, match="not available"):
        _build_host(args)

"""Runner runtime host selection.

The lifecycle supervisor binds a single host from the clean-break ``--engine`` enum.
``nautilus`` selects the real ``NtTradingNodeHost`` and ``sandbox-sim`` selects the
explicit contract-test stub. Execution admission still guards every live deploy.

After the CLI package split: ``_build_host`` lives in ``custos.cli._daemon`` (the flat
``custos.cli.main`` module was retired). Tests build the ``Namespace``
directly rather than routing through a legacy parser.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from custos.cli._daemon import _build_host
from custos.engines.nautilus import host as nautilus_host


def _host_args(*, engine: str = "nautilus") -> argparse.Namespace:
    return argparse.Namespace(
        tenant_id="acme",
        runner_id="r-1",
        engine=engine,
    )


def test_build_host_defaults_to_nautilus() -> None:
    assert type(_build_host(_host_args())).__name__ == "NtTradingNodeHost"


def test_build_host_uses_runner_fact_sandbox_when_explicit() -> None:
    assert type(_build_host(_host_args(engine="sandbox-sim"))).__name__ == "SandboxRunnerFactHost"


@pytest.mark.asyncio
async def test_build_host_nt_without_runtime_fails_fast(monkeypatch) -> None:
    # The nautilus engine selects the real host; if the runtime is absent it must fail
    # fast on deploy rather than silently doing nothing (no stub fallback).
    monkeypatch.setattr(nautilus_host, "TradingNode", None)
    host = _build_host(_host_args())
    with pytest.raises(RuntimeError, match="nautilus"):
        await host.deploy(
            {"deployment_instance_id": "x", "deployment_spec_id": "spec-x"},
            {},
            SimpleNamespace(activation_id="activation-x", strategy=object()),
        )

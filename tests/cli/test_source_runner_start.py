"""The signed daemon opts into one bounded source subscription explicitly."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.cli import _daemon
from custos.cli.subcommands import _build_parser
from custos.cli.subcommands.start import _source_runner_options

INSTANCE = UUID("20000000-0000-4000-8000-000000000001")
RUNNER = UUID("10000000-0000-4000-8000-000000000001")


def parsed(*extra: str) -> argparse.Namespace:
    return _build_parser().parse_args(["start", "--reconcile", "--enabled-mode", "testnet", *extra])


def source_flags(tmp_path: Path) -> tuple[str, ...]:
    return (
        "--external-source-instance-id",
        str(INSTANCE),
        "--external-source-authorization",
        str(tmp_path / "source.json"),
        "--external-source-nats-credentials",
        str(tmp_path / "source.creds"),
        "--nats-sim-url",
        "nats://broker.example:4222",
        "--nats-sim-server-name",
        "broker.example",
        "--nats-sim-ca",
        str(tmp_path / "ca.pem"),
    )


def test_default_start_has_no_source_and_complete_flags_build_one_source(tmp_path: Path) -> None:
    assert _source_runner_options(parsed()) is None
    args = parsed(*source_flags(tmp_path))
    options = _source_runner_options(args)
    assert options is not None
    assert options.instance_id == INSTANCE
    assert options.authorization_path == tmp_path / "source.json"
    assert options.credentials_path == tmp_path / "source.creds"
    assert options.inbox_path == args.runner_fact_outbox.parent / f"external-source-{INSTANCE}.db"
    assert options.nats_url == "nats://broker.example:4222"


@pytest.mark.parametrize(
    "extra",
    [
        ("--external-source-instance-id", str(INSTANCE)),
        ("--enabled-mode", "live"),
        ("--development-local-nats-url", "nats://127.0.0.1:4222"),
        ("--engine", "sandbox-sim"),
    ],
)
def test_source_flags_refuse_partial_or_weakened_signed_lane(tmp_path: Path, extra) -> None:
    base = source_flags(tmp_path)
    if extra[0] == "--external-source-instance-id":
        args = parsed(*extra)
    else:
        args = parsed(*base, *extra)
    with pytest.raises(ValueError, match="external source"):
        _source_runner_options(args)


def test_direct_daemon_call_cannot_silently_ignore_source_options() -> None:
    base = dict(
        source_runner_options=SimpleNamespace(instance_id=INSTANCE),
        reconcile=True,
        enabled_modes=("testnet",),
        engine="nautilus",
        development_local_nats_url="",
    )
    _daemon._require_source_runner_lane(SimpleNamespace(**base))
    for changes in (
        {"reconcile": False},
        {"enabled_modes": ("testnet", "live")},
        {"engine": "sandbox-sim"},
        {"development_local_nats_url": "nats://127.0.0.1:4222"},
    ):
        with pytest.raises(RuntimeError, match="external source"):
            _daemon._require_source_runner_lane(SimpleNamespace(**{**base, **changes}))


def test_production_state_root_contains_source_authority_and_credentials(tmp_path: Path) -> None:
    args = parsed(*source_flags(tmp_path))
    root = tmp_path / "state-root"
    root.mkdir()
    args.production_state_root = root
    with pytest.raises(ValueError, match="production state root"):
        _source_runner_options(args)


@pytest.mark.asyncio
async def test_daemon_source_task_uses_separate_runtime_and_reports_state(monkeypatch) -> None:
    states = []
    stop = asyncio.Event()
    host = object()
    options = SimpleNamespace(instance_id=INSTANCE)

    class Runtime:
        def __init__(self, *, options, tenant_id, runner_id, host, report):
            assert options.instance_id == INSTANCE
            assert tenant_id == "tenant-a" and runner_id == RUNNER
            assert host is host_ref
            self.report = report

        async def run(self, received_stop):
            assert received_stop is stop
            self.report("active", None)
            self.report("fault", "authorization_invalid")
            received_stop.set()

    host_ref = host
    monkeypatch.setattr(
        "custos.engines.nautilus.external_source_runner_runtime.ExternalSourceRunnerRuntime",
        Runtime,
    )
    monkeypatch.setattr(
        _daemon._slog, "info", lambda event, **fields: states.append((event, fields))
    )
    monkeypatch.setattr(
        _daemon._slog, "warning", lambda event, **fields: states.append((event, fields))
    )
    await _daemon._run_external_source_subscription(
        stop, options=options, tenant_id="tenant-a", runner_id=RUNNER, host=host
    )
    assert states[0][0] == "external_source_subscription_active"
    assert states[1][0] == "external_source_subscription_fault"
    assert states[1][1]["reason_code"] == "authorization_invalid"

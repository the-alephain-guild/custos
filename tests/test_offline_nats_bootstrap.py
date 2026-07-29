"""``arx-runner nats bootstrap`` creates the offline lane's own JetStream topology.

The operator owns this NATS instance, but owning it is not the same as it being
empty. Bootstrap therefore reconciles only streams it can prove are its own, and
refuses rather than reshapes anything else it finds under those names.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nats.js.errors import NotFoundError

from custos.cli.subcommands import main
from custos.offline.transport import (
    bootstrap_standalone_streams,
    ensure_standalone_streams,
    standalone_stream_configs,
)

TENANT = "local"


class _FakeJetStream:
    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.existing: dict[str, Any] = dict(existing or {})
        self.added: list[Any] = []
        self.updated: list[Any] = []

    async def stream_info(self, name: str) -> SimpleNamespace:
        if name not in self.existing:
            raise NotFoundError
        return SimpleNamespace(config=self.existing[name])

    async def add_stream(self, config: Any) -> None:
        self.added.append(config)
        self.existing[config.name] = config

    async def update_stream(self, config: Any) -> None:
        self.updated.append(config)
        self.existing[config.name] = config


class _FakeConnection:
    def __init__(self, jetstream: _FakeJetStream) -> None:
        self._jetstream = jetstream
        self.drained = False

    def jetstream(self) -> _FakeJetStream:
        return self._jetstream

    async def drain(self) -> None:
        self.drained = True


def test_topology_covers_desired_and_observed_state() -> None:
    desired, observed = standalone_stream_configs(TENANT)

    assert desired.subjects == [f"arx.{TENANT}.deployment_spec.>"]
    assert f"arx.{TENANT}.deployment_status.>" in (observed.subjects or [])
    assert desired.max_msgs_per_subject == 1


def test_stream_names_are_scoped_to_the_tenant() -> None:
    ours = {config.name for config in standalone_stream_configs(TENANT)}
    theirs = {config.name for config in standalone_stream_configs("other-tenant")}

    assert ours.isdisjoint(theirs)


@pytest.mark.parametrize("tenant", ["", "../escape", "a" * 65, "tenant with spaces", "t\x00"])
def test_refuses_a_tenant_that_cannot_address_a_subject(tenant: str) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        standalone_stream_configs(tenant)


async def test_creates_both_streams_when_absent() -> None:
    jetstream = _FakeJetStream()

    await ensure_standalone_streams(jetstream, TENANT)

    assert {config.name for config in jetstream.added} == {
        config.name for config in standalone_stream_configs(TENANT)
    }


async def test_is_idempotent_across_repeated_runs() -> None:
    jetstream = _FakeJetStream()
    await ensure_standalone_streams(jetstream, TENANT)
    jetstream.added.clear()

    await ensure_standalone_streams(jetstream, TENANT)

    assert jetstream.added == []
    assert jetstream.updated == []


async def test_reconciles_drift_on_a_stream_it_owns() -> None:
    desired, _ = standalone_stream_configs(TENANT)
    drifted = standalone_stream_configs(TENANT)[0]
    drifted.subjects = [f"arx.{TENANT}.deployment_spec.stale"]
    jetstream = _FakeJetStream({desired.name: drifted})

    await ensure_standalone_streams(jetstream, TENANT)

    assert [config.name for config in jetstream.updated] == [desired.name]


async def test_refuses_a_stream_it_does_not_own() -> None:
    desired, _ = standalone_stream_configs(TENANT)
    someone_elses = standalone_stream_configs(TENANT)[0]
    someone_elses.metadata = {"owner": "someone-else"}
    jetstream = _FakeJetStream({desired.name: someone_elses})

    with pytest.raises(RuntimeError, match="not owned"):
        await ensure_standalone_streams(jetstream, TENANT)

    assert jetstream.updated == []


async def test_bootstrap_drains_the_connection_it_opened() -> None:
    connection = _FakeConnection(_FakeJetStream())

    async def connect(url: str) -> _FakeConnection:
        return connection

    await bootstrap_standalone_streams(
        nats_url="nats://nats:4222", tenant_id=TENANT, connect_factory=connect
    )

    assert connection.drained


async def test_bootstrap_retries_until_nats_accepts_the_connection() -> None:
    connection = _FakeConnection(_FakeJetStream())
    attempts = 0

    async def connect(url: str) -> _FakeConnection:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError("not up yet")
        return connection

    await bootstrap_standalone_streams(
        nats_url="nats://nats:4222", tenant_id=TENANT, connect_factory=connect
    )

    assert attempts == 3


async def test_bootstrap_gives_up_with_a_named_timeout() -> None:
    async def never(url: str) -> _FakeConnection:
        raise ConnectionRefusedError("still down")

    with pytest.raises(TimeoutError, match="nats://nats:4222"):
        await bootstrap_standalone_streams(
            nats_url="nats://nats:4222",
            tenant_id=TENANT,
            timeout_secs=0.05,
            connect_factory=never,
        )


def test_the_consumer_bootstrap_invocation_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`deploy/custos/docker-compose.yaml` nats-bootstrap passes exactly these flags."""

    seen: dict[str, Any] = {}

    async def fake_bootstrap(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("custos.cli.subcommands.nats.bootstrap_standalone_streams", fake_bootstrap)

    exit_code = main(
        [
            "nats",
            "bootstrap",
            "--profile",
            "standalone",
            "--nats-url",
            "nats://nats:4222",
            "--tenant-id",
            TENANT,
        ]
    )

    assert exit_code == 0
    assert seen["nats_url"] == "nats://nats:4222"
    assert seen["tenant_id"] == TENANT


def test_bootstrap_reports_infrastructure_failure_as_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(**kwargs: Any) -> None:
        raise TimeoutError("nats never became ready")

    monkeypatch.setattr("custos.cli.subcommands.nats.bootstrap_standalone_streams", failing)

    exit_code = main(["nats", "bootstrap", "--profile", "standalone", "--tenant-id", TENANT])

    assert exit_code == 1
    assert "nats never became ready" in capsys.readouterr().err


def test_bootstrap_refuses_an_unknown_profile() -> None:
    with pytest.raises(SystemExit):
        main(["nats", "bootstrap", "--profile", "cloud", "--tenant-id", TENANT])

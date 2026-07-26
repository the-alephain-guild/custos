"""Real-JetStream RunnerFact outbox and PubAck acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import nats
import pytest

from custos.core.nats_transport import DevelopmentLocalNatsConnectionProfile
from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
)

_IMAGE = os.environ.get("CUSTOS_NATS_TEST_IMAGE", "nats:2.10-alpine")
_ENABLED = os.environ.get("CUSTOS_RUN_REAL_RUNNER_FACT_PUBLICATION") == "1"
_TENANT_ID = "acme"
_RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
_DEPLOYMENT_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
_DEPLOYMENT_SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
_STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
_CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
_SPEC_DIGEST = "a" * 64
_CAPABILITY_DIGEST = "b" * 64
_KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"
_STREAM = "CRUCIBLE_RUNNER_FACT_SIM_V1"


def _require_local_gate() -> None:
    if not _ENABLED:
        pytest.skip(
            "set CUSTOS_RUN_REAL_RUNNER_FACT_PUBLICATION=1 "
            "to run the real RunnerFact publication gate"
        )
    if shutil.which("docker") is None:
        pytest.fail("docker is required by the real RunnerFact publication gate")
    inspected = subprocess.run(
        ["docker", "image", "inspect", _IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        pytest.fail(
            f"immutable test image {_IMAGE} is unavailable; preload it before running the gate"
        )


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _wait_ready(container: str) -> None:
    for _ in range(50):
        logs = _run("docker", "logs", container, check=False)
        if "Server is ready" in f"{logs.stdout}\n{logs.stderr}":
            return
        time.sleep(0.1)
    logs = _run("docker", "logs", container, check=False)
    pytest.fail(f"NATS did not become ready:\n{logs.stdout}\n{logs.stderr}")


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id=_TENANT_ID,
        trading_mode="sandbox",
        runner_id=_RUNNER_ID,
        deployment_instance_id=_DEPLOYMENT_INSTANCE_ID,
        deployment_spec_id=_DEPLOYMENT_SPEC_ID,
        deployment_spec_digest=_SPEC_DIGEST,
        generation=1,
        strategy_id=_STRATEGY_ID,
        capability_version_id=_CAPABILITY_VERSION_ID,
        capability_version=1,
        capability_manifest_digest=_CAPABILITY_DIGEST,
    )


async def _exercise_publication(nats_url: str, database: Path) -> None:
    subject = f"crucible.runner.fact.v1.{_TENANT_ID}.{_RUNNER_ID}.sandbox"
    admin = await nats.connect(servers=[nats_url], name="runner-fact-acceptance-admin")
    jetstream = admin.jetstream()
    await jetstream.add_stream(name=_STREAM, subjects=[subject])

    authority = _authority()
    outbox = RunnerFactOutbox(database)
    identity = RunnerFactIdentity.from_private_bytes(
        bytes(range(1, 33)),
        _KEY_ID,
    )
    fact = RunnerDeploymentLifecycleFact.observed(
        authority,
        generation=1,
        lifecycle_state="running",
        command_fingerprint=_SPEC_DIGEST,
        outcome="applied",
    ).to_wire()
    batch_id = await outbox.enqueue(authority, identity, [fact])
    assert batch_id is not None
    pending = await outbox.pending()
    assert len(pending) == 1

    profile = DevelopmentLocalNatsConnectionProfile(
        tenant_id=_TENANT_ID,
        runner_id=_RUNNER_ID,
        nats_url=nats_url,
    )
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": profile},
        outbox=outbox,
        runner_id=_RUNNER_ID,
        authority_guard=lambda: None,
    )
    try:
        assert await publisher.drain_once() == 1
        assert await outbox.pending() == []

        subscription = await jetstream.pull_subscribe(
            subject,
            durable="runner-fact-local-acceptance",
            stream=_STREAM,
        )
        messages = await subscription.fetch(1, timeout=2)
        assert len(messages) == 1
        message = messages[0]
        document = json.loads(message.data)
        assert document["batch_id"] == str(batch_id)
        assert document["tenant_id"] == _TENANT_ID
        assert document["runner_id"] == str(_RUNNER_ID)
        assert document["deployment_instance_id"] == str(_DEPLOYMENT_INSTANCE_ID)
        assert document["generation"] == 1
        assert message.headers is not None
        assert message.headers["Nats-Msg-Id"] == str(batch_id)
        await message.ack()

        stream = await jetstream.stream_info(_STREAM)
        assert stream.state.messages == 1
        assert await RunnerFactOutbox(database).pending() == []
    finally:
        await publisher.close()
        await admin.close()


@pytest.mark.integration
@pytest.mark.docker
def test_real_jetstream_puback_clears_only_the_published_runner_fact(
    tmp_path: Path,
) -> None:
    _require_local_gate()
    container = f"custos-runner-fact-{uuid4().hex}"
    started = _run(
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        container,
        "-p",
        "127.0.0.1::4222",
        _IMAGE,
        "-js",
    )
    assert started.stdout.strip()
    try:
        _wait_ready(container)
        port_output = _run("docker", "port", container, "4222/tcp").stdout.strip()
        port = int(port_output.rsplit(":", 1)[1])
        asyncio.run(
            _exercise_publication(
                f"nats://127.0.0.1:{port}",
                tmp_path / "runner-fact-outbox.sqlite3",
            )
        )
    finally:
        _run("docker", "stop", "-t", "1", container, check=False)

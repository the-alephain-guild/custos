"""Real-JetStream RunnerFact outbox and PubAck acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import nats
import pytest

from custos.core.runner_fact import RunnerFactOutbox

_IMAGE = os.environ.get("CUSTOS_NATS_TEST_IMAGE", "nats:2.10-alpine")
_ENABLED = os.environ.get("CUSTOS_RUN_REAL_RUNNER_FACT_PUBLICATION") == "1"
_TENANT_ID = "acme"
_RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
_DEPLOYMENT_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
_STREAM = "CRUCIBLE_RUNNER_FACT_V1"
_STREAM_SUBJECTS = "crucible.runner.fact.v1.*.*.*"


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


async def _exercise_publication(nats_url: str, database: Path) -> None:
    subject = f"crucible.runner.fact.v1.{_TENANT_ID}.{_RUNNER_ID}.sandbox"
    admin = await nats.connect(servers=[nats_url], name="runner-fact-acceptance-admin")
    jetstream = admin.jetstream()
    await jetstream.add_stream(name=_STREAM, subjects=[_STREAM_SUBJECTS])

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("runner_fact_publication_process.py")),
        "--nats-url",
        nats_url,
        "--database",
        str(database),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except TimeoutError as error:
        process.terminate()
        stdout, stderr = await process.communicate()
        raise AssertionError(
            "RunnerFact publication process did not exit after PubAck: "
            f"{stderr.decode() or stdout.decode()}"
        ) from error
    assert process.returncode == 0, stderr.decode()
    publication = json.loads(stdout)
    assert publication["delivered"] == 1
    assert publication["pending_after"] == 0
    assert publication["subject"] == subject
    assert publication["publication_receipt_payload_sha256"] == publication["payload_sha256"]
    assert publication["broker_stream"] == _STREAM
    assert publication["broker_sequence"] == 1
    assert publication["broker_domain"] is None
    assert publication["puback_duplicate"] is False
    try:
        subscription = await jetstream.pull_subscribe(
            subject,
            durable="runner-fact-local-acceptance",
            stream=_STREAM,
        )
        messages = await subscription.fetch(1, timeout=2)
        assert len(messages) == 1
        message = messages[0]
        document = json.loads(message.data)
        assert document["batch_id"] == publication["batch_id"]
        assert document["tenant_id"] == _TENANT_ID
        assert document["runner_id"] == str(_RUNNER_ID)
        assert document["deployment_instance_id"] == str(_DEPLOYMENT_INSTANCE_ID)
        assert document["generation"] == 7
        assert hashlib.sha256(message.data).hexdigest() == publication["payload_sha256"]
        assert message.headers is not None
        assert message.headers["Nats-Msg-Id"] == publication["batch_id"]
        await message.ack()

        stream = await jetstream.stream_info(_STREAM)
        assert stream.state.messages == 1
        assert await RunnerFactOutbox(database).pending() == []
    finally:
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

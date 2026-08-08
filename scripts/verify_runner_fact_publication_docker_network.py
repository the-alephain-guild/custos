#!/usr/bin/env python3
"""Verify RunnerFact publication entirely inside an isolated Docker network."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNNER_IMAGE = os.environ.get("CUSTOS_TEST_IMAGE", "custos-runner:test")
DEFAULT_NATS_IMAGE = os.environ.get("CUSTOS_NATS_TEST_IMAGE", "nats:2.10-alpine")
STREAM = "CRUCIBLE_RUNNER_FACT_V1"
STREAM_SUBJECTS = "crucible.runner.fact.v1.*.*.*"
TENANT_ID = "acme"
RUNNER_ID = "10000000-0000-4000-8000-000000000001"
DEPLOYMENT_INSTANCE_ID = "20000000-0000-4000-8000-000000000002"


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def require_image(image: str) -> None:
    inspected = run("docker", "image", "inspect", image, check=False)
    if inspected.returncode != 0:
        raise RuntimeError(f"required immutable image is unavailable: {image}")


def wait_for_nats(container: str) -> None:
    for _ in range(100):
        logs = run("docker", "logs", container, check=False)
        if "Server is ready" in f"{logs.stdout}\n{logs.stderr}":
            return
        time.sleep(0.1)
    logs = run("docker", "logs", container, check=False)
    raise RuntimeError(f"NATS did not become ready:\n{logs.stdout}\n{logs.stderr}")


async def verify_inside_network(nats_url: str, database: Path) -> dict[str, Any]:
    import nats

    from custos.core.runner_fact import RunnerFactOutbox

    subject = f"crucible.runner.fact.v1.{TENANT_ID}.{RUNNER_ID}.sandbox"
    admin = await nats.connect(servers=[nats_url], name="runner-fact-network-acceptance")
    jetstream = admin.jetstream()
    try:
        await jetstream.add_stream(name=STREAM, subjects=[STREAM_SUBJECTS])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "tests/integration/runner_fact_publication_process.py"),
            "--nats-url",
            nats_url,
            "--database",
            str(database),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode != 0:
            raise RuntimeError(
                "RunnerFact publication process failed: " + (stderr.decode() or stdout.decode())
            )
        output_lines = [line for line in stdout.decode().splitlines() if line.strip()]
        if not output_lines:
            raise RuntimeError("RunnerFact publication process produced no receipt")
        publication = json.loads(output_lines[-1])
        expected = {
            "delivered": 1,
            "pending_after": 0,
            "subject": subject,
            "broker_stream": STREAM,
            "broker_sequence": 1,
            "broker_domain": None,
            "puback_duplicate": False,
        }
        for field, value in expected.items():
            if publication.get(field) != value:
                raise RuntimeError(
                    f"RunnerFact publication receipt {field} differs: "
                    f"{publication.get(field)!r} != {value!r}"
                )
        if publication.get("publication_receipt_payload_sha256") != publication.get(
            "payload_sha256"
        ):
            raise RuntimeError("durable PubAck receipt payload digest differs")

        subscription = await jetstream.pull_subscribe(
            subject,
            durable="runner-fact-network-acceptance",
            stream=STREAM,
        )
        messages = await subscription.fetch(1, timeout=3)
        if len(messages) != 1:
            raise RuntimeError("exactly one RunnerFact batch was expected")
        message = messages[0]
        document = json.loads(message.data)
        if (
            document.get("batch_id") != publication.get("batch_id")
            or document.get("tenant_id") != TENANT_ID
            or document.get("runner_id") != RUNNER_ID
            or document.get("deployment_instance_id") != DEPLOYMENT_INSTANCE_ID
            or document.get("generation") != 7
        ):
            raise RuntimeError("published RunnerFact identity or generation differs")
        if message.headers is None or message.headers.get("Nats-Msg-Id") != publication.get(
            "batch_id"
        ):
            raise RuntimeError("published RunnerFact message id differs")
        await message.ack()
        if await RunnerFactOutbox(database).pending() != []:
            raise RuntimeError("RunnerFact outbox retained a batch after durable PubAck")
        stream = await jetstream.stream_info(STREAM)
        if stream.state.messages != 1:
            raise RuntimeError("JetStream did not retain exactly one RunnerFact batch")
        return {
            "status": "LOCAL_DOCKER_NETWORK_RUNNER_FACT_PUBLICATION_VERIFIED",
            "batch_id": publication["batch_id"],
            "payload_sha256": publication["payload_sha256"],
            "broker_stream": publication["broker_stream"],
            "broker_sequence": publication["broker_sequence"],
            "deployment_instance_id": DEPLOYMENT_INSTANCE_ID,
            "generation": 7,
            "host_tcp_exposed": False,
            "production_ready": False,
        }
    finally:
        await admin.close()


def run_outer(runner_image: str, nats_image: str) -> int:
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required by the Docker-network acceptance")
    require_image(runner_image)
    require_image(nats_image)
    suffix = uuid4().hex
    network = f"custos-runner-fact-{suffix}"
    nats_container = f"custos-runner-fact-nats-{suffix}"
    run("docker", "network", "create", network)
    try:
        started = run(
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            nats_container,
            "--network",
            network,
            "--network-alias",
            "nats",
            nats_image,
            "-js",
        )
        if not started.stdout.strip():
            raise RuntimeError("NATS container did not return a container id")
        try:
            wait_for_nats(nats_container)
            client = run(
                "docker",
                "run",
                "--rm",
                "--network",
                f"container:{nats_container}",
                "--workdir",
                "/workspace",
                "--entrypoint",
                "python",
                "--env",
                "PYTHONPATH=/workspace/src",
                "--volume",
                f"{ROOT}:/workspace:ro",
                runner_image,
                "/workspace/scripts/verify_runner_fact_publication_docker_network.py",
                "--inside",
                "--nats-url",
                "nats://127.0.0.1:4222",
                "--database",
                "/tmp/runner-fact-outbox.sqlite3",
                check=False,
            )
            if client.returncode != 0:
                raise RuntimeError(
                    "Docker-network acceptance client failed: " + (client.stderr or client.stdout)
                )
            output_lines = [line for line in client.stdout.splitlines() if line.strip()]
            if not output_lines:
                raise RuntimeError("Docker-network acceptance produced no receipt")
            receipt = json.loads(output_lines[-1])
            if receipt.get("status") != ("LOCAL_DOCKER_NETWORK_RUNNER_FACT_PUBLICATION_VERIFIED"):
                raise RuntimeError("Docker-network acceptance receipt status differs")
            print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        finally:
            run("docker", "stop", "-t", "1", nats_container, check=False)
    finally:
        run("docker", "network", "rm", network, check=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--nats-url")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--runner-image", default=DEFAULT_RUNNER_IMAGE)
    parser.add_argument("--nats-image", default=DEFAULT_NATS_IMAGE)
    args = parser.parse_args()
    if args.inside:
        if args.nats_url is None or args.database is None:
            parser.error("--inside requires --nats-url and --database")
        print(
            json.dumps(
                asyncio.run(verify_inside_network(args.nats_url, args.database)),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    return run_outer(args.runner_image, args.nats_image)


if __name__ == "__main__":
    raise SystemExit(main())

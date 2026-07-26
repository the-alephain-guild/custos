"""Consume one Crucible-issued runner transport authority in a real process."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from custos.core.machine_credential_vault import MachineCredential
from custos.core.nats_client import CrucibleNatsClient
from custos.core.nats_transport import RunnerNatsTransportConnectionProfile
from custos.core.runner_nats_authority import RunnerNatsTransportAuthorityClient


def _load_machine_credential(path: Path) -> MachineCredential:
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise PermissionError("machine credential fixture must have mode 0600")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("machine credential fixture must be an object")
    return MachineCredential.from_document(document)


def _write_ready(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    machine_credential = _load_machine_credential(args.machine_credential)
    authority = RunnerNatsTransportAuthorityClient(
        args.crucible_url,
        machine_credential,
    )
    operation = authority.prepare_initial(
        authorization_intent_id=args.authorization_intent_id,
        trading_mode="sandbox",
        expected_issuer_public_key=args.issuer_public_key,
    )
    completion = await asyncio.to_thread(
        authority.execute,
        operation,
        timeout_seconds=args.operation_timeout_secs,
    )
    credential = completion.credential
    if credential is None:
        raise RuntimeError("initial transport operation returned no credential")
    profile = RunnerNatsTransportConnectionProfile(
        credential=credential,
        nats_url=args.nats_url,
        ca_path=args.ca_path,
        server_name=args.server_name,
        pinned_issuer_public_key=args.issuer_public_key,
    )
    client = CrucibleNatsClient(
        connection_profile=profile,
        tenant_id=machine_credential.tenant_id,
        runner_id=str(machine_credential.runner_id),
        machine_credential=machine_credential,
    )
    await client.connect()
    try:
        subscription = await client.subscribe_control()
        _write_ready(
            args.ready_file,
            {
                "operation_id": str(operation.operation_id),
                "credential_generation": credential.credential_generation,
                "user_public_key": credential.user_public_key,
                "durable_name": credential.durable_config["durable_name"],
                "user_seed_egress": False,
            },
        )
        message = await subscription.next_msg(timeout=args.command_timeout_secs)
        if message.subject != credential.durable_config["filter_subjects"][0]:
            raise RuntimeError("delivered command subject differs from exact authority")
        if bytes(message.data) != args.command_payload.encode("utf-8"):
            raise RuntimeError("delivered command payload differs")
        await message.ack_sync(timeout=args.command_timeout_secs)
    finally:
        await client.close()

    receipt = {
        "operation_id": str(operation.operation_id),
        "request_fingerprint": completion.request_fingerprint,
        "tenant_id": credential.tenant_id,
        "runner_id": str(credential.runner_id),
        "trading_mode": credential.trading_mode,
        "transport_domain": credential.transport_domain,
        "credential_generation": credential.credential_generation,
        "authority_digest": credential.authority_digest,
        "user_public_key": credential.user_public_key,
        "durable_name": credential.durable_config["durable_name"],
        "command_subject": credential.durable_config["filter_subjects"][0],
        "command_payload_sha256": hashlib.sha256(
            args.command_payload.encode("utf-8")
        ).hexdigest(),
        "command_acked": True,
        "user_seed_egress": False,
        "user_jwt_egress": False,
        "machine_private_key_egress": False,
    }
    forbidden = {"user_seed", "user_jwt", "private_key", "machine_credential"}
    if forbidden.intersection(receipt):
        raise RuntimeError("consumer receipt contains secret material")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crucible-url", required=True)
    parser.add_argument("--machine-credential", type=Path, required=True)
    parser.add_argument("--authorization-intent-id", type=UUID, required=True)
    parser.add_argument("--issuer-public-key", required=True)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--ca-path", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--command-payload", required=True)
    parser.add_argument("--operation-timeout-secs", type=float, default=30.0)
    parser.add_argument("--command-timeout-secs", type=float, default=15.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

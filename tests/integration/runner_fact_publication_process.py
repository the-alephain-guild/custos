"""Launch one RunnerFact publication through production modules."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

from custos.core.nats_transport import (
    DevelopmentLocalNatsConnectionProfile,
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportCredential,
)
from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
)

TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
DEPLOYMENT_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
DEPLOYMENT_SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
SPEC_DIGEST = "a" * 64
CAPABILITY_DIGEST = "a1feebeb052f478831f49562cf6343a47c15a5f6d0c1b3028bfe8e56f1821a68"
KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id=TENANT_ID,
        trading_mode="sandbox",
        runner_id=RUNNER_ID,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        deployment_spec_id=DEPLOYMENT_SPEC_ID,
        deployment_spec_digest=SPEC_DIGEST,
        generation=7,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_VERSION_ID,
        capability_version=1,
        capability_manifest_digest=CAPABILITY_DIGEST,
    )


async def _publish(connection_profile: object, database: Path) -> dict[str, object]:
    authority = _authority()
    outbox = RunnerFactOutbox(database)
    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), KEY_ID)
    fact = RunnerDeploymentLifecycleFact.observed(
        authority,
        generation=authority.generation,
        lifecycle_state="running",
        command_fingerprint=SPEC_DIGEST,
        outcome="applied",
    ).to_wire()
    batch_id = await outbox.enqueue(authority, identity, [fact])
    if batch_id is None:
        raise RuntimeError("acceptance RunnerFact was unexpectedly deduplicated")
    pending = await outbox.pending()
    if len(pending) != 1:
        raise RuntimeError("acceptance outbox must contain exactly one batch")
    payload_sha256 = hashlib.sha256(pending[0].payload).hexdigest()

    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": connection_profile},
        outbox=outbox,
        runner_id=RUNNER_ID,
        authority_guard=lambda: None,
    )
    try:
        delivered = await publisher.drain_once()
    finally:
        await publisher.close()
    publication_receipt = await outbox.publication_receipt(batch_id)
    if publication_receipt is None:
        raise RuntimeError("JetStream PubAck did not create a durable publication receipt")
    return {
        "batch_id": str(batch_id),
        "subject": authority.subject,
        "payload_sha256": payload_sha256,
        "delivered": delivered,
        "pending_after": len(await RunnerFactOutbox(database).pending()),
        "publication_receipt_payload_sha256": (
            publication_receipt.batch_payload_sha256
        ),
        "broker_stream": publication_receipt.broker_stream,
        "broker_sequence": publication_receipt.broker_sequence,
        "broker_domain": publication_receipt.broker_domain,
        "puback_duplicate": publication_receipt.duplicate,
    }


def _connection_profile(args: argparse.Namespace) -> object:
    authentication_values = (
        args.transport_credential,
        args.ca_path,
        args.server_name,
        args.pinned_issuer_public_key,
    )
    if not any(authentication_values):
        return DevelopmentLocalNatsConnectionProfile(
            tenant_id=TENANT_ID,
            runner_id=RUNNER_ID,
            nats_url=args.nats_url,
        )
    if not all(authentication_values):
        raise ValueError(
            "authenticated publication requires transport credential, CA path, "
            "server name, and pinned issuer public key"
        )
    document = json.loads(args.transport_credential.read_text())
    credential = RunnerNatsTransportCredential.from_document(document)
    if (
        credential.tenant_id != TENANT_ID
        or credential.runner_id != RUNNER_ID
        or credential.trading_mode != "sandbox"
    ):
        raise ValueError("transport credential does not match the publication identity")
    return RunnerNatsTransportConnectionProfile(
        credential=credential,
        nats_url=args.nats_url,
        ca_path=args.ca_path,
        server_name=args.server_name,
        pinned_issuer_public_key=args.pinned_issuer_public_key,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--transport-credential", type=Path)
    parser.add_argument("--ca-path", type=Path)
    parser.add_argument("--server-name")
    parser.add_argument("--pinned-issuer-public-key")
    args = parser.parse_args()
    try:
        connection_profile = _connection_profile(args)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            asyncio.run(_publish(connection_profile, args.database)),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

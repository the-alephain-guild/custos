"""Launch one signed command through engine readiness and authenticated RunnerFact publication."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.cli._daemon import _runner_fact_authority
from custos.core.engine_lifecycle import EngineLifecycleConfig, EngineLifecycleSupervisor
from custos.core.nats_transport import (
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportCredential,
)
from custos.core.runner_command_intake import (
    CommandDeliveryPolicy,
    CommandIntakeCoordinator,
    CrucibleRunnerCommandAuthenticator,
    VerifiedRunnerCommand,
)
from custos.core.runner_command_runtime import RunnerCommandRuntimeCoordinator
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerCapabilityScopeBinding,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
    RunnerStateStore,
    capability_binding_evidence_digest,
)
from custos.engines.nautilus.host import SandboxSimulationHost

ROOT = Path(__file__).resolve().parents[2]
COMMAND_FIXTURE = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"
TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
RUNNER_FACT_KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"
COMMAND_KEY_ID = "fixture-domain-key-v1"
PRIVATE_KEY_BYTES = bytes(range(1, 33))


class _AcceptanceDelivery:
    delivered_count = 1
    delivery_id = "authenticated-command-lifecycle-acceptance"

    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data
        self.events: list[str] = []

    async def ack(self) -> None:
        self.events.append("ack")

    async def nak(self, delay: float | None = None) -> None:
        self.events.append(f"nak:{delay}")

    async def term(self) -> None:
        self.events.append("term")

    async def in_progress(self) -> None:
        self.events.append("in_progress")


class _AcceptanceReleaseResolver:
    async def resolve(self, verified: object) -> object:
        del verified
        return SimpleNamespace(
            release_authority=object(),
            release_statement_bytes=b"acceptance-release-statement",
            detached_bundle_path=ROOT / "pyproject.toml",
            member_paths={"wheel": ROOT / "pyproject.toml"},
            verified_at=object(),
        )


class _AcceptanceArtifactRuntime:
    def __init__(self, state: RunnerStateStore) -> None:
        self._state = state

    async def prepare(self, **kwargs: object) -> object:
        durable = await self._state.load_durable_desired_command(
            UUID(str(kwargs["deployment_instance_id"]))
        )
        return SimpleNamespace(
            verified=VerifiedRunnerCommand(
                command=durable.command,
                command_fingerprint=durable.command_fingerprint,
                verification_receipt=durable.verification_receipt,
            ),
            receipt=SimpleNamespace(
                runner_local_policy_decision=SimpleNamespace(
                    policy_id="authenticated-command-lifecycle-acceptance"
                )
            )
        )

    async def activate(self, prepared: object, *, loader: object) -> object:
        del loader
        verified = prepared.verified
        activation_id = "authenticated-command-lifecycle-activation"
        await self._state.record_artifact_activation(
            verified=verified,
            activation_id=activation_id,
            artifact_identity_digest=verified.command.artifact_identity_digest,
            artifact_authority_digest=verified.command.artifact_source.snapshot.snapshot_digest,
        )
        return SimpleNamespace(
            activation_id=activation_id,
            strategy=object(),
        )


class _AcceptanceCredentialResolver:
    async def resolve(self, verified: object, credential_scope: object) -> dict[str, object]:
        del verified, credential_scope
        return {}


def _recursively_sorted(value: object) -> object:
    if isinstance(value, dict):
        return {key: _recursively_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_recursively_sorted(item) for item in value]
    return value


def _signed_command() -> tuple[_AcceptanceDelivery, Ed25519PrivateKey]:
    fixture = json.loads(COMMAND_FIXTURE.read_text(encoding="utf-8"))
    case = next(
        value for value in fixture["cases"] if value["name"] == "deployment_spec_ready_for_runner"
    )
    event = dict(case["event_document"])
    event["payload"] = _recursively_sorted(event["payload"])
    event_bytes = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    subject = str(case["subject"])
    subject_bytes = subject.encode("utf-8")
    framed = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
    private_key = Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY_BYTES)

    def base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    envelope = {
        "schema_version": 1,
        "signature_profile": "crucible-domain-event-v1-exact-bytes",
        "event_encoding": "application/json;base64url",
        "event_bytes": base64url(event_bytes),
        "signature_key_id": COMMAND_KEY_ID,
        "signature": base64url(private_key.sign(framed)),
    }
    return (
        _AcceptanceDelivery(
            subject,
            json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        ),
        private_key,
    )


def _capability(command: Any, identity: RunnerFactIdentity) -> RunnerCapabilityReceipt:
    binding = RunnerCapabilityScopeBinding(
        projector="deployment_lifecycle",
        trading_mode=command.trading_mode,
        deployment_instance_id=str(command.deployment_instance_id),
        deployment_spec_id=str(command.deployment_spec_id),
        deployment_spec_digest=command.deployment_spec_digest,
        strategy_id=str(command.strategy_id),
        source_policy_digest=None,
        required_venues=(),
    )
    bindings = (binding,)
    return RunnerCapabilityReceipt(
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        capability_version_id=CAPABILITY_VERSION_ID,
        capability_version=1,
        manifest_digest=hashlib.sha256(b"authenticated-command-lifecycle-capability").hexdigest(),
        key_id=identity.key_id,
        key_version=1,
        algorithm="ed25519",
        public_key_digest=hashlib.sha256(identity.public_key_bytes).hexdigest(),
        binding_status="validated",
        binding_evidence_digest=capability_binding_evidence_digest(
            TENANT_ID,
            RUNNER_ID,
            bindings,
        ),
        capability_manifest={},
        scope_bindings=bindings,
    )


def _transport_profile(args: argparse.Namespace) -> RunnerNatsTransportConnectionProfile:
    credential = RunnerNatsTransportCredential.from_document(
        json.loads(args.transport_credential.read_text())
    )
    if (
        credential.tenant_id != TENANT_ID
        or credential.runner_id != RUNNER_ID
        or credential.trading_mode != "sandbox"
    ):
        raise ValueError("transport credential does not match the command runtime identity")
    return RunnerNatsTransportConnectionProfile(
        credential=credential,
        nats_url=args.nats_url,
        ca_path=args.ca_path,
        server_name=args.server_name,
        pinned_issuer_public_key=args.pinned_issuer_public_key,
    )


async def _run(args: argparse.Namespace) -> dict[str, object]:
    delivery, command_private_key = _signed_command()
    identity = RunnerFactIdentity.from_private_bytes(PRIVATE_KEY_BYTES, RUNNER_FACT_KEY_ID)
    outbox = RunnerFactOutbox(args.database)
    authenticator = CrucibleRunnerCommandAuthenticator(
        expected_tenant_id=TENANT_ID,
        expected_runner_id=RUNNER_ID,
        allowed_trading_modes=frozenset({"sandbox"}),
        signature_keys={COMMAND_KEY_ID: command_private_key.public_key()},
    )
    verified = authenticator.verify(
        subject=delivery.subject,
        signed_envelope_bytes=delivery.data,
    )
    capability = _capability(verified.command, identity)
    state = RunnerStateStore(
        outbox=outbox,
        identity=identity,
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        authority_resolver=lambda accepted: _runner_fact_authority(capability, accepted),
    )
    delivery_policy = CommandDeliveryPolicy(in_progress_interval_seconds=0.05)
    runtime = RunnerCommandRuntimeCoordinator(
        intake=CommandIntakeCoordinator(
            authenticator=authenticator,
            durability=state,
            policy=delivery_policy,
        ),
        durability=state,
        release_resolver=_AcceptanceReleaseResolver(),
        artifact_runtime=_AcceptanceArtifactRuntime(state),
        entry_point_loader=object(),
        credential_resolver=_AcceptanceCredentialResolver(),
        engine_lifecycle=EngineLifecycleSupervisor(
            engine=SandboxSimulationHost(),
            state_store=state,
            artifact_capability=ArtifactRuntimeCapabilityV1.production_ready(),
            config=EngineLifecycleConfig(live_execution_enabled=False),
        ),
        delivery_policy=delivery_policy,
    )
    result = await runtime.process(delivery)
    if delivery.events != ["ack"] or result.ready_receipt is None:
        raise RuntimeError(
            "signed command did not reach durable engine readiness before ACK: "
            f"status={result.status.value}, reason={result.reason_code}, "
            f"delivery_events={delivery.events}"
        )
    pending = await outbox.pending()
    if len(pending) != 1:
        raise RuntimeError("command lifecycle must create exactly one durable RunnerFact batch")
    batch = pending[0]
    document = json.loads(batch.payload)
    fact = document["facts"][0]
    if (
        fact["kind"] != "RunnerDeploymentLifecycleFact.v1"
        or fact["lifecycle_state"] != "running"
        or fact["outcome"] != "applied"
    ):
        raise RuntimeError("command lifecycle emitted an unexpected RunnerFact")
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": _transport_profile(args)},
        outbox=outbox,
        runner_id=RUNNER_ID,
        authority_guard=lambda: None,
    )
    try:
        delivered = await publisher.drain_once()
    finally:
        await publisher.close()
    return {
        "batch_id": str(batch.batch_id),
        "subject": batch.subject,
        "payload_sha256": hashlib.sha256(batch.payload).hexdigest(),
        "command_acked": delivery.events == ["ack"],
        "runtime_status": result.status.value,
        "engine_ready": result.ready_receipt is not None,
        "deployment_instance_id": str(verified.command.deployment_instance_id),
        "lifecycle_fact_kind": fact["kind"],
        "lifecycle_state": fact["lifecycle_state"],
        "lifecycle_outcome": fact["outcome"],
        "delivered": delivered,
        "pending_after": len(await RunnerFactOutbox(args.database).pending()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--transport-credential", type=Path, required=True)
    parser.add_argument("--ca-path", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--pinned-issuer-public-key", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

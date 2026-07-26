"""Launch the production daemon around one authenticated command lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import signal
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.cli import _daemon as daemon_module
from custos.core.machine_credential_vault import MachineCredential
from custos.core.nats_transport import RunnerNatsTransportCredential
from custos.core.runner_command_intake import (
    CrucibleRunnerCommandAuthenticator,
    VerifiedRunnerCommand,
)
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactIdentity,
    RunnerFactOutbox,
    RunnerStateStore,
    capability_binding_evidence_digest,
    normalize_capability_scope_bindings,
)
from custos.core.runner_toml import RunnerToml

ROOT = Path(__file__).resolve().parents[2]
COMMAND_FIXTURE = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"
CAPABILITY_RECEIPT = ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
MACHINE_CREDENTIAL_ID = UUID("60000000-0000-4000-8000-000000000006")
RUNNER_FACT_KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"
COMMAND_KEY_ID = "fixture-domain-key-v1"
PRIVATE_KEY_BYTES = bytes(range(1, 33))


def _recursively_sorted(value: object) -> object:
    if isinstance(value, dict):
        return {key: _recursively_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_recursively_sorted(item) for item in value]
    return value


def _fixture_signed_command() -> tuple[str, bytes]:
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
    return subject, json.dumps(envelope, separators=(",", ":")).encode("utf-8")


class _AcceptanceReleaseResolver:
    async def resolve(self, verified: object) -> object:
        del verified
        return SimpleNamespace(
            release_authority=object(),
            release_statement_bytes=b"daemon-acceptance-release-statement",
            detached_bundle_path=ROOT / "pyproject.toml",
            member_paths={"wheel": ROOT / "pyproject.toml"},
            verified_at=datetime.now(UTC),
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
                    policy_id="daemon-lifecycle-acceptance"
                )
            ),
        )

    async def activate(self, prepared: object, *, loader: object) -> object:
        del loader
        verified = prepared.verified
        activation_id = "daemon-lifecycle-activation"
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


def _capability(command: Any, identity: RunnerFactIdentity) -> RunnerCapabilityReceipt:
    canonical = RunnerCapabilityReceipt.load(CAPABILITY_RECEIPT)
    manifest = json.loads(json.dumps(canonical.capability_manifest))
    for key in (
        "settlement_scope_bindings",
        "risk_scope_bindings",
        "reconciliation_scope_bindings",
        "health_scope_bindings",
        "deployment_lifecycle_scope_bindings",
    ):
        for binding in manifest[key]:
            binding["deployment_spec_digest"] = command.deployment_spec_digest
            if key == "reconciliation_scope_bindings":
                binding["source_policy_digest"] = command.deployment_spec["source_policy_digest"]
    bindings = normalize_capability_scope_bindings(manifest)
    receipt = RunnerCapabilityReceipt(
        tenant_id=canonical.tenant_id,
        runner_id=canonical.runner_id,
        capability_version_id=canonical.capability_version_id,
        capability_version=canonical.capability_version,
        manifest_digest=hashlib.sha256(
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest(),
        key_id=canonical.key_id,
        key_version=canonical.key_version,
        algorithm=canonical.algorithm,
        public_key_digest=canonical.public_key_digest,
        binding_status=canonical.binding_status,
        binding_evidence_digest=capability_binding_evidence_digest(
            canonical.tenant_id,
            canonical.runner_id,
            bindings,
        ),
        capability_manifest=manifest,
        scope_bindings=bindings,
    )
    receipt.require_scope_bindings(
        projectors=("deployment_lifecycle",),
        trading_mode=command.trading_mode,
        deployment_instance_id=command.deployment_instance_id,
        deployment_spec_id=command.deployment_spec_id,
        deployment_spec_digest=command.deployment_spec_digest,
        strategy_id=command.strategy_id,
    )
    if (
        receipt.tenant_id != TENANT_ID
        or receipt.runner_id != RUNNER_ID
        or receipt.capability_version_id != CAPABILITY_VERSION_ID
        or receipt.key_id != identity.key_id
        or receipt.public_key_digest != hashlib.sha256(identity.public_key_bytes).hexdigest()
    ):
        raise RuntimeError("canonical capability receipt differs from runner identity")
    return receipt


def _transport_credential(path: Path) -> RunnerNatsTransportCredential:
    credential = RunnerNatsTransportCredential.from_document(
        json.loads(path.read_text(encoding="utf-8"))
    )
    if (
        credential.tenant_id != TENANT_ID
        or credential.runner_id != RUNNER_ID
        or credential.trading_mode != "sandbox"
    ):
        raise ValueError("transport credential does not match the daemon runtime identity")
    return credential


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _install_acceptance_authorities(
    *,
    machine_credential: MachineCredential,
    metadata: RunnerToml,
    transport_credential: RunnerNatsTransportCredential,
    capability: RunnerCapabilityReceipt,
) -> None:
    class StaticRunnerToml:
        @staticmethod
        def read(path: Path) -> RunnerToml:
            del path
            return metadata

    class StaticMachineCredentialVault:
        def __init__(self, path: Path) -> None:
            del path

        def load(self) -> MachineCredential:
            return machine_credential

    class StaticMachineCredentialHttpClient:
        def __init__(self, backend_url: str, credential: MachineCredential) -> None:
            del backend_url
            self.credential = credential

        def verify_active(self) -> None:
            self.credential.assert_active()

    class StaticTransportAuthority:
        def active(self, trading_mode: str) -> RunnerNatsTransportCredential:
            if trading_mode != "sandbox":
                raise ValueError("acceptance transport exposes only sandbox")
            return transport_credential

    class StaticTransportSet:
        @staticmethod
        def load(path: Path, enabled_modes: object) -> StaticTransportAuthority:
            del path
            if tuple(enabled_modes) != ("sandbox",):
                raise ValueError("acceptance transport requires exactly sandbox")
            return StaticTransportAuthority()

    class StaticCapabilityReceipt:
        @staticmethod
        def load(path: Path) -> RunnerCapabilityReceipt:
            del path
            return capability

    def build_strategy_release_runtime(
        *,
        args: argparse.Namespace,
        state_store: RunnerStateStore,
        material_authority: object,
    ) -> tuple[object, object]:
        del args, material_authority
        return _AcceptanceReleaseResolver(), _AcceptanceArtifactRuntime(state_store)

    def build_credential_resolver(vault: object) -> _AcceptanceCredentialResolver:
        del vault
        return _AcceptanceCredentialResolver()

    daemon_module.RunnerToml = StaticRunnerToml  # type: ignore[misc]
    daemon_module.MachineCredentialVault = StaticMachineCredentialVault  # type: ignore[misc]
    daemon_module.MachineCredentialHttpClient = (  # type: ignore[misc]
        StaticMachineCredentialHttpClient
    )
    daemon_module.RunnerNatsTransportSet = StaticTransportSet  # type: ignore[misc]
    daemon_module.RunnerCapabilityReceipt = StaticCapabilityReceipt  # type: ignore[misc]
    daemon_module._build_strategy_release_runtime = (  # type: ignore[assignment]
        build_strategy_release_runtime
    )
    daemon_module.VaultRunnerCredentialResolverV1 = (  # type: ignore[misc]
        build_credential_resolver
    )


def _runtime_args(
    args: argparse.Namespace,
    *,
    command_public_key_path: Path,
) -> argparse.Namespace:
    root = args.database.parent
    return argparse.Namespace(
        ready_file=args.ready_file,
        runner_toml_path=root / "runner.toml",
        machine_vault=root / "runner-machine.enc",
        development_local_nats_url="",
        enabled_modes=("sandbox",),
        nats_transport_vault_dir=root / "nats-transport",
        nats_sim_url=args.nats_url,
        nats_sim_ca=args.ca_path,
        nats_sim_server_name=args.server_name,
        nats_sim_issuer_public_key=args.pinned_issuer_public_key,
        nats_live_url="",
        nats_live_ca=args.ca_path,
        nats_live_server_name="",
        nats_live_issuer_public_key="",
        tenant_id=TENANT_ID,
        runner_id=str(RUNNER_ID),
        runner_capability=root / "runner-capability.json",
        runner_fact_outbox=args.database,
        reconcile=True,
        crucible_domain_key_id=COMMAND_KEY_ID,
        crucible_domain_public_key=command_public_key_path,
        engine="sandbox-sim",
        development_artifact_root=root / "development-artifacts",
        artifact_release_policy_envelope=None,
        artifact_release_policy_key_id="",
        artifact_release_policy_public_key=None,
        artifact_sigstore_trusted_root=None,
        artifact_registry="ghcr.io",
        artifact_registry_username="",
        artifact_registry_token="",
        artifact_cache_dir=root / "artifact-cache",
        artifact_quarantine_dir=root / "artifact-quarantine",
        artifact_activation_dir=root / "artifact-activation",
        vault_dir=root / "venue-vault",
        runner_fact_snapshot_interval_secs=3600.0,
        runner_fact_period_secs=3600,
        runner_fact_period_retry_secs=3600.0,
    )


def _command_outcome(database: Path) -> dict[str, object] | None:
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        outcome = connection.execute(
            """
            SELECT
                deployment_instance_id, outcome, durable_disposition,
                lifecycle_batch_id
            FROM command_outcomes
            WHERE lifecycle_batch_id IS NOT NULL
            ORDER BY recorded_at_ns DESC
            LIMIT 1
            """
        ).fetchone()
        if outcome is None:
            return None
        applied = connection.execute(
            """
            SELECT observed_status
            FROM applied_deployments
            WHERE deployment_instance_id = ?
            """,
            (str(outcome["deployment_instance_id"]),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        if "connection" in locals():
            connection.close()
    return {
        "deployment_instance_id": str(outcome["deployment_instance_id"]),
        "outcome": str(outcome["outcome"]),
        "durable_disposition": str(outcome["durable_disposition"]),
        "lifecycle_batch_id": str(outcome["lifecycle_batch_id"]),
        "observed_status": str(applied["observed_status"]) if applied is not None else None,
    }


async def _wait_for_daemon_ready(
    task: asyncio.Task[int],
    ready_file: Path,
) -> None:
    for _ in range(200):
        if task.done():
            await task
            raise RuntimeError("daemon exited before readiness")
        if ready_file.is_file():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not publish readiness")


async def _wait_for_publication(
    task: asyncio.Task[int],
    database: Path,
) -> tuple[dict[str, object], object]:
    outbox = RunnerFactOutbox(database)
    for _ in range(400):
        if task.done():
            await task
            raise RuntimeError("daemon exited before command publication")
        outcome = _command_outcome(database)
        if outcome is not None:
            batch_id = UUID(str(outcome["lifecycle_batch_id"]))
            receipt = await outbox.publication_receipt(batch_id)
            if receipt is not None and not await outbox.pending():
                return outcome, receipt
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not publish the lifecycle batch")


async def _run(args: argparse.Namespace) -> dict[str, object]:
    command_private_key = Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY_BYTES)
    expected_subject, expected_envelope = _fixture_signed_command()
    verified = CrucibleRunnerCommandAuthenticator(
        expected_tenant_id=TENANT_ID,
        expected_runner_id=RUNNER_ID,
        allowed_trading_modes=frozenset({"sandbox"}),
        signature_keys={COMMAND_KEY_ID: command_private_key.public_key()},
    ).verify(
        subject=expected_subject,
        signed_envelope_bytes=expected_envelope,
    )
    identity = RunnerFactIdentity.from_private_bytes(PRIVATE_KEY_BYTES, RUNNER_FACT_KEY_ID)
    capability = _capability(verified.command, identity)
    transport_credential = _transport_credential(args.transport_credential)
    valid_until = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    machine_credential = MachineCredential(
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        credential_id=MACHINE_CREDENTIAL_ID,
        credential_version=1,
        credential_valid_until=valid_until,
        machine_key_id=RUNNER_FACT_KEY_ID,
        machine_credential="rkc1.daemon-local-acceptance",
        private_key_bytes=PRIVATE_KEY_BYTES,
    )
    metadata = RunnerToml(
        tenant_id=TENANT_ID,
        runner_id=str(RUNNER_ID),
        backend_url="http://127.0.0.1:9",
        credential_id=str(MACHINE_CREDENTIAL_ID),
        credential_version=1,
        credential_valid_until=_timestamp_text(valid_until),
        machine_key_id=RUNNER_FACT_KEY_ID,
        machine_vault_path=str(args.database.with_suffix(".machine.enc").resolve()),
        enrolled_at=_timestamp_text(datetime.now(UTC).replace(microsecond=0)),
    )
    _install_acceptance_authorities(
        machine_credential=machine_credential,
        metadata=metadata,
        transport_credential=transport_credential,
        capability=capability,
    )
    command_public_key_path = args.database.with_suffix(".command.pub")
    command_public_key_path.write_text(
        base64.b64encode(
            command_private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii"),
        encoding="ascii",
    )
    daemon_task = asyncio.create_task(
        daemon_module.run_daemon(
            _runtime_args(args, command_public_key_path=command_public_key_path)
        ),
        name="custos-daemon-acceptance",
    )
    try:
        await _wait_for_daemon_ready(daemon_task, args.ready_file)
        outcome, publication = await _wait_for_publication(daemon_task, args.database)
    finally:
        if not daemon_task.done():
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(daemon_task, timeout=10)

    command_acked = outcome["durable_disposition"] == "ack"
    engine_ready = outcome["observed_status"] == "ready"
    reopened_state = RunnerStateStore(
        outbox=RunnerFactOutbox(args.database),
        identity=identity,
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        authority_resolver=lambda _verified: capability,
    )
    effective_policy = await reopened_state.load_effective_runner_safety_policy(
        "sandbox",
        now=datetime.now(UTC),
    )
    policy_document = effective_policy.policy.model_dump(mode="json")
    return {
        "batch_id": str(publication.batch_id),
        "subject": publication.subject,
        "payload_sha256": publication.batch_payload_sha256,
        "command_acked": command_acked,
        "runtime_status": "applied_acked" if command_acked else "unexpected",
        "engine_ready": engine_ready,
        "deployment_instance_id": str(verified.command.deployment_instance_id),
        "lifecycle_fact_kind": "RunnerDeploymentLifecycleFact.v1",
        "lifecycle_state": verified.command.lifecycle_state,
        "lifecycle_outcome": str(outcome["outcome"]),
        "delivered": 1,
        "pending_after": len(await RunnerFactOutbox(args.database).pending()),
        "daemon_launched": True,
        "authenticated_policy_consumed": True,
        "policy_id": policy_document["policy_id"],
        "policy_revision": policy_document["revision"],
        "policy_digest": policy_document["policy_digest"],
        "policy_status": policy_document["status"],
        "durable_puback_receipt": True,
        "broker_stream": publication.broker_stream,
        "broker_sequence": publication.broker_sequence,
        "puback_duplicate": publication.duplicate,
        "production_authority_issued": False,
        "production_policy_issued": False,
        "immutable_artifact_materialized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--transport-credential", type=Path, required=True)
    parser.add_argument("--ca-path", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--pinned-issuer-public-key", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Launch the production daemon around one authenticated command lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import signal
import sqlite3
import stat
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.artifacts.immutable_material import RegistryStrategyReleaseMaterializerV1
from custos.artifacts.policy import (
    ArchiveLimitsV1,
    ReleaseTrustPolicyV1,
    SignedReleaseTrustPolicyEnvelopeV1,
    SigstoreIdentityV1,
    canonical_policy_bytes,
    release_policy_signature_message,
)
from custos.artifacts.release_resolver import (
    CrucibleStrategyReleaseArtifactResolverV1,
    StrategyReleaseResolutionRejected,
)
from custos.artifacts.runtime import (
    ArtifactRuntimeCapabilityV1,
    ArtifactRuntimeConfigV1,
    StrategyArtifactRuntimeV1,
)
from custos.artifacts.verification_types import (
    RunnerLocalArtifactVerificationConfig,
    SigstoreVerificationEvidence,
    SigstoreVerificationRequest,
)
from custos.cli import _daemon as daemon_module
from custos.cli.subcommands import nats_transport as nats_transport_cli
from custos.core.machine_credential_vault import MachineCredential, MachineCredentialVault
from custos.core.nats_transport import (
    RunnerNatsTransportBundle,
    RunnerNatsTransportCredential,
    RunnerNatsTransportVault,
)
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactIdentity,
    RunnerFactOutbox,
    RunnerStateStore,
    capability_binding_evidence_digest,
    normalize_capability_scope_bindings,
)
from custos.core.runner_nats_authority import RunnerNatsTransportAuthorityClient
from custos.core.runner_toml import RunnerToml

ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_RECEIPT = ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
MACHINE_CREDENTIAL_ID = UUID("60000000-0000-4000-8000-000000000006")
RUNNER_FACT_KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"
COMMAND_KEY_ID = "fixture-domain-key-v1"
PRIVATE_KEY_BYTES = bytes(range(1, 33))
ARTIFACT_POLICY_KEY_BYTES = bytes(range(33, 65))
ARTIFACT_REGISTRY = "local.alephain.test"
ARTIFACT_REPOSITORY = "v1-team/strategies"
ARTIFACT_WORKFLOW_IDENTITY = (
    "https://github.com/alchymia-labs/philosophers-stone/"
    ".github/workflows/publish-strategy-artifact.yml@refs/heads/main"
)
ARTIFACT_SOURCE_REPOSITORY = "https://github.com/alchymia-labs/philosophers-stone"
ARTIFACT_TRUSTED_ROOT = b'{"profile":"custos-local-immutable-acceptance-v1"}'
RUNTIME_ARTIFACT_NAME = "resources/config.schema.json"
RUNTIME_ARTIFACT_BYTES = b'{"additionalProperties":false,"type":"object"}'
STRATEGY_WHEEL_NAME = "v1_team_strategy-1.0.0-py3-none-any.whl"
STRATEGY_ENTRY_POINT = "strategies.supertrend:RuntimeAdapter"
STRATEGY_ENTRY_POINT_GROUP = "alephain.strategy_runtime.v1"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _record_hash(payload: bytes) -> str:
    return f"sha256={_base64url(hashlib.sha256(payload).digest())}"


def _wheel_member(name: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_STORED
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o644) << 16
    return member


def _write_immutable_strategy_wheel(path: Path) -> dict[str, object]:
    module = b"""class RuntimeAdapter:
    def build_config(self, effective_config, execution_context):
        del execution_context
        return dict(effective_config)

    def build_strategy(self, config):
        return {"artifact": "immutable-strategy-release-v1", "config": config}
"""
    dist_info = "v1_team_strategy-1.0.0.dist-info"
    entries = [
        ("strategies/__init__.py", b""),
        ("strategies/supertrend.py", module),
        (
            f"{dist_info}/entry_points.txt",
            (f"[{STRATEGY_ENTRY_POINT_GROUP}]\nv1-team = {STRATEGY_ENTRY_POINT}\n").encode("ascii"),
        ),
        (
            f"{dist_info}/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: custos-acceptance\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n",
        ),
        (
            f"{dist_info}/METADATA",
            b"Metadata-Version: 2.3\nName: v1-team-strategy\nVersion: 1.0.0\n",
        ),
        (RUNTIME_ARTIFACT_NAME, RUNTIME_ARTIFACT_BYTES),
    ]
    rows = [[name, _record_hash(payload), str(len(payload))] for name, payload in entries]
    record_name = f"{dist_info}/RECORD"
    rows.append([record_name, "", ""])
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries.append((record_name, record.getvalue().encode("utf-8")))

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries:
            archive.writestr(_wheel_member(name), payload)
    os.chmod(path, 0o400)
    payload = path.read_bytes()
    return {
        "artifact_name": STRATEGY_WHEEL_NAME,
        "artifact_path": str(path.resolve()),
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "artifact_size_bytes": len(payload),
        "entry_point_group": STRATEGY_ENTRY_POINT_GROUP,
        "entry_point_name": STRATEGY_ENTRY_POINT,
        "registry": ARTIFACT_REGISTRY,
        "repository": ARTIFACT_REPOSITORY,
        "runtime_artifact": {
            "media_type": "application/schema+json",
            "name": RUNTIME_ARTIFACT_NAME,
            "role": "runtime_artifact",
            "sha256": hashlib.sha256(RUNTIME_ARTIFACT_BYTES).hexdigest(),
            "size_bytes": len(RUNTIME_ARTIFACT_BYTES),
        },
    }


class _DirectoryOciBlobTransport:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def fetch_blob(
        self,
        *,
        registry: str,
        repository: str,
        digest: str,
        max_bytes: int,
    ) -> bytes:
        if registry != ARTIFACT_REGISTRY or repository != ARTIFACT_REPOSITORY:
            raise StrategyReleaseResolutionRejected(
                "local immutable artifact coordinate differs from acceptance authority"
            )
        path = self._root / "sha256" / digest
        if path.is_symlink() or not path.is_file():
            raise StrategyReleaseResolutionRejected("local immutable artifact blob is absent")
        payload = path.read_bytes()
        if len(payload) > max_bytes or hashlib.sha256(payload).hexdigest() != digest:
            raise StrategyReleaseResolutionRejected("local immutable artifact blob differs")
        return payload


class _AcceptanceSigstoreVerifier:
    capability_id = "local-deterministic-sigstore-acceptance-v1"

    def verify(self, request: SigstoreVerificationRequest) -> SigstoreVerificationEvidence:
        if len(request.accepted_identities) != 1:
            raise StrategyReleaseResolutionRejected(
                "acceptance policy must authorize exactly one producer identity"
            )
        identity = request.accepted_identities[0]
        return SigstoreVerificationEvidence(
            verifier_capability_id=self.capability_id,
            bundle_sha256=hashlib.sha256(request.bundle_path.read_bytes()).hexdigest(),
            trusted_root_sha256=hashlib.sha256(request.trusted_root_bytes).hexdigest(),
            issuer=identity.issuer,
            workflow_identity=identity.workflow_identity,
            source_repository=identity.source_repository,
            verified_subjects=request.required_subjects,
            transparency_log_verified=True,
        )


def _build_acceptance_strategy_release_runtime(
    *,
    state_store: RunnerStateStore,
    material_authority: object,
    material_root: Path,
    cache_root: Path,
    quarantine_root: Path,
    activation_root: Path,
) -> tuple[object, StrategyArtifactRuntimeV1]:
    now = datetime.now(UTC)
    policy_key = Ed25519PrivateKey.from_private_bytes(ARTIFACT_POLICY_KEY_BYTES)
    policy = ReleaseTrustPolicyV1(
        policy_id="custos-local-immutable-acceptance-v1",
        version=1,
        not_before=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
        sigstore_trusted_root_sha256=hashlib.sha256(ARTIFACT_TRUSTED_ROOT).hexdigest(),
        accepted_identities=(
            SigstoreIdentityV1(
                issuer="https://token.actions.githubusercontent.com",
                workflow_identity=ARTIFACT_WORKFLOW_IDENTITY,
                source_repository=ARTIFACT_SOURCE_REPOSITORY,
            ),
        ),
        require_transparency_log=True,
        archive_limits=ArchiveLimitsV1(),
    )
    policy_bytes = canonical_policy_bytes(policy)
    envelope = SignedReleaseTrustPolicyEnvelopeV1(
        policy_bytes=_base64url(policy_bytes),
        signature_key_id="custos-local-artifact-policy-v1",
        signature=_base64url(policy_key.sign(release_policy_signature_message(policy_bytes))),
    ).model_dump_json()
    resolver = CrucibleStrategyReleaseArtifactResolverV1(
        authority=material_authority,
        materializer=RegistryStrategyReleaseMaterializerV1(
            cache_root=cache_root.resolve(),
            transport=_DirectoryOciBlobTransport(material_root),
        ),
    )
    runtime = StrategyArtifactRuntimeV1(
        state=state_store,
        config=ArtifactRuntimeConfigV1(
            local_verification=RunnerLocalArtifactVerificationConfig(
                signed_policy_envelope_bytes=envelope.encode("utf-8"),
                policy_authority_key_id="custos-local-artifact-policy-v1",
                policy_authority_public_key=policy_key.public_key(),
                sigstore_trusted_root_bytes=ARTIFACT_TRUSTED_ROOT,
                quarantine_parent=quarantine_root.resolve(),
            ),
            activation_parent=activation_root.resolve(),
            capability=ArtifactRuntimeCapabilityV1.production_ready(),
        ),
        sigstore_verifier=_AcceptanceSigstoreVerifier(),
    )
    return resolver, runtime


class _AcceptanceCredentialResolver:
    async def resolve(self, verified: object, credential_scope: object) -> dict[str, object]:
        del verified, credential_scope
        return {}


def _capability(
    deployment_authority: Mapping[str, object],
    identity: RunnerFactIdentity,
) -> RunnerCapabilityReceipt:
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
            binding["deployment_instance_id"] = str(deployment_authority["deployment_instance_id"])
            binding["deployment_spec_id"] = str(deployment_authority["deployment_spec_id"])
            binding["deployment_spec_digest"] = deployment_authority["deployment_spec_digest"]
            binding["strategy_id"] = str(deployment_authority["strategy_id"])
            if key == "reconciliation_scope_bindings":
                binding["source_policy_digest"] = deployment_authority["source_policy_digest"]
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
        trading_mode="sandbox",
        deployment_instance_id=UUID(str(deployment_authority["deployment_instance_id"])),
        deployment_spec_id=UUID(str(deployment_authority["deployment_spec_id"])),
        deployment_spec_digest=str(deployment_authority["deployment_spec_digest"]),
        strategy_id=UUID(str(deployment_authority["strategy_id"])),
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


def _machine_credential(path: Path) -> MachineCredential:
    if path.stat().st_mode & 0o777 != 0o600:
        raise PermissionError("machine credential fixture must have mode 0600")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("machine credential fixture must be an object")
    credential = MachineCredential.from_document(document)
    if credential.tenant_id != TENANT_ID or credential.runner_id != RUNNER_ID:
        raise ValueError("machine credential does not match the daemon runtime identity")
    return credential


def _write_runtime_authority_receipt(
    path: Path,
    capability: RunnerCapabilityReceipt,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        payload = json.dumps(
            {
                "capability_manifest_digest": capability.manifest_digest,
                "capability_version": capability.capability_version,
                "capability_version_id": str(capability.capability_version_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _write_artifact_input(path: Path, fixture: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(fixture, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def _wait_for_deployment_authority(path: Path) -> dict[str, object]:
    required = {
        "artifact_digests",
        "command_subject",
        "deployment_instance_id",
        "deployment_spec_digest",
        "deployment_spec_id",
        "generation",
        "source_policy_digest",
        "strategy_id",
        "strategy_release_id",
    }
    for _ in range(400):
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) != required:
                raise RuntimeError("deployment authority rendezvous shape is invalid")
            UUID(str(document["deployment_instance_id"]))
            UUID(str(document["deployment_spec_id"]))
            UUID(str(document["strategy_id"]))
            UUID(str(document["strategy_release_id"]))
            if (
                document["generation"] != 1
                or not isinstance(document["command_subject"], str)
                or any(
                    not isinstance(document[field], str) or len(str(document[field])) != 64
                    for field in (
                        "deployment_spec_digest",
                        "source_policy_digest",
                    )
                )
                or not isinstance(document["artifact_digests"], list)
                or len(document["artifact_digests"]) != 3
                or any(
                    not isinstance(digest, str) or len(digest) != 64
                    for digest in document["artifact_digests"]
                )
            ):
                raise RuntimeError("deployment authority rendezvous values are invalid")
            return document
        await asyncio.sleep(0.05)
    raise TimeoutError("Crucible did not publish deployment authority")


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _assert_encrypted_vault(path: Path, forbidden_values: tuple[bytes, ...]) -> None:
    if path.stat().st_mode & 0o777 != 0o600:
        raise PermissionError(f"encrypted vault must have mode 0600: {path}")
    ciphertext = path.read_bytes()
    for forbidden in forbidden_values:
        if forbidden and forbidden in ciphertext:
            raise RuntimeError(f"encrypted vault contains plaintext secret bytes: {path}")


def _install_acceptance_authorities(
    *,
    machine_credential: MachineCredential,
    metadata: RunnerToml,
    transport_credential: RunnerNatsTransportCredential,
    capability: RunnerCapabilityReceipt,
    verify_machine_authority_with_crucible: bool,
    use_encrypted_authority_vaults: bool,
    artifact_material_dir: Path,
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
        return _build_acceptance_strategy_release_runtime(
            state_store=state_store,
            material_authority=material_authority,
            material_root=artifact_material_dir,
            cache_root=args.artifact_cache_dir,
            quarantine_root=args.artifact_quarantine_dir,
            activation_root=args.artifact_activation_dir,
        )

    def build_credential_resolver(vault: object) -> _AcceptanceCredentialResolver:
        del vault
        return _AcceptanceCredentialResolver()

    if not use_encrypted_authority_vaults:
        daemon_module.RunnerToml = StaticRunnerToml  # type: ignore[misc]
        daemon_module.MachineCredentialVault = (  # type: ignore[misc]
            StaticMachineCredentialVault
        )
        daemon_module.RunnerNatsTransportSet = StaticTransportSet  # type: ignore[misc]
    if not verify_machine_authority_with_crucible:
        daemon_module.MachineCredentialHttpClient = (  # type: ignore[misc]
            StaticMachineCredentialHttpClient
        )
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


def _runner_policy_is_durable(database: Path) -> bool:
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM runner_cap_policy_head AS head
                JOIN runner_cap_policy AS policy ON policy.policy_id = head.policy_id
                WHERE head.tenant_scope = ?
                  AND head.trading_mode = 'sandbox'
                  AND head.runner_id = ?
                """,
                (TENANT_ID, str(RUNNER_ID)),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row[0] == 1


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
) -> tuple[dict[str, object], Any]:
    outbox = RunnerFactOutbox(database)
    for _ in range(400):
        if task.done():
            await task
            raise RuntimeError("daemon exited before command publication")
        outcome = _command_outcome(database)
        if outcome is not None:
            batch_id = UUID(str(outcome["lifecycle_batch_id"]))
            receipt = await outbox.publication_receipt(batch_id)
            if (
                receipt is not None
                and not await outbox.pending()
                and _runner_policy_is_durable(database)
            ):
                return outcome, receipt
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not publish the lifecycle batch")


async def _stop_daemon(task: asyncio.Task[int]) -> None:
    if not task.done():
        os.kill(os.getpid(), signal.SIGTERM)
    exit_code = await asyncio.wait_for(task, timeout=10)
    if exit_code != 0:
        raise RuntimeError(f"daemon exited with status {exit_code}")


async def _wait_for_rotation_intent(path: Path) -> UUID:
    for _ in range(200):
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            if set(document) != {"authorization_intent_id"}:
                raise RuntimeError("rotation authorization intent shape is invalid")
            return UUID(str(document["authorization_intent_id"]))
        await asyncio.sleep(0.05)
    raise TimeoutError("rotation authorization intent was not published")


async def _run(args: argparse.Namespace) -> dict[str, object]:
    root = args.database.parent
    command_private_key = Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY_BYTES)
    service_authority = args.machine_credential is not None
    transport_operation_id: str | None = None
    if service_authority:
        if (
            args.transport_credential is not None
            or not args.crucible_url
            or args.authorization_intent_id is None
            or args.runtime_authority_ready_file is None
            or args.rotation_intent_file is None
            or not args.age_recipient
        ):
            raise ValueError(
                "service authority mode requires machine credential, Crucible URL, "
                "authorization intent, runtime and rotation rendezvous files, and "
                "age recipient without a static transport credential"
            )
        machine_credential = _machine_credential(args.machine_credential)
        authority = RunnerNatsTransportAuthorityClient(
            args.crucible_url,
            machine_credential,
        )
        operation = authority.prepare_initial(
            authorization_intent_id=args.authorization_intent_id,
            trading_mode="sandbox",
            expected_issuer_public_key=args.pinned_issuer_public_key,
        )
        completion = await asyncio.to_thread(
            authority.execute,
            operation,
            timeout_seconds=args.operation_timeout_secs,
        )
        if completion.credential is None:
            raise RuntimeError("service-issued transport operation returned no credential")
        transport_credential = completion.credential
        transport_operation_id = str(completion.operation_id)
        backend_url = args.crucible_url
    else:
        if args.transport_credential is None:
            raise ValueError("static acceptance mode requires --transport-credential")
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
        backend_url = "http://127.0.0.1:9"
    artifact_fixture = _write_immutable_strategy_wheel(
        args.artifact_material_dir / "input" / STRATEGY_WHEEL_NAME
    )
    _write_artifact_input(args.artifact_input_file, artifact_fixture)
    deployment_authority = await _wait_for_deployment_authority(args.deployment_authority_file)
    identity = RunnerFactIdentity.from_private_bytes(
        machine_credential.private_key_bytes,
        machine_credential.machine_key_id,
    )
    capability = _capability(deployment_authority, identity)
    machine_vault_path = root / "runner-machine.enc"
    transport_vault_dir = root / "nats-transport"
    metadata = RunnerToml(
        tenant_id=TENANT_ID,
        runner_id=str(RUNNER_ID),
        backend_url=backend_url,
        credential_id=str(machine_credential.credential_id),
        credential_version=machine_credential.credential_version,
        credential_valid_until=_timestamp_text(machine_credential.credential_valid_until),
        machine_key_id=machine_credential.machine_key_id,
        machine_vault_path=str(machine_vault_path.resolve()),
        enrolled_at=_timestamp_text(datetime.now(UTC).replace(microsecond=0)),
    )
    encrypted_machine_vault_used = False
    encrypted_transport_vault_used = False
    plaintext_machine_bootstrap_removed = False
    authority_vault_digests: tuple[bytes, bytes] | None = None
    if service_authority:
        assert args.machine_credential is not None
        assert args.age_recipient is not None
        RunnerToml.write(root / "runner.toml", metadata)
        MachineCredentialVault(machine_vault_path).persist(
            machine_credential,
            age_recipient=args.age_recipient,
        )
        RunnerNatsTransportVault(transport_vault_dir, "sandbox").persist(
            RunnerNatsTransportBundle(
                active=transport_credential,
                pending_operation=None,
            ),
            age_recipient=args.age_recipient,
        )
        _assert_encrypted_vault(
            machine_vault_path,
            (
                machine_credential.machine_credential.encode("utf-8"),
                base64.b64encode(machine_credential.private_key_bytes),
            ),
        )
        _assert_encrypted_vault(
            transport_vault_dir / "sandbox.enc",
            (
                transport_credential.user_seed,
                transport_credential.user_jwt.encode("utf-8"),
            ),
        )
        args.machine_credential.unlink()
        plaintext_machine_bootstrap_removed = not args.machine_credential.exists()
        encrypted_machine_vault_used = True
        encrypted_transport_vault_used = True
        authority_vault_digests = (
            hashlib.sha256(machine_vault_path.read_bytes()).digest(),
            hashlib.sha256((transport_vault_dir / "sandbox.enc").read_bytes()).digest(),
        )
    _install_acceptance_authorities(
        machine_credential=machine_credential,
        metadata=metadata,
        transport_credential=transport_credential,
        capability=capability,
        verify_machine_authority_with_crucible=service_authority,
        use_encrypted_authority_vaults=service_authority,
        artifact_material_dir=args.artifact_material_dir,
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
    daemon_launch_count = 0
    daemon_restart_completed = False
    encrypted_vaults_reloaded_after_restart = False
    transport_rotation_completed = False
    rotation_pending_operation_cleared = False
    rotation_revocation_receipt_verified = False
    old_generation_reconnect_denied = False
    rotated_vault_reloaded_by_daemon = False
    active_transport_credential = transport_credential
    if service_authority:
        first_daemon_task = asyncio.create_task(
            daemon_module.run_daemon(
                _runtime_args(args, command_public_key_path=command_public_key_path)
            ),
            name="custos-daemon-restart-preflight",
        )
        daemon_launch_count += 1
        try:
            await _wait_for_daemon_ready(first_daemon_task, args.ready_file)
        finally:
            await _stop_daemon(first_daemon_task)
        if args.ready_file.exists():
            raise RuntimeError("daemon did not clear readiness before restart")

    daemon_task = asyncio.create_task(
        daemon_module.run_daemon(
            _runtime_args(args, command_public_key_path=command_public_key_path)
        ),
        name="custos-daemon-acceptance",
    )
    daemon_launch_count += 1
    try:
        await _wait_for_daemon_ready(daemon_task, args.ready_file)
        if service_authority:
            assert authority_vault_digests is not None
            reloaded_vault_digests = (
                hashlib.sha256(machine_vault_path.read_bytes()).digest(),
                hashlib.sha256((transport_vault_dir / "sandbox.enc").read_bytes()).digest(),
            )
            if reloaded_vault_digests != authority_vault_digests:
                raise RuntimeError("encrypted authority vault bytes changed across restart")
            daemon_restart_completed = True
            encrypted_vaults_reloaded_after_restart = True
            assert args.runtime_authority_ready_file is not None
            _write_runtime_authority_receipt(
                args.runtime_authority_ready_file,
                capability,
            )
        outcome, publication = await _wait_for_publication(daemon_task, args.database)
    finally:
        await _stop_daemon(daemon_task)

    if service_authority:
        assert args.rotation_intent_file is not None
        assert args.age_recipient is not None
        rotation_authorization_intent_id = await _wait_for_rotation_intent(
            args.rotation_intent_file
        )
        rotation_exit_code = await asyncio.to_thread(
            nats_transport_cli.run,
            SimpleNamespace(
                transport_action="rotate",
                runner_toml=root / "runner.toml",
                machine_vault=None,
                transport_vault_dir=transport_vault_dir,
                trading_mode="sandbox",
                crucible_url=args.crucible_url,
                authorization_intent_id=rotation_authorization_intent_id,
                issuer_public_key=args.pinned_issuer_public_key,
                nats_url=args.nats_url,
                nats_ca=args.ca_path,
                nats_server_name=args.server_name,
                operation_timeout_secs=args.operation_timeout_secs,
                verification_timeout_secs=10.0,
                age_recipient=args.age_recipient,
            ),
        )
        if rotation_exit_code != 0:
            raise RuntimeError("production NATS transport rotation failed")
        rotated_bundle = RunnerNatsTransportVault(
            transport_vault_dir,
            "sandbox",
        ).load()
        if rotated_bundle.active is None:
            raise RuntimeError("rotated transport vault has no active credential")
        if rotated_bundle.pending_operation is not None:
            raise RuntimeError("rotated transport vault retained a pending operation")
        if (
            rotated_bundle.active.credential_generation
            != transport_credential.credential_generation + 1
        ):
            raise RuntimeError("transport credential generation did not advance exactly once")
        if rotated_bundle.active.user_public_key == transport_credential.user_public_key:
            raise RuntimeError("transport rotation reused the retired User NKey")
        active_transport_credential = rotated_bundle.active
        transport_rotation_completed = True
        rotation_pending_operation_cleared = True
        rotation_revocation_receipt_verified = True
        old_generation_reconnect_denied = True

        rotated_daemon_task = asyncio.create_task(
            daemon_module.run_daemon(
                _runtime_args(args, command_public_key_path=command_public_key_path)
            ),
            name="custos-daemon-rotated-authority",
        )
        daemon_launch_count += 1
        try:
            await _wait_for_daemon_ready(rotated_daemon_task, args.ready_file)
            rotated_vault_reloaded_by_daemon = True
        finally:
            await _stop_daemon(rotated_daemon_task)

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
    with sqlite3.connect(args.database) as connection:
        activation = connection.execute(
            """
            SELECT a.activation_id, a.state, d.artifact_activation_id
            FROM artifact_activation AS a
            JOIN applied_deployments AS d
              ON d.deployment_instance_id = a.deployment_instance_id
            WHERE a.deployment_instance_id = ?
            """,
            (str(deployment_authority["deployment_instance_id"]),),
        ).fetchone()
    cached_digests = {
        path.name
        for path in (root / "artifact-cache" / "sha256").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    artifact_digests = deployment_authority["artifact_digests"]
    if not isinstance(artifact_digests, list):
        raise RuntimeError("deployment authority artifact digests are invalid")
    expected_artifact_digests = set(artifact_digests)
    immutable_artifact_materialized = (
        activation is not None
        and activation[1] == "active"
        and activation[0] == activation[2]
        and cached_digests == expected_artifact_digests
        and len(list((root / "artifact-activation").rglob("strategies/supertrend.py"))) == 1
    )
    if not immutable_artifact_materialized:
        raise RuntimeError("immutable StrategyRelease materialization receipt is incomplete")
    policy_document = effective_policy.policy.model_dump(mode="json")
    return {
        "batch_id": str(publication.batch_id),
        "subject": publication.subject,
        "payload_sha256": publication.batch_payload_sha256,
        "command_acked": command_acked,
        "runtime_status": "applied_acked" if command_acked else "unexpected",
        "engine_ready": engine_ready,
        "deployment_instance_id": str(deployment_authority["deployment_instance_id"]),
        "lifecycle_fact_kind": "RunnerDeploymentLifecycleFact.v1",
        "lifecycle_state": "running",
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
        "production_authority_issued": service_authority,
        "production_policy_issued": False,
        "immutable_artifact_materialized": immutable_artifact_materialized,
        "strategy_release_id": str(deployment_authority["strategy_release_id"]),
        "command_subject": str(deployment_authority["command_subject"]),
        "transport_operation_id": transport_operation_id,
        "transport_credential_generation": active_transport_credential.credential_generation,
        "transport_rotation_operation_id": (
            str(active_transport_credential.operation_id) if transport_rotation_completed else None
        ),
        "encrypted_machine_vault_used": encrypted_machine_vault_used,
        "encrypted_transport_vault_used": encrypted_transport_vault_used,
        "plaintext_machine_bootstrap_removed": plaintext_machine_bootstrap_removed,
        "daemon_launch_count": daemon_launch_count,
        "daemon_restart_completed": daemon_restart_completed,
        "encrypted_vaults_reloaded_after_restart": encrypted_vaults_reloaded_after_restart,
        "transport_rotation_completed": transport_rotation_completed,
        "rotation_pending_operation_cleared": rotation_pending_operation_cleared,
        "rotation_revocation_receipt_verified": rotation_revocation_receipt_verified,
        "old_generation_reconnect_denied": old_generation_reconnect_denied,
        "rotated_vault_reloaded_by_daemon": rotated_vault_reloaded_by_daemon,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--transport-credential", type=Path)
    parser.add_argument("--machine-credential", type=Path)
    parser.add_argument("--crucible-url")
    parser.add_argument("--authorization-intent-id", type=UUID)
    parser.add_argument("--artifact-input-file", type=Path, required=True)
    parser.add_argument("--artifact-material-dir", type=Path, required=True)
    parser.add_argument("--deployment-authority-file", type=Path, required=True)
    parser.add_argument("--runtime-authority-ready-file", type=Path)
    parser.add_argument("--rotation-intent-file", type=Path)
    parser.add_argument("--age-recipient")
    parser.add_argument("--operation-timeout-secs", type=float, default=30.0)
    parser.add_argument("--ca-path", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--pinned-issuer-public-key", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from custos_toolkit.contracts.strategy_execution import (
    DevelopmentSourceRefV1,
    FrozenJsonObject,
    RunnerLocalArtifactPolicyDecisionV1,
    StrategyArtifactPreImportVerificationReceiptV1,
    StrategyArtifactRefV1,
    StrategyExecutionContextV1,
    canonical_json_bytes,
    canonical_json_digest,
    deep_freeze_json,
)

from custos.artifacts.activation import (
    ArtifactActivationCandidateV1,
    DurableArtifactActivatorV1,
    DurableArtifactRuntimeState,
    RuntimeEntryPointLoader,
)
from custos.artifacts.archive import QuarantinedWheel, quarantine_wheel
from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import ArchiveLimitsV1, verify_signed_release_policy
from custos.artifacts.verification_types import (
    DigestSubject,
    RunnerLocalArtifactVerificationConfig,
    SigstoreVerificationEvidence,
    SigstoreVerificationRequest,
    SigstoreVerifierCapability,
)
from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import StrategyReleaseArtifactSourceV1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^(?P<second>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z$"
)
_STRATEGY_RELEASE_SNAPSHOT_FIELDS = {
    "release_id",
    "definition_id",
    "lifecycle_version",
    "release_number",
    "artifact_ref_digest",
    "manifest_digest",
    "release_bom_digest",
    "artifact_evidence_digest",
    "validated_at",
    "snapshot_digest",
}


def _strategy_release_snapshot_digest(snapshot: Mapping[str, object]) -> str:
    if set(snapshot) != _STRATEGY_RELEASE_SNAPSHOT_FIELDS:
        raise ValueError("StrategyRelease snapshot shape is not exact V1")
    try:
        release_id = UUID(str(snapshot["release_id"]))
        definition_id = UUID(str(snapshot["definition_id"]))
    except (ValueError, TypeError) as error:
        raise ValueError("StrategyRelease snapshot identity is invalid") from error
    integers: list[bytes] = []
    for field in ("lifecycle_version", "release_number"):
        value = snapshot[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value >= 2**64:
            raise ValueError(f"StrategyRelease snapshot {field} is invalid")
        integers.append(value.to_bytes(8, "big"))
    digests: list[bytes] = []
    for field in (
        "artifact_ref_digest",
        "manifest_digest",
        "release_bom_digest",
        "artifact_evidence_digest",
    ):
        value = snapshot[field]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(f"StrategyRelease snapshot {field} is invalid")
        digests.append(value.encode("ascii"))
    timestamp = snapshot["validated_at"]
    match = _UTC_TIMESTAMP.fullmatch(timestamp) if isinstance(timestamp, str) else None
    if match is None:
        raise ValueError("StrategyRelease snapshot validated_at is not canonical UTC")
    validated_at = (
        f"{match.group('second')}."
        f"{(match.group('fraction') or '').ljust(9, '0')}Z"
    ).encode("ascii")
    hasher = hashlib.sha256()
    hasher.update(b"strategy-release-snapshot-v1")
    for part in (
        release_id.bytes,
        definition_id.bytes,
        *integers,
        *digests,
        validated_at,
    ):
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return hasher.hexdigest()


class ArtifactRuntimeBlocked(RuntimeError):
    """The V1 artifact authority or a required local verifier is unavailable."""


class ArtifactRuntimeActivationError(RuntimeError):
    """A verified artifact could not become a durable visible activation."""


@dataclass(frozen=True, slots=True)
class ArtifactRuntimeCapabilityV1:
    status: Literal["BLOCKED", "READY"]
    blocked_reason: str | None

    @classmethod
    def blocked(cls, reason: str) -> ArtifactRuntimeCapabilityV1:
        if not reason.strip():
            raise ValueError("blocked artifact runtime capability requires a reason")
        return cls(status="BLOCKED", blocked_reason=reason.strip())

    @classmethod
    def production_ready(cls) -> ArtifactRuntimeCapabilityV1:
        return cls(status="READY", blocked_reason=None)

    @property
    def ready(self) -> bool:
        return self.status == "READY" and self.blocked_reason is None


@dataclass(frozen=True, slots=True)
class ArtifactRuntimeConfigV1:
    local_verification: RunnerLocalArtifactVerificationConfig
    activation_parent: Path
    capability: ArtifactRuntimeCapabilityV1

    def __post_init__(self) -> None:
        if not self.activation_parent.is_absolute():
            raise ValueError("runner-local activation parent must be an absolute path")


@dataclass(frozen=True, slots=True)
class VerifiedArtifactMemberV1:
    role: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    path: Path


@dataclass(frozen=True, slots=True)
class StrategyReleaseArtifactAuthorityV1:
    """Immutable Crucible-owned release material consumed by the runner.

    The resolver that constructs this value owns transport authentication and
    StrategyRelease authorization. Custos only verifies execution bytes and
    binds them to the signed DeploymentSpec digests.
    """

    strategy_release_id: UUID
    strategy_release_snapshot_bytes: bytes
    release_bom: dict[str, object]
    release_bom_digest: str
    artifact_ref: StrategyArtifactRefV1
    artifact_ref_digest: str
    detached_attestation_ref: dict[str, object]
    crucible_artifact_evidence: dict[str, object]
    crucible_artifact_evidence_digest: str
    crucible_artifact_evidence_digest_input_bytes: bytes
    crucible_artifact_acceptance: dict[str, object]
    crucible_artifact_acceptance_receipt_digest: str

    def __post_init__(self) -> None:
        if self.strategy_release_id.int == 0:
            raise ValueError("strategy_release_id must not be nil")
        digest_values = (
            self.release_bom_digest,
            self.artifact_ref_digest,
            self.crucible_artifact_evidence_digest,
            self.crucible_artifact_acceptance_receipt_digest,
        )
        if any(_SHA256.fullmatch(value) is None for value in digest_values):
            raise ValueError("StrategyRelease authority digests must be lowercase SHA-256")
        if canonical_json_digest(self.release_bom) != self.release_bom_digest:
            raise ValueError("release_bom_digest differs from release BOM")
        if (
            canonical_json_digest(self.artifact_ref.model_dump(mode="json"))
            != self.artifact_ref_digest
        ):
            raise ValueError("artifact_ref_digest differs from StrategyArtifactRefV1")
        if (
            hashlib.sha256(self.crucible_artifact_evidence_digest_input_bytes).hexdigest()
            != self.crucible_artifact_evidence_digest
        ):
            raise ValueError("artifact evidence digest differs from Crucible digest input")
        evidence_input = _strict_json_object(
            self.crucible_artifact_evidence_digest_input_bytes,
            "Crucible artifact evidence digest input",
            require_canonical=False,
        )
        expected_evidence_input = dict(self.crucible_artifact_evidence)
        expected_evidence_input["composite_evidence_digest"] = ""
        if evidence_input != expected_evidence_input:
            raise ValueError("artifact evidence differs from Crucible digest input")
        snapshot = _strict_json_object(
            self.strategy_release_snapshot_bytes,
            "StrategyRelease snapshot",
        )
        if snapshot != self.crucible_artifact_acceptance:
            raise ValueError("StrategyRelease snapshot differs from Crucible acceptance")
        if (
            self.strategy_release_snapshot_digest
            != self.crucible_artifact_acceptance_receipt_digest
        ):
            raise ValueError("StrategyRelease snapshot digest differs from acceptance receipt")

    @property
    def strategy_release_snapshot_digest(self) -> str:
        snapshot = _strict_json_object(
            self.strategy_release_snapshot_bytes,
            "StrategyRelease snapshot",
        )
        return _strategy_release_snapshot_digest(snapshot)

    def assert_command_binding(self, command: CrucibleRunnerDeploymentCommandV1) -> None:
        source = command.artifact_source
        if not isinstance(source, StrategyReleaseArtifactSourceV1):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.COMMAND_BINDING_INVALID,
                "StrategyRelease material cannot satisfy a development-source command",
            )
        snapshot = source.snapshot
        expected = (
            (self.strategy_release_id, snapshot.release_id),
            (self.strategy_release_snapshot_digest, snapshot.snapshot_digest),
            (self.artifact_ref.artifact_sha256, snapshot.artifact_digest),
            (self.artifact_ref.manifest_sha256, snapshot.manifest_digest),
        )
        if any(actual != signed for actual, signed in expected):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.COMMAND_BINDING_INVALID,
                "StrategyRelease material differs from signed DeploymentSpec authority",
            )


@dataclass(frozen=True, slots=True)
class PreparedStrategyArtifact:
    command: CrucibleRunnerDeploymentCommandV1
    release_authority: StrategyReleaseArtifactAuthorityV1
    activation_id: str
    receipt: StrategyArtifactPreImportVerificationReceiptV1
    verified_members: tuple[VerifiedArtifactMemberV1, ...]
    quarantine_root: Path
    verified_entry_point: str
    effective_config: FrozenJsonObject
    execution_context: StrategyExecutionContextV1


@dataclass(frozen=True, slots=True)
class ActivatedStrategyArtifact:
    prepared: PreparedStrategyArtifact
    activation_root: Path
    strategy: object

    @property
    def activation_id(self) -> str:
        return self.prepared.activation_id


RuntimeArtifactSource: TypeAlias = PreparedStrategyArtifact | DevelopmentSourceRefV1


class ArtifactMemberVerifier(Protocol):
    def verify(
        self,
        release_bom: Mapping[str, object],
        member_paths: Mapping[str, Path],
    ) -> tuple[VerifiedArtifactMemberV1, ...]: ...


class ArtifactQuarantineCapability(Protocol):
    def quarantine(
        self,
        *,
        wheel_path: Path,
        entry_point_group: str,
        entry_point: str,
        limits: ArchiveLimitsV1,
        quarantine_parent: Path,
    ) -> QuarantinedWheel: ...


def _strict_json_object(
    payload: bytes,
    label: str,
    *,
    require_canonical: bool = True,
) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            f"{label} is not strict UTF-8 JSON",
        ) from error
    if not isinstance(value, dict) or (
        require_canonical and canonical_json_bytes(value) != payload
    ):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            f"{label} is not an exact canonical JSON object",
        )
    return value


def _read_stable_member(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ArtifactVerificationError(
            ArtifactVerificationCode.MEMBER_UNSTABLE,
            f"BOM member is not a stable regular file: {label}",
        )
    before = path.stat()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.MEMBER_UNSTABLE,
            f"BOM member cannot be read: {label}",
        ) from error
    after = path.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or len(payload) != after.st_size:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.MEMBER_UNSTABLE,
            f"BOM member changed while being read: {label}",
        )
    return payload


def verify_full_bom_member_files(
    release_bom: Mapping[str, object],
    member_paths: Mapping[str, Path],
) -> tuple[VerifiedArtifactMemberV1, ...]:
    """Derive the only member table from the full PS BOM and verify every byte."""

    members = release_bom.get("members")
    if not isinstance(members, list) or not members:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.MEMBER_SET_MISMATCH,
            "full release BOM members must be a non-empty array",
        )
    expected_names = {member.get("name") for member in members if isinstance(member, Mapping)}
    if (
        len(expected_names) != len(members)
        or any(not isinstance(name, str) or not name for name in expected_names)
        or set(member_paths) != expected_names
    ):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.MEMBER_SET_MISMATCH,
            "member paths must exactly match names derived from the full release BOM",
        )
    verified: list[VerifiedArtifactMemberV1] = []
    for raw in members:
        if not isinstance(raw, Mapping):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                "release BOM member is not an object",
            )
        name = cast(str, raw["name"])
        role = raw.get("role")
        media_type = raw.get("media_type")
        size_bytes = raw.get("size_bytes")
        digest = raw.get("sha256")
        if (
            not isinstance(role, str)
            or not isinstance(media_type, str)
            or type(size_bytes) is not int
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                f"release BOM member metadata is invalid: {name}",
            )
        path = member_paths[name]
        payload = _read_stable_member(path, label=name)
        if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != digest:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                f"release BOM member bytes differ: {name}",
            )
        verified.append(
            VerifiedArtifactMemberV1(
                role=role,
                name=name,
                media_type=media_type,
                size_bytes=size_bytes,
                sha256=digest,
                path=path,
            )
        )
    return tuple(verified)


class FullBomMemberVerifier:
    def verify(
        self,
        release_bom: Mapping[str, object],
        member_paths: Mapping[str, Path],
    ) -> tuple[VerifiedArtifactMemberV1, ...]:
        return verify_full_bom_member_files(release_bom, member_paths)


class ProductionArtifactQuarantiner:
    def quarantine(
        self,
        *,
        wheel_path: Path,
        entry_point_group: str,
        entry_point: str,
        limits: ArchiveLimitsV1,
        quarantine_parent: Path,
    ) -> QuarantinedWheel:
        return quarantine_wheel(
            wheel_path,
            entry_point_group=entry_point_group,
            entry_point=entry_point,
            limits=limits,
            quarantine_parent=quarantine_parent,
        )


def _statement_subjects(statement: Mapping[str, object]) -> tuple[DigestSubject, ...]:
    raw_subjects = statement.get("subject")
    if not isinstance(raw_subjects, list) or not raw_subjects:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "release statement has no signed subjects",
        )
    subjects: list[DigestSubject] = []
    for raw in raw_subjects:
        if not isinstance(raw, Mapping):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "release statement subject is not an object",
            )
        name = raw.get("name")
        digest = raw.get("digest")
        sha256 = digest.get("sha256") if isinstance(digest, Mapping) else None
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "release statement subject lacks exact SHA-256",
            )
        subjects.append(DigestSubject(name=name, sha256=sha256))
    if len({item.name for item in subjects}) != len(subjects):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "release statement subject names are duplicated",
        )
    return tuple(subjects)


def _validate_sigstore_against_crucible(
    evidence: SigstoreVerificationEvidence,
    request: SigstoreVerificationRequest,
    authority: StrategyReleaseArtifactAuthorityV1,
) -> None:
    claims = authority.crucible_artifact_evidence.get("signed_producer_claims")
    proof = authority.crucible_artifact_evidence.get("sigstore_proof")
    if not isinstance(claims, Mapping) or not isinstance(proof, Mapping):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "Crucible evidence lacks signed producer claims or Sigstore proof",
        )
    expected_identity = (
        proof.get("issuer"),
        claims.get("workflow_identity"),
        claims.get("producer_repository"),
    )
    actual_identity = (
        evidence.issuer,
        evidence.workflow_identity,
        evidence.source_repository,
    )
    expected_subjects = {(item.name, item.sha256) for item in request.required_subjects}
    actual_subjects = {(item.name, item.sha256) for item in evidence.verified_subjects}
    if (
        evidence.verifier_capability_id == ""
        or evidence.bundle_sha256 != authority.detached_attestation_ref.get("bundle_sha256")
        or evidence.bundle_sha256 != proof.get("bundle_sha256")
        or evidence.trusted_root_sha256 != hashlib.sha256(request.trusted_root_bytes).hexdigest()
        or actual_identity != expected_identity
        or actual_identity
        not in {
            (item.issuer, item.workflow_identity, item.source_repository)
            for item in request.accepted_identities
        }
        or actual_subjects != expected_subjects
        or not evidence.transparency_log_verified
    ):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "detached bundle verification differs from local policy or Crucible evidence",
        )


class StrategyArtifactRuntimeV1:
    def __init__(
        self,
        *,
        state: DurableArtifactRuntimeState,
        config: ArtifactRuntimeConfigV1,
        sigstore_verifier: SigstoreVerifierCapability,
        member_verifier: ArtifactMemberVerifier | None = None,
        quarantiner: ArtifactQuarantineCapability | None = None,
    ) -> None:
        self._state = state
        self._config = config
        self._sigstore_verifier = sigstore_verifier
        self._member_verifier = member_verifier or FullBomMemberVerifier()
        self._quarantiner = quarantiner or ProductionArtifactQuarantiner()
        self._activator = DurableArtifactActivatorV1(
            state=state,
            activation_parent=config.activation_parent,
        )

    @property
    def capability_ready(self) -> bool:
        return self._config.capability.ready

    async def prepare(
        self,
        *,
        deployment_instance_id: UUID,
        release_authority: StrategyReleaseArtifactAuthorityV1,
        release_statement_bytes: bytes,
        detached_bundle_path: Path,
        member_paths: Mapping[str, Path],
        verified_at: datetime,
    ) -> PreparedStrategyArtifact:
        if not self._config.capability.ready:
            raise ArtifactRuntimeBlocked(
                self._config.capability.blocked_reason or "artifact runtime capability is not READY"
            )
        durable = await self._state.load_durable_desired_command(deployment_instance_id)
        command = durable.command
        if not isinstance(command, CrucibleRunnerDeploymentCommandV1):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.COMMAND_BINDING_INVALID,
                "artifact runtime requires the durable runner-command model",
            )
        if command.deployment_instance_id != deployment_instance_id:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.COMMAND_BINDING_INVALID,
                "durable desired command differs from requested deployment instance",
            )
        release_authority.assert_command_binding(command)

        local = self._config.local_verification
        verified_policy = verify_signed_release_policy(
            local.signed_policy_envelope_bytes,
            authority_key_id=local.policy_authority_key_id,
            authority_public_key=local.policy_authority_public_key,
            sigstore_trusted_root_bytes=local.sigstore_trusted_root_bytes,
            now=verified_at,
        )
        statement = _strict_json_object(release_statement_bytes, "release statement")
        statement_digest = hashlib.sha256(release_statement_bytes).hexdigest()
        if statement_digest != release_authority.detached_attestation_ref.get("statement_sha256"):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "release statement bytes differ from detached attestation reference",
            )
        bundle_bytes = _read_stable_member(detached_bundle_path, label="detached bundle")
        if hashlib.sha256(
            bundle_bytes
        ).hexdigest() != release_authority.detached_attestation_ref.get("bundle_sha256"):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "detached bundle bytes differ from detached attestation reference",
            )

        verified_members = self._member_verifier.verify(
            release_authority.release_bom,
            member_paths,
        )
        strategy_wheels = [item for item in verified_members if item.role == "strategy_wheel"]
        if len(strategy_wheels) != 1:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                "full release BOM must resolve one verified strategy wheel",
            )
        subjects = _statement_subjects(statement)
        sigstore_request = SigstoreVerificationRequest(
            bundle_path=detached_bundle_path,
            trusted_root_bytes=local.sigstore_trusted_root_bytes,
            accepted_identities=verified_policy.policy.accepted_identities,
            required_subjects=subjects,
            quarantine_parent=local.quarantine_parent,
        )
        try:
            sigstore_evidence = self._sigstore_verifier.verify(sigstore_request)
        except ArtifactVerificationError:
            raise
        except Exception as error:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
                "detached bundle verifier failed",
            ) from error
        _validate_sigstore_against_crucible(
            sigstore_evidence,
            sigstore_request,
            release_authority,
        )

        entry_point_group = release_authority.release_bom.get("entry_point_group")
        entry_point = release_authority.release_bom.get("entry_point_name")
        if not isinstance(entry_point_group, str) or not isinstance(entry_point, str):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MANIFEST_INVALID,
                "full release BOM lacks the typed strategy entry point",
            )
        quarantined = self._quarantiner.quarantine(
            wheel_path=strategy_wheels[0].path,
            entry_point_group=entry_point_group,
            entry_point=entry_point,
            limits=verified_policy.policy.archive_limits,
            quarantine_parent=local.quarantine_parent,
        )

        policy_decision = RunnerLocalArtifactPolicyDecisionV1(
            authority="custos-runner-local",
            policy_id=verified_policy.policy.policy_id,
            policy_version=verified_policy.policy.version,
            policy_digest=verified_policy.policy_digest,
            evaluated_at=verified_at,
            decision="accepted",
            release_bom_digest=release_authority.release_bom_digest,
            artifact_ref_digest=release_authority.artifact_ref_digest,
            artifact_evidence_digest=release_authority.crucible_artifact_evidence_digest,
            artifact_acceptance_receipt_digest=(
                release_authority.crucible_artifact_acceptance_receipt_digest
            ),
        )
        receipt = StrategyArtifactPreImportVerificationReceiptV1(
            verification_profile="custos-artifact-pre-import-verification-v1",
            verified_at=verified_at,
            release_bom=release_authority.release_bom,
            release_bom_digest=release_authority.release_bom_digest,
            release_statement=statement,
            release_statement_digest=statement_digest,
            artifact_ref=release_authority.artifact_ref,
            artifact_ref_digest=release_authority.artifact_ref_digest,
            detached_attestation_ref=release_authority.detached_attestation_ref,
            detached_attestation_ref_digest=canonical_json_digest(
                release_authority.detached_attestation_ref
            ),
            crucible_artifact_evidence=release_authority.crucible_artifact_evidence,
            crucible_artifact_evidence_digest=(release_authority.crucible_artifact_evidence_digest),
            crucible_artifact_acceptance=release_authority.crucible_artifact_acceptance,
            crucible_artifact_acceptance_receipt_digest=(
                release_authority.crucible_artifact_acceptance_receipt_digest
            ),
            runner_local_policy_decision=policy_decision,
        )
        runtime_spec = command.to_runtime_spec()
        frozen = deep_freeze_json(runtime_spec.strategy_config)
        if not isinstance(frozen, Mapping):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.COMMAND_BINDING_INVALID,
                "effective configuration is not a JSON object",
            )
        execution_context = StrategyExecutionContextV1(
            engine="nautilus",
            trading_mode=command.trading_mode,
            deployment_instance_id=command.deployment_instance_id,
            deployment_spec_id=command.deployment_spec_id,
            deployment_spec_digest=command.deployment_spec_digest,
            effective_config_digest=canonical_json_digest(runtime_spec.strategy_config),
            generation=command.generation,
        )
        activation_id = str(
            uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "custos-artifact-activation-v1",
                        str(command.deployment_instance_id),
                        str(command.deployment_spec_id),
                        str(command.generation),
                        command.strategy_artifact_digest,
                    )
                ),
            )
        )
        return PreparedStrategyArtifact(
            command=command,
            release_authority=release_authority,
            activation_id=activation_id,
            receipt=receipt,
            verified_members=verified_members,
            quarantine_root=quarantined.root,
            verified_entry_point=quarantined.verified_entry_point,
            effective_config=cast(FrozenJsonObject, frozen),
            execution_context=execution_context,
        )

    async def activate(
        self,
        prepared: PreparedStrategyArtifact,
        *,
        loader: RuntimeEntryPointLoader,
    ) -> ActivatedStrategyArtifact:
        if not self._config.capability.ready:
            raise ArtifactRuntimeBlocked(
                self._config.capability.blocked_reason or "artifact runtime capability is not READY"
            )
        release_authority = prepared.release_authority
        try:
            materialized = await self._activator.activate(
                ArtifactActivationCandidateV1(
                    command=prepared.command,
                    activation_id=prepared.activation_id,
                    quarantine_root=prepared.quarantine_root,
                    entry_point=prepared.verified_entry_point,
                    effective_config=prepared.effective_config,
                    execution_context=prepared.execution_context,
                    artifact_identity_digest=release_authority.artifact_ref_digest,
                    artifact_authority_digest=(release_authority.crucible_artifact_evidence_digest),
                ),
                loader=loader,
            )
        except RuntimeError as error:
            raise ArtifactRuntimeActivationError(str(error)) from error
        return ActivatedStrategyArtifact(
            prepared=prepared,
            activation_root=materialized.activation_root,
            strategy=materialized.strategy,
        )


__all__ = [
    "ActivatedStrategyArtifact",
    "ArtifactRuntimeCapabilityV1",
    "ArtifactRuntimeConfigV1",
    "ArtifactRuntimeActivationError",
    "ArtifactRuntimeBlocked",
    "PreparedStrategyArtifact",
    "RuntimeArtifactSource",
    "StrategyArtifactRuntimeV1",
    "StrategyReleaseArtifactAuthorityV1",
    "VerifiedArtifactMemberV1",
    "verify_full_bom_member_files",
]

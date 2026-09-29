"""Strict canonical BOM and detached receipt models of the strategy release contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

BOM_SCHEMA_VERSION = "alephain.strategy-release-bom.v1"
RECEIPT_SCHEMA_VERSION = "alephain.strategy-release-bom-receipt.v1"
CANONICALIZATION = "sha256-canonical-json-v1"
EXECUTION_ABI = "alephain.strategy_runtime.v1"
ENGINE = "nautilus"
ENGINE_VERSION = "2.0.0rc5+sodex.2"
PYTHON_REQUIRES = ">=3.12,<3.13"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
STRATEGY_RELEASE_PREDICATE_TYPE = "https://the-alephain-guild.dev/attestation/strategy-release/v1"
STRATEGY_RELEASE_PREDICATE_SCHEMA_VERSION = "alephain.strategy-release-statement-predicate.v1"
ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION = "alephain.artifact-attestation-ref.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_ENTRY_POINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class ArtifactMemberRole(StrEnum):
    """Exact Custos ArtifactMemberRole values consumed by the release projection."""

    BASE_CONTRACTS_WHEEL = "base_contracts_wheel"
    NAUTILUS_WHEEL = "nautilus_wheel"
    STRATEGY_WHEEL = "strategy_wheel"
    STRATEGY_MANIFEST = "strategy_manifest"
    RUNTIME_ARTIFACT = "runtime_artifact"
    ATTESTATION_BUNDLE = "attestation_bundle"
    TOOLKIT_SBOM = "toolkit_sbom"
    STRATEGY_SBOM = "strategy_sbom"
    CONTRACT_SCHEMA = "contract_schema"
    SOURCE_TREE = "source_tree"


_BOM_REQUIRED_SINGLETON_ROLES = frozenset(
    {
        ArtifactMemberRole.BASE_CONTRACTS_WHEEL,
        ArtifactMemberRole.NAUTILUS_WHEEL,
        ArtifactMemberRole.STRATEGY_WHEEL,
        ArtifactMemberRole.STRATEGY_MANIFEST,
        ArtifactMemberRole.TOOLKIT_SBOM,
        ArtifactMemberRole.STRATEGY_SBOM,
        ArtifactMemberRole.CONTRACT_SCHEMA,
        ArtifactMemberRole.SOURCE_TREE,
    }
)


def _expect_exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"{label} keys differ: missing={missing}, unknown={unknown}")


def _require_non_empty(value: str, label: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be lowercase sha256 hex")


def _require_commit(value: str, label: str) -> None:
    if not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full lowercase commit digest")


def _require_coordinate(value: str, label: str) -> None:
    _require_non_empty(value, label)
    if value.startswith(("/", "./", "../", "file:")) or "\\" in value:
        raise ValueError(f"{label} must not be a local filesystem path")


@dataclass(frozen=True, slots=True)
class BomMemberV1:
    """PS member binding with a coordinate in addition to the Custos member fields."""

    role: ArtifactMemberRole
    coordinate: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _require_coordinate(self.coordinate, "member.coordinate")
        if not _MEMBER_NAME_RE.fullmatch(self.name):
            raise ValueError("member.name is not a safe relative artifact name")
        _require_non_empty(self.media_type, "member.media_type")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("member.size_bytes must be a non-negative integer")
        _require_sha256(self.sha256, "member.sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "coordinate": self.coordinate,
            "media_type": self.media_type,
            "name": self.name,
            "role": self.role.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BomMemberV1:
        _expect_exact_keys(
            value,
            frozenset({"role", "coordinate", "name", "media_type", "size_bytes", "sha256"}),
            "BomMemberV1",
        )
        role_value = value["role"]
        if not isinstance(role_value, str):
            raise ValueError("member.role is not a Custos ArtifactMemberRole")
        try:
            role = ArtifactMemberRole(role_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("member.role is not a Custos ArtifactMemberRole") from exc
        if not all(
            isinstance(value[key], str) for key in ("coordinate", "name", "media_type", "sha256")
        ):
            raise TypeError("member string fields must be strings")
        size_bytes = value["size_bytes"]
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            raise TypeError("member.size_bytes must be an integer")
        return cls(
            role=role,
            coordinate=value["coordinate"],  # type: ignore[arg-type]
            name=value["name"],  # type: ignore[arg-type]
            media_type=value["media_type"],  # type: ignore[arg-type]
            size_bytes=size_bytes,
            sha256=value["sha256"],  # type: ignore[arg-type]
        )


BOM_FIELD_NAMES = (
    "schema_version",
    "canonicalization",
    "producer_repository",
    "producer_commit",
    "strategy_coordinate",
    "engine",
    "engine_version",
    "python_requires",
    "entry_point_group",
    "entry_point_name",
    "execution_abi_schema_coordinate",
    "execution_abi_schema_sha256",
    "execution_abi_golden_coordinate",
    "execution_abi_golden_sha256",
    "contract_asset_index_coordinate",
    "contract_asset_index_sha256",
    "toolkit_coordinate",
    "toolkit_wheel_sha256",
    "toolkit_sbom_sha256",
    "strategy_sbom_sha256",
    "strategy_source_commit",
    "strategy_source_tree_sha256",
    "strategy_artifact_coordinate",
    "strategy_artifact_sha256",
    "strategy_manifest_sha256",
    "build_lock_sha256",
    "zero_rewrite_semantic_diff_sha256",
    "zero_rewrite_characterization_sha256",
    "members",
)


@dataclass(frozen=True, slots=True)
class StrategyReleaseBomV1:
    """Canonical PS build BOM; it is not a Crucible StrategyRelease aggregate."""

    schema_version: str
    canonicalization: str
    producer_repository: str
    producer_commit: str
    strategy_coordinate: str
    engine: str
    engine_version: str
    python_requires: str
    entry_point_group: str
    entry_point_name: str
    execution_abi_schema_coordinate: str
    execution_abi_schema_sha256: str
    execution_abi_golden_coordinate: str
    execution_abi_golden_sha256: str
    contract_asset_index_coordinate: str
    contract_asset_index_sha256: str
    toolkit_coordinate: str
    toolkit_wheel_sha256: str
    toolkit_sbom_sha256: str
    strategy_sbom_sha256: str
    strategy_source_commit: str
    strategy_source_tree_sha256: str
    strategy_artifact_coordinate: str
    strategy_artifact_sha256: str
    strategy_manifest_sha256: str
    build_lock_sha256: str
    zero_rewrite_semantic_diff_sha256: str
    zero_rewrite_characterization_sha256: str
    members: tuple[BomMemberV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != BOM_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {BOM_SCHEMA_VERSION}")
        if self.canonicalization != CANONICALIZATION:
            raise ValueError(f"canonicalization must be {CANONICALIZATION}")
        if self.engine != ENGINE or self.engine_version != ENGINE_VERSION:
            raise ValueError("engine contract must be nautilus 2.0.0rc5+sodex.2")
        if self.python_requires != PYTHON_REQUIRES:
            raise ValueError(f"python_requires must be {PYTHON_REQUIRES}")
        if self.entry_point_group != EXECUTION_ABI:
            raise ValueError(f"entry_point_group must be {EXECUTION_ABI}")
        if not _ENTRY_POINT_RE.fullmatch(self.entry_point_name):
            raise ValueError("entry_point_name must be a module:attribute target")
        module = self.entry_point_name.split(":", 1)[0].split(".", 1)[0]
        if module in {"shared", "pandas_ta"}:
            raise ValueError("entry_point_name cannot use a legacy top-level toolkit alias")

        for label in (
            "producer_repository",
            "strategy_coordinate",
            "execution_abi_schema_coordinate",
            "execution_abi_golden_coordinate",
            "contract_asset_index_coordinate",
            "toolkit_coordinate",
            "strategy_artifact_coordinate",
        ):
            _require_coordinate(getattr(self, label), label)
        _require_commit(self.producer_commit, "producer_commit")
        _require_commit(self.strategy_source_commit, "strategy_source_commit")
        for label in (
            "execution_abi_schema_sha256",
            "execution_abi_golden_sha256",
            "contract_asset_index_sha256",
            "toolkit_wheel_sha256",
            "toolkit_sbom_sha256",
            "strategy_sbom_sha256",
            "strategy_source_tree_sha256",
            "strategy_artifact_sha256",
            "strategy_manifest_sha256",
            "build_lock_sha256",
            "zero_rewrite_semantic_diff_sha256",
            "zero_rewrite_characterization_sha256",
        ):
            _require_sha256(getattr(self, label), label)

        member_keys = {(member.role, member.name, member.coordinate) for member in self.members}
        if len(member_keys) != len(self.members):
            raise ValueError("members must be unique by role, name, and coordinate")
        by_role: dict[ArtifactMemberRole, list[BomMemberV1]] = {}
        for member in self.members:
            by_role.setdefault(member.role, []).append(member)
        if ArtifactMemberRole.ATTESTATION_BUNDLE in by_role:
            raise ValueError(
                "attestation_bundle is detached and cannot be stored in the canonical BOM"
            )
        missing_or_repeated = sorted(
            role.value for role in _BOM_REQUIRED_SINGLETON_ROLES if len(by_role.get(role, ())) != 1
        )
        if missing_or_repeated:
            raise ValueError(
                "canonical BOM requires exactly one singleton member for roles: "
                f"{missing_or_repeated}"
            )
        expected = {
            ArtifactMemberRole.NAUTILUS_WHEEL: self.toolkit_wheel_sha256,
            ArtifactMemberRole.TOOLKIT_SBOM: self.toolkit_sbom_sha256,
            ArtifactMemberRole.STRATEGY_SBOM: self.strategy_sbom_sha256,
            ArtifactMemberRole.STRATEGY_WHEEL: self.strategy_artifact_sha256,
            ArtifactMemberRole.STRATEGY_MANIFEST: self.strategy_manifest_sha256,
            ArtifactMemberRole.CONTRACT_SCHEMA: self.contract_asset_index_sha256,
            ArtifactMemberRole.SOURCE_TREE: self.strategy_source_tree_sha256,
        }
        for role, digest in expected.items():
            if by_role[role][0].sha256 != digest:
                raise ValueError(f"{role.value} member digest differs from its BOM binding")

    def member(self, role: ArtifactMemberRole) -> BomMemberV1:
        matches = [member for member in self.members if member.role is role]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one {role.value} member")
        return matches[0]

    def to_mapping(self) -> dict[str, object]:
        values = {name: getattr(self, name) for name in BOM_FIELD_NAMES if name != "members"}
        values["members"] = [
            member.to_mapping()
            for member in sorted(
                self.members,
                key=lambda item: (item.role.value, item.name, item.coordinate),
            )
        ]
        return values

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseBomV1:
        _expect_exact_keys(value, frozenset(BOM_FIELD_NAMES), "StrategyReleaseBomV1")
        members_raw = value["members"]
        if not isinstance(members_raw, Sequence) or isinstance(members_raw, (str, bytes)):
            raise TypeError("members must be an array")
        members = tuple(
            BomMemberV1.from_mapping(item) for item in members_raw if isinstance(item, Mapping)
        )
        if len(members) != len(members_raw):
            raise TypeError("every member must be an object")
        string_values: dict[str, str] = {}
        for name in BOM_FIELD_NAMES:
            if name == "members":
                continue
            field_value = value[name]
            if not isinstance(field_value, str):
                raise TypeError(f"{name} must be a string")
            string_values[name] = field_value
        return cls(members=members, **string_values)


RECEIPT_FIELD_NAMES = (
    "schema_version",
    "producer_repository",
    "producer_commit",
    "strategy_artifact_coordinate",
    "bom_sha256",
    "strategy_artifact_sha256",
    "strategy_manifest_sha256",
    "attestation_bundle_coordinate",
    "attestation_bundle_sha256",
    "attestation_trust_policy_coordinate",
    "attestation_trust_policy_sha256",
)


@dataclass(frozen=True, slots=True)
class StrategyReleaseBomReceiptV1:
    """Detached publication receipt created only after BOM and attestation are final."""

    schema_version: str
    producer_repository: str
    producer_commit: str
    strategy_artifact_coordinate: str
    bom_sha256: str
    strategy_artifact_sha256: str
    strategy_manifest_sha256: str
    attestation_bundle_coordinate: str
    attestation_bundle_sha256: str
    attestation_trust_policy_coordinate: str
    attestation_trust_policy_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {RECEIPT_SCHEMA_VERSION}")
        for label in (
            "producer_repository",
            "strategy_artifact_coordinate",
            "attestation_bundle_coordinate",
            "attestation_trust_policy_coordinate",
        ):
            _require_coordinate(getattr(self, label), label)
        _require_commit(self.producer_commit, "producer_commit")
        for label in (
            "bom_sha256",
            "strategy_artifact_sha256",
            "strategy_manifest_sha256",
            "attestation_bundle_sha256",
            "attestation_trust_policy_sha256",
        ):
            _require_sha256(getattr(self, label), label)

    def to_mapping(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in RECEIPT_FIELD_NAMES}

    def assert_matches(self, bom: StrategyReleaseBomV1) -> None:
        expected_bom_sha256 = sha256_hex(canonical_json_bytes(bom.to_mapping()))
        expected = {
            "producer_repository": bom.producer_repository,
            "producer_commit": bom.producer_commit,
            "strategy_artifact_coordinate": bom.strategy_artifact_coordinate,
            "bom_sha256": expected_bom_sha256,
            "strategy_artifact_sha256": bom.strategy_artifact_sha256,
            "strategy_manifest_sha256": bom.strategy_manifest_sha256,
        }
        mismatched = sorted(
            name
            for name, expected_value in expected.items()
            if getattr(self, name) != expected_value
        )
        if mismatched:
            raise ValueError(f"detached receipt differs from canonical BOM: {mismatched}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseBomReceiptV1:
        _expect_exact_keys(value, frozenset(RECEIPT_FIELD_NAMES), "StrategyReleaseBomReceiptV1")
        if not all(isinstance(value[name], str) for name in RECEIPT_FIELD_NAMES):
            raise TypeError("detached receipt fields must be strings")
        return cls(**{name: value[name] for name in RECEIPT_FIELD_NAMES})  # type: ignore[arg-type]


STATEMENT_SUBJECT_NAMES = (
    "strategy-release-bom-v1",
    "strategy-artifact",
    "strategy-manifest-v1",
    "strategy-artifact-ref-v1",
)


@dataclass(frozen=True, slots=True)
class StatementSubjectV1:
    name: str
    sha256: str

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "statement subject name")
        _require_sha256(self.sha256, "statement subject sha256")

    @property
    def digest(self) -> dict[str, str]:
        return {"sha256": self.sha256}

    def to_mapping(self) -> dict[str, object]:
        return {"name": self.name, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StatementSubjectV1:
        _expect_exact_keys(value, frozenset({"name", "digest"}), "StatementSubjectV1")
        digest = value["digest"]
        if not isinstance(value["name"], str) or not isinstance(digest, Mapping):
            raise TypeError("statement subject name/digest types differ")
        _expect_exact_keys(digest, frozenset({"sha256"}), "StatementSubjectV1.digest")
        if not isinstance(digest["sha256"], str):
            raise TypeError("statement subject sha256 must be a string")
        return cls(name=value["name"], sha256=digest["sha256"])


PREDICATE_FIELD_NAMES = (
    "schema_version",
    "producer_repository",
    "producer_commit",
    "workflow_identity",
    "source_date_epoch",
    "strategy_source_tree_sha256",
    "execution_abi_schema_sha256",
    "contract_asset_index_sha256",
    "toolkit_wheel_sha256",
    "toolkit_sbom_sha256",
    "strategy_sbom_sha256",
    "build_lock_sha256",
    "zero_rewrite_semantic_diff_sha256",
    "zero_rewrite_characterization_sha256",
    "engine",
    "engine_version",
    "python_requires",
    "entry_point_group",
    "entry_point_name",
)


@dataclass(frozen=True, slots=True)
class StrategyReleasePredicateV1:
    schema_version: str
    producer_repository: str
    producer_commit: str
    workflow_identity: str
    source_date_epoch: int
    strategy_source_tree_sha256: str
    execution_abi_schema_sha256: str
    contract_asset_index_sha256: str
    toolkit_wheel_sha256: str
    toolkit_sbom_sha256: str
    strategy_sbom_sha256: str
    build_lock_sha256: str
    zero_rewrite_semantic_diff_sha256: str
    zero_rewrite_characterization_sha256: str
    engine: str
    engine_version: str
    python_requires: str
    entry_point_group: str
    entry_point_name: str

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_RELEASE_PREDICATE_SCHEMA_VERSION:
            raise ValueError("strategy release predicate schema version differs")
        _require_coordinate(self.producer_repository, "predicate producer_repository")
        _require_commit(self.producer_commit, "predicate producer_commit")
        _require_non_empty(self.workflow_identity, "predicate workflow_identity")
        if isinstance(self.source_date_epoch, bool) or self.source_date_epoch < 0:
            raise ValueError("predicate source_date_epoch must be non-negative")
        for field in (
            "strategy_source_tree_sha256",
            "execution_abi_schema_sha256",
            "contract_asset_index_sha256",
            "toolkit_wheel_sha256",
            "toolkit_sbom_sha256",
            "strategy_sbom_sha256",
            "build_lock_sha256",
            "zero_rewrite_semantic_diff_sha256",
            "zero_rewrite_characterization_sha256",
        ):
            _require_sha256(getattr(self, field), f"predicate {field}")
        if self.engine != ENGINE or self.engine_version != ENGINE_VERSION:
            raise ValueError("predicate engine contract differs")
        if self.python_requires != PYTHON_REQUIRES or self.entry_point_group != EXECUTION_ABI:
            raise ValueError("predicate runtime contract differs")
        if not _ENTRY_POINT_RE.fullmatch(self.entry_point_name):
            raise ValueError("predicate entry_point_name differs")

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in PREDICATE_FIELD_NAMES}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleasePredicateV1:
        _expect_exact_keys(value, frozenset(PREDICATE_FIELD_NAMES), "StrategyReleasePredicateV1")
        if not isinstance(value["source_date_epoch"], int) or isinstance(
            value["source_date_epoch"], bool
        ):
            raise TypeError("predicate source_date_epoch must be an integer")
        if any(
            not isinstance(value[name], str)
            for name in PREDICATE_FIELD_NAMES
            if name != "source_date_epoch"
        ):
            raise TypeError("predicate string fields must be strings")
        return cls(**{name: value[name] for name in PREDICATE_FIELD_NAMES})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class StrategyReleaseStatementV1:
    statement_type: str
    subjects: tuple[StatementSubjectV1, ...]
    predicate_type: str
    predicate: StrategyReleasePredicateV1

    def __post_init__(self) -> None:
        if self.statement_type != IN_TOTO_STATEMENT_TYPE:
            raise ValueError("in-toto statement type differs")
        if self.predicate_type != STRATEGY_RELEASE_PREDICATE_TYPE:
            raise ValueError("strategy release predicate type differs")
        if tuple(subject.name for subject in self.subjects) != STATEMENT_SUBJECT_NAMES:
            raise ValueError("strategy release statement subjects differ or are out of order")

    def to_mapping(self) -> dict[str, object]:
        return {
            "_type": self.statement_type,
            "subject": [subject.to_mapping() for subject in self.subjects],
            "predicateType": self.predicate_type,
            "predicate": self.predicate.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StrategyReleaseStatementV1:
        _expect_exact_keys(
            value,
            frozenset({"_type", "subject", "predicateType", "predicate"}),
            "StrategyReleaseStatementV1",
        )
        subjects = value["subject"]
        predicate = value["predicate"]
        if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)):
            raise TypeError("statement subject must be an array")
        if not isinstance(predicate, Mapping):
            raise TypeError("statement predicate must be an object")
        parsed_subjects = tuple(
            StatementSubjectV1.from_mapping(item) for item in subjects if isinstance(item, Mapping)
        )
        if len(parsed_subjects) != len(subjects):
            raise TypeError("statement subjects must be objects")
        if not isinstance(value["_type"], str) or not isinstance(value["predicateType"], str):
            raise TypeError("statement type fields must be strings")
        return cls(
            statement_type=value["_type"],
            subjects=parsed_subjects,
            predicate_type=value["predicateType"],
            predicate=StrategyReleasePredicateV1.from_mapping(predicate),
        )


ATTESTATION_REF_FIELD_NAMES = (
    "schema_version",
    "statement_coordinate",
    "statement_sha256",
    "bundle_coordinate",
    "bundle_sha256",
    "payload_type",
    "predicate_type",
)


@dataclass(frozen=True, slots=True)
class ArtifactAttestationRefV1:
    schema_version: str
    statement_coordinate: str
    statement_sha256: str
    bundle_coordinate: str
    bundle_sha256: str
    payload_type: str
    predicate_type: str

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION:
            raise ValueError("ArtifactAttestationRefV1 schema_version differs")
        if self.payload_type != DSSE_PAYLOAD_TYPE:
            raise ValueError("ArtifactAttestationRefV1 payload_type differs")
        if self.predicate_type != STRATEGY_RELEASE_PREDICATE_TYPE:
            raise ValueError("ArtifactAttestationRefV1 predicate_type differs")
        for coordinate, digest, label in (
            (self.statement_coordinate, self.statement_sha256, "statement"),
            (self.bundle_coordinate, self.bundle_sha256, "bundle"),
        ):
            _require_coordinate(coordinate, f"{label}_coordinate")
            _require_sha256(digest, f"{label}_sha256")
            if not coordinate.endswith(f"@sha256:{digest}"):
                raise ValueError(f"{label}_coordinate must be digest-pinned")

    def to_mapping(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in ATTESTATION_REF_FIELD_NAMES}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArtifactAttestationRefV1:
        _expect_exact_keys(
            value, frozenset(ATTESTATION_REF_FIELD_NAMES), "ArtifactAttestationRefV1"
        )
        if any(not isinstance(value[name], str) for name in ATTESTATION_REF_FIELD_NAMES):
            raise TypeError("ArtifactAttestationRefV1 fields must be strings")
        return cls(**{name: value[name] for name in ATTESTATION_REF_FIELD_NAMES})  # type: ignore[arg-type]


def canonical_json_bytes(value: object) -> bytes:
    """Encode exact Custos sha256-canonical-json-v1 bytes."""

    return _encode_canonical_json(value).encode("utf-8")


def _encode_canonical_json(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON numbers must be finite")
        if value == 0:
            return "0"
        text = format(value.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, float):
        raise TypeError("float is forbidden in canonical JSON")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        entries = (
            f"{json.dumps(key, ensure_ascii=False)}:{_encode_canonical_json(value[key])}"
            for key in sorted(value)
        )
        return "{" + ",".join(entries) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_canonical_json(item) for item in value) + "]"
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _string_schema(*, const: str | None = None, pattern: str | None = None) -> dict[str, object]:
    schema: dict[str, object] = {"type": "string", "minLength": 1}
    if const is not None:
        schema["const"] = const
    if pattern is not None:
        schema["pattern"] = pattern
        schema.pop("minLength", None)
    return schema


def _member_schema() -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": {
            "coordinate": _string_schema(),
            "media_type": _string_schema(),
            "name": _string_schema(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$"),
            "role": {"enum": [role.value for role in ArtifactMemberRole], "type": "string"},
            "sha256": _string_schema(pattern=r"^[0-9a-f]{64}$"),
            "size_bytes": {"minimum": 0, "type": "integer"},
        },
        "required": ["role", "coordinate", "name", "media_type", "size_bytes", "sha256"],
        "type": "object",
    }


def bom_json_schema() -> dict[str, object]:
    sha = _string_schema(pattern=r"^[0-9a-f]{64}$")
    commit = _string_schema(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    properties: dict[str, object] = {
        "schema_version": _string_schema(const=BOM_SCHEMA_VERSION),
        "canonicalization": _string_schema(const=CANONICALIZATION),
        "producer_repository": _string_schema(),
        "producer_commit": commit,
        "strategy_coordinate": _string_schema(),
        "engine": _string_schema(const=ENGINE),
        "engine_version": _string_schema(const=ENGINE_VERSION),
        "python_requires": _string_schema(const=PYTHON_REQUIRES),
        "entry_point_group": _string_schema(const=EXECUTION_ABI),
        "entry_point_name": _string_schema(pattern=_ENTRY_POINT_RE.pattern),
        "execution_abi_schema_coordinate": _string_schema(),
        "execution_abi_schema_sha256": sha,
        "execution_abi_golden_coordinate": _string_schema(),
        "execution_abi_golden_sha256": sha,
        "contract_asset_index_coordinate": _string_schema(),
        "contract_asset_index_sha256": sha,
        "toolkit_coordinate": _string_schema(),
        "toolkit_wheel_sha256": sha,
        "toolkit_sbom_sha256": sha,
        "strategy_sbom_sha256": sha,
        "strategy_source_commit": commit,
        "strategy_source_tree_sha256": sha,
        "strategy_artifact_coordinate": _string_schema(),
        "strategy_artifact_sha256": sha,
        "strategy_manifest_sha256": sha,
        "build_lock_sha256": sha,
        "zero_rewrite_semantic_diff_sha256": sha,
        "zero_rewrite_characterization_sha256": sha,
        "members": {"items": {"$ref": "#/$defs/BomMemberV1"}, "minItems": 9, "type": "array"},
    }
    return {
        "$defs": {"BomMemberV1": _member_schema()},
        "$id": "https://philosophers-stone.the-alephain-guild/contracts/strategy-release-bom-v1.schema.json",
        "additionalProperties": False,
        "description": "Canonical PS build/provenance BOM, not a StrategyRelease lifecycle record.",
        "properties": properties,
        "required": list(BOM_FIELD_NAMES),
        "title": "StrategyReleaseBomV1",
        "type": "object",
    }


def receipt_json_schema() -> dict[str, object]:
    sha = _string_schema(pattern=r"^[0-9a-f]{64}$")
    return {
        "$id": "https://philosophers-stone.the-alephain-guild/contracts/strategy-release-bom-receipt-v1.schema.json",
        "additionalProperties": False,
        "description": "Detached attestation/publication binding emitted after canonical BOM finalization.",
        "properties": {
            "schema_version": _string_schema(const=RECEIPT_SCHEMA_VERSION),
            "producer_repository": _string_schema(),
            "producer_commit": _string_schema(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
            "strategy_artifact_coordinate": _string_schema(),
            "bom_sha256": sha,
            "strategy_artifact_sha256": sha,
            "strategy_manifest_sha256": sha,
            "attestation_bundle_coordinate": _string_schema(),
            "attestation_bundle_sha256": sha,
            "attestation_trust_policy_coordinate": _string_schema(),
            "attestation_trust_policy_sha256": sha,
        },
        "required": list(RECEIPT_FIELD_NAMES),
        "title": "StrategyReleaseBomReceiptV1",
        "type": "object",
    }


def strategy_release_statement_json_schema() -> dict[str, object]:
    sha = _string_schema(pattern=r"^[0-9a-f]{64}$")
    predicate_properties: dict[str, object] = {
        "schema_version": _string_schema(const=STRATEGY_RELEASE_PREDICATE_SCHEMA_VERSION),
        "producer_repository": _string_schema(),
        "producer_commit": _string_schema(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
        "workflow_identity": _string_schema(),
        "source_date_epoch": {"minimum": 0, "type": "integer"},
        "strategy_source_tree_sha256": sha,
        "execution_abi_schema_sha256": sha,
        "contract_asset_index_sha256": sha,
        "toolkit_wheel_sha256": sha,
        "toolkit_sbom_sha256": sha,
        "strategy_sbom_sha256": sha,
        "build_lock_sha256": sha,
        "zero_rewrite_semantic_diff_sha256": sha,
        "zero_rewrite_characterization_sha256": sha,
        "engine": _string_schema(const=ENGINE),
        "engine_version": _string_schema(const=ENGINE_VERSION),
        "python_requires": _string_schema(const=PYTHON_REQUIRES),
        "entry_point_group": _string_schema(const=EXECUTION_ABI),
        "entry_point_name": _string_schema(pattern=_ENTRY_POINT_RE.pattern),
    }
    return {
        "$defs": {
            "StatementSubjectV1": {
                "additionalProperties": False,
                "properties": {
                    "name": {"enum": list(STATEMENT_SUBJECT_NAMES), "type": "string"},
                    "digest": {
                        "additionalProperties": False,
                        "properties": {"sha256": sha},
                        "required": ["sha256"],
                        "type": "object",
                    },
                },
                "required": ["name", "digest"],
                "type": "object",
            },
            "StrategyReleasePredicateV1": {
                "additionalProperties": False,
                "properties": predicate_properties,
                "required": list(PREDICATE_FIELD_NAMES),
                "type": "object",
            },
        },
        "$id": (
            "https://philosophers-stone.the-alephain-guild/contracts/"
            "strategy-release-statement-v1.schema.json"
        ),
        "x-dsse-payloadType": DSSE_PAYLOAD_TYPE,
        "additionalProperties": False,
        "properties": {
            "_type": _string_schema(const=IN_TOTO_STATEMENT_TYPE),
            "subject": {
                "items": {"$ref": "#/$defs/StatementSubjectV1"},
                "maxItems": len(STATEMENT_SUBJECT_NAMES),
                "minItems": len(STATEMENT_SUBJECT_NAMES),
                "type": "array",
            },
            "predicateType": _string_schema(const=STRATEGY_RELEASE_PREDICATE_TYPE),
            "predicate": {"$ref": "#/$defs/StrategyReleasePredicateV1"},
        },
        "required": ["_type", "subject", "predicateType", "predicate"],
        "title": "StrategyReleaseStatementV1",
        "type": "object",
    }


def artifact_attestation_ref_json_schema() -> dict[str, object]:
    sha = _string_schema(pattern=r"^[0-9a-f]{64}$")
    return {
        "$id": (
            "https://philosophers-stone.the-alephain-guild/contracts/"
            "artifact-attestation-ref-v1.schema.json"
        ),
        "additionalProperties": False,
        "description": (
            "Detached reference created after statement and bundle bytes are immutable; "
            "not a verification or trust-policy receipt."
        ),
        "properties": {
            "schema_version": _string_schema(const=ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION),
            "statement_coordinate": _string_schema(),
            "statement_sha256": sha,
            "bundle_coordinate": _string_schema(),
            "bundle_sha256": sha,
            "payload_type": _string_schema(const=DSSE_PAYLOAD_TYPE),
            "predicate_type": _string_schema(const=STRATEGY_RELEASE_PREDICATE_TYPE),
        },
        "required": list(ATTESTATION_REF_FIELD_NAMES),
        "title": "ArtifactAttestationRefV1",
        "type": "object",
    }


def parse_canonical_bom_bytes(payload: bytes) -> StrategyReleaseBomV1:
    """Parse BOM bytes only when they already equal the canonical representation."""

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("BOM is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("BOM JSON must be an object")
    bom = StrategyReleaseBomV1.from_mapping(value)
    if payload != canonical_json_bytes(bom.to_mapping()):
        raise ValueError("BOM bytes are not canonical JSON")
    return bom


def parse_canonical_receipt_bytes(payload: bytes) -> StrategyReleaseBomReceiptV1:
    """Parse detached receipt bytes without accepting normalization or field loss."""

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("receipt is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("receipt JSON must be an object")
    receipt = StrategyReleaseBomReceiptV1.from_mapping(value)
    if payload != canonical_json_bytes(receipt.to_mapping()):
        raise ValueError("receipt bytes are not canonical JSON")
    return receipt

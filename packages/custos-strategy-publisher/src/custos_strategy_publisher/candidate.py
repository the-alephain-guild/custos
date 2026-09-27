"""Compare two independent builds and freeze them into one unsigned candidate.

The candidate is credential-free: it is built without signing identity or registry
write access, and the publish step signs exactly these bytes without rebuilding.
Who produced it is recorded, not assumed: the producer repository, its GHCR
repository and the workflow that built it are checked for shape here and for
agreement with the publishing run where the candidate is loaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

from .build_input import (
    BUILD_INPUT_FILENAME,
    BUILD_INPUT_SCHEMA_VERSION,
    PRODUCER_GATE_SCHEMA_VERSION,
)
from .model import StrategyReleaseStatementV1, canonical_json_bytes
from .oci_primitives import (
    DISCOVERY_TAG_PATTERN,
    PRODUCER_REPOSITORY_PATTERN,
    RELEASE_LAYER_MEDIA_TYPES,
    WORKFLOW_REF_PATTERN,
    OciBlobV1,
    require_ghcr_repository,
)

UNSIGNED_CANDIDATE_SCHEMA_VERSION = "alephain.strategy-artifact-unsigned-candidate.v1"
UNSIGNED_CANDIDATE_FILENAME = "unsigned-candidate-v1.json"
UNSIGNED_CANDIDATE_SIDECAR = f"{UNSIGNED_CANDIDATE_FILENAME}.sha256"
# The publisher's own contract goldens: checked-in example bytes, never a release.
_PUBLISHER_CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TAG_RE = re.compile(DISCOVERY_TAG_PATTERN)
_PRODUCER_REPOSITORY_RE = re.compile(PRODUCER_REPOSITORY_PATTERN)
_WORKFLOW_REF_RE = re.compile(WORKFLOW_REF_PATTERN)
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUILD_TREE_DOMAIN = b"alephain.strategy-artifact-unsigned-build-tree.v1\0"


class UnsignedCandidateError(ValueError):
    """The candidate is mutable, synthetic, unverified, or provenance-drifted."""


@dataclass(frozen=True, slots=True)
class CandidateWorkflowV1:
    workflow_ref: str
    run_id: int
    run_attempt: int
    artifact_name: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_name": self.artifact_name,
            "run_attempt": self.run_attempt,
            "run_id": self.run_id,
            "workflow_ref": self.workflow_ref,
        }


@dataclass(frozen=True, slots=True)
class CandidateFileV1:
    path: str
    sha256: str
    size_bytes: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class UnsignedCandidateV1:
    envelope_digest: str
    repository: str
    discovery_tag: str
    producer_repository: str
    producer_commit: str
    strategy_coordinate: str
    candidate_workflow: CandidateWorkflowV1
    build_tree_sha256: str
    release_layers: tuple[OciBlobV1, ...]
    release_layer_files: tuple[CandidateFileV1, ...]
    producer_gate_receipt: CandidateFileV1


@dataclass(frozen=True, slots=True)
class VerifiedBuildTreeV1:
    """One closed deterministic build tree safe for downstream materialization."""

    root: Path
    producer_commit: str
    strategy_coordinate: str
    discovery_tag: str
    build_tree_sha256: str
    files: Mapping[str, bytes]
    release_layer_files: tuple[CandidateFileV1, ...]
    producer_gate_receipt: CandidateFileV1


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _closed_json(content: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise UnsignedCandidateError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            content,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                UnsignedCandidateError(f"{label} contains non-finite number: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnsignedCandidateError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise UnsignedCandidateError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise UnsignedCandidateError(f"{label} fields differ")


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UnsignedCandidateError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise UnsignedCandidateError(f"{label} must be an array")
    return value


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UnsignedCandidateError(f"{label} must be a string")
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsignedCandidateError(f"{label} must be an integer")
    return value


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or value.endswith("/")
    ):
        raise UnsignedCandidateError("candidate file path is unsafe")
    return path.as_posix()


def _stable_snapshot(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise UnsignedCandidateError("candidate build root must be one regular directory")
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise UnsignedCandidateError(f"candidate build contains symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise UnsignedCandidateError(f"candidate build contains non-file: {relative}")
        before = path.stat(follow_symlinks=False)
        content = path.read_bytes()
        after = path.stat(follow_symlinks=False)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise UnsignedCandidateError(f"candidate build file changed while read: {relative}")
        snapshot[relative] = content
    if not snapshot:
        raise UnsignedCandidateError("candidate build must not be empty")
    return snapshot


def _file_from_spec(
    value: Mapping[str, object],
    snapshot: Mapping[str, bytes],
    label: str,
) -> tuple[CandidateFileV1, bytes]:
    _exact_keys(value, {"path", "sha256", "size_bytes"}, label)
    path = _safe_relative_path(_as_str(value["path"], f"{label}.path"))
    digest = _as_str(value["sha256"], f"{label}.sha256")
    size = _as_int(value["size_bytes"], f"{label}.size_bytes")
    if _SHA256_RE.fullmatch(digest) is None or size <= 0:
        raise UnsignedCandidateError(f"{label} digest or size differs")
    content = snapshot.get(path)
    if content is None:
        raise UnsignedCandidateError(f"{label} file is absent")
    if len(content) != size or _sha256(content) != digest:
        raise UnsignedCandidateError(f"{label} bytes differ")
    return CandidateFileV1(path=path, sha256=digest, size_bytes=size), content


def _contains_fixture_marker(value: object) -> bool:
    if isinstance(value, str):
        return "fixture://" in value
    if isinstance(value, Mapping):
        if value.get("contract_only") is True:
            return True
        return any(_contains_fixture_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_fixture_marker(item) for item in value)
    return False


def _forbidden_fixture_digests(repo_root: Path) -> set[str]:
    """Digests of checked-in fixtures and goldens, in the producer and the publisher."""

    digests: set[str] = set()
    for authority in (repo_root.resolve() / "docs" / "authority", _PUBLISHER_CONTRACTS):
        if not authority.is_dir():
            continue
        for path in authority.rglob("*"):
            relative = path.relative_to(authority)
            if not path.is_file() or path.is_symlink():
                continue
            if "fixtures" in relative.parts or ".golden." in path.name:
                digests.add(_sha256(path.read_bytes()))
    return digests


def _check_workflow_ref(workflow_ref: str) -> None:
    if _WORKFLOW_REF_RE.fullmatch(workflow_ref) is None:
        raise UnsignedCandidateError("candidate workflow ref is not a GitHub workflow ref")


def _tree_digest(snapshot: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(_BUILD_TREE_DOMAIN)
    for path, content in sorted(snapshot.items()):
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_gate_receipt(
    content: bytes,
    producer_commit: str,
) -> dict[str, object]:
    value = _closed_json(content, "producer gate receipt")
    expected = {
        "artifact_build_verified",
        "external_publication_completed",
        "manifest_verified",
        "producer_commit",
        "schema_version",
        "statement_pre_sign_verified",
        "verify_before_publish",
        "zero_rewrite_characterization_sha256",
        "zero_rewrite_semantic_diff_sha256",
        "zero_rewrite_verified",
    }
    _exact_keys(value, expected, "producer gate receipt")
    if canonical_json_bytes(value) != content:
        raise UnsignedCandidateError("producer gate receipt is not canonical JSON")
    if (
        value["schema_version"] != PRODUCER_GATE_SCHEMA_VERSION
        or value["producer_commit"] != producer_commit
        or value["artifact_build_verified"] is not True
        or value["manifest_verified"] is not True
        or value["statement_pre_sign_verified"] is not True
        or value["verify_before_publish"] is not True
        or value["zero_rewrite_verified"] is not True
        or value["external_publication_completed"] is not False
    ):
        raise UnsignedCandidateError("producer gate receipt is not release-eligible")
    for key in (
        "zero_rewrite_characterization_sha256",
        "zero_rewrite_semantic_diff_sha256",
    ):
        digest = value[key]
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise UnsignedCandidateError("producer gate receipt zero-rewrite digest differs")
    return value


def _validate_build_snapshot(
    snapshot: Mapping[str, bytes],
    *,
    producer_commit: str,
    repo_root: Path,
) -> tuple[dict[str, object], tuple[CandidateFileV1, ...], CandidateFileV1]:
    manifest_bytes = snapshot.get(BUILD_INPUT_FILENAME)
    if manifest_bytes is None:
        raise UnsignedCandidateError(f"{BUILD_INPUT_FILENAME} is absent")
    manifest = _closed_json(manifest_bytes, "unsigned build input")
    expected = {
        "discovery_tag",
        "producer_commit",
        "producer_gate_receipt",
        "producer_repository",
        "release_layers",
        "repository",
        "schema_version",
        "strategy_coordinate",
    }
    _exact_keys(manifest, expected, "unsigned build input")
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise UnsignedCandidateError("unsigned build input is not canonical JSON")
    if (
        manifest["schema_version"] != BUILD_INPUT_SCHEMA_VERSION
        or manifest["producer_commit"] != producer_commit
    ):
        raise UnsignedCandidateError("unsigned build authority differs")
    try:
        require_ghcr_repository(_as_str(manifest["repository"], "repository"))
    except ValueError as error:
        raise UnsignedCandidateError(str(error)) from error
    producer_repository = _as_str(manifest["producer_repository"], "producer_repository")
    if _PRODUCER_REPOSITORY_RE.fullmatch(producer_repository) is None:
        raise UnsignedCandidateError("producer repository is not <owner>/<name>")
    if _COMMIT_RE.fullmatch(producer_commit) is None:
        raise UnsignedCandidateError("producer commit must be full lowercase hex")
    discovery_tag = _as_str(manifest["discovery_tag"], "discovery_tag")
    strategy_coordinate = _as_str(manifest["strategy_coordinate"], "strategy_coordinate")
    if _TAG_RE.fullmatch(discovery_tag) is None or not strategy_coordinate:
        raise UnsignedCandidateError("candidate tag or strategy coordinate differs")
    if _contains_fixture_marker(manifest):
        raise UnsignedCandidateError("contract fixture cannot become a real candidate")

    raw_layers = _as_list(manifest["release_layers"], "release_layers")
    if len(raw_layers) != len(RELEASE_LAYER_MEDIA_TYPES):
        raise UnsignedCandidateError("release layer count differs")
    layer_files: list[CandidateFileV1] = []
    release_layers: list[OciBlobV1] = []
    for role, raw in zip(RELEASE_LAYER_MEDIA_TYPES, raw_layers, strict=True):
        value = _as_mapping(raw, "release layer")
        _exact_keys(
            value,
            {"media_type", "name", "path", "role", "sha256", "size_bytes"},
            role,
        )
        if value["role"] != role or value["media_type"] != RELEASE_LAYER_MEDIA_TYPES[role]:
            raise UnsignedCandidateError(f"release layer contract differs for {role}")
        file_spec, content = _file_from_spec(
            {
                "path": value["path"],
                "sha256": value["sha256"],
                "size_bytes": value["size_bytes"],
            },
            snapshot,
            role,
        )
        name = _as_str(value["name"], f"{role}.name")
        layer_files.append(file_spec)
        release_layers.append(
            OciBlobV1(
                role=role,
                name=name,
                media_type=RELEASE_LAYER_MEDIA_TYPES[role],
                content=content,
            )
        )

    gate_spec, gate_content = _file_from_spec(
        _as_mapping(manifest["producer_gate_receipt"], "producer_gate_receipt"),
        snapshot,
        "producer_gate_receipt",
    )
    _validate_gate_receipt(gate_content, producer_commit)
    allowed = {
        BUILD_INPUT_FILENAME,
        gate_spec.path,
        *(item.path for item in layer_files),
    }
    if set(snapshot) != allowed:
        raise UnsignedCandidateError("candidate build contains unreferenced or missing files")

    statement = next(
        layer for layer in release_layers if layer.role == "strategy_release_statement"
    )
    statement_value = _closed_json(statement.content, "strategy release statement")
    try:
        StrategyReleaseStatementV1.from_mapping(statement_value)
    except (TypeError, ValueError) as error:
        raise UnsignedCandidateError("strategy release statement contract differs") from error
    if canonical_json_bytes(statement_value) != statement.content:
        raise UnsignedCandidateError("strategy release statement is not canonical JSON")

    forbidden = _forbidden_fixture_digests(repo_root)
    for item in (*layer_files, gate_spec):
        if item.sha256 in forbidden:
            raise UnsignedCandidateError("checked-in contract fixture cannot become a candidate")
    for layer in release_layers:
        if layer.media_type.endswith("+json"):
            value = _closed_json(layer.content, layer.role)
            if _contains_fixture_marker(value):
                raise UnsignedCandidateError("fixture-marked JSON cannot become a candidate")
    return manifest, tuple(layer_files), gate_spec


def verify_build_tree(
    build_root: Path,
    *,
    repo_root: Path,
) -> VerifiedBuildTreeV1:
    """Validate a producer build once and expose an immutable byte snapshot."""

    snapshot = _stable_snapshot(build_root)
    manifest_bytes = snapshot.get(BUILD_INPUT_FILENAME)
    if manifest_bytes is None:
        raise UnsignedCandidateError(f"{BUILD_INPUT_FILENAME} is absent")
    manifest = _closed_json(manifest_bytes, "unsigned build input")
    producer_commit = _as_str(manifest.get("producer_commit"), "producer_commit")
    validated, layer_files, gate_spec = _validate_build_snapshot(
        snapshot,
        producer_commit=producer_commit,
        repo_root=repo_root,
    )
    return VerifiedBuildTreeV1(
        root=build_root.resolve(),
        producer_commit=producer_commit,
        strategy_coordinate=_as_str(validated["strategy_coordinate"], "strategy_coordinate"),
        discovery_tag=_as_str(validated["discovery_tag"], "discovery_tag"),
        build_tree_sha256=_tree_digest(snapshot),
        files=MappingProxyType(dict(snapshot)),
        release_layer_files=layer_files,
        producer_gate_receipt=gate_spec,
    )


def _atomic_materialize(output: Path, files: Mapping[str, bytes]) -> None:
    output = output.resolve()
    if output.exists():
        raise UnsignedCandidateError("candidate output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    temporary.mkdir(exist_ok=False)
    try:
        for relative, content in sorted(files.items()):
            target = temporary / _safe_relative_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def finalize_unsigned_candidate(
    first_build: Path,
    second_build: Path,
    output: Path,
    *,
    repo_root: Path,
    producer_commit: str,
    producer_repository: str,
    repository: str,
    workflow_ref: str,
    run_id: int,
    run_attempt: int,
    artifact_name: str,
) -> str:
    """Compare two isolated builds and atomically freeze one unsigned envelope.

    ``producer_repository`` and ``repository`` are what the building run is: the
    repository it checked out and the GHCR repository it publishes to. The build
    must have been made for exactly those.
    """

    if first_build.resolve() == second_build.resolve():
        raise UnsignedCandidateError("two distinct build roots are required")
    if output.resolve() in {first_build.resolve(), second_build.resolve()}:
        raise UnsignedCandidateError("candidate output must be distinct from both builds")
    first = _stable_snapshot(first_build)
    second = _stable_snapshot(second_build)
    if first != second:
        raise UnsignedCandidateError("independent candidate builds differ byte-for-byte")
    manifest, layer_files, gate_spec = _validate_build_snapshot(
        first,
        producer_commit=producer_commit,
        repo_root=repo_root,
    )
    if (
        manifest["producer_repository"] != producer_repository
        or manifest["repository"] != repository
    ):
        raise UnsignedCandidateError("the build was made for another producer or repository")
    _check_workflow_ref(workflow_ref)
    if run_id <= 0 or run_attempt <= 0:
        raise UnsignedCandidateError("candidate workflow run identity must be positive")
    if _ARTIFACT_NAME_RE.fullmatch(artifact_name) is None:
        raise UnsignedCandidateError("candidate artifact name is invalid")
    workflow = CandidateWorkflowV1(
        workflow_ref=workflow_ref,
        run_id=run_id,
        run_attempt=run_attempt,
        artifact_name=artifact_name,
    )
    build_tree_sha256 = _tree_digest(first)
    envelope = {
        "build_manifest": CandidateFileV1(
            path=BUILD_INPUT_FILENAME,
            sha256=_sha256(first[BUILD_INPUT_FILENAME]),
            size_bytes=len(first[BUILD_INPUT_FILENAME]),
        ).to_mapping(),
        "build_tree_sha256": build_tree_sha256,
        "builds_byte_identical": True,
        "candidate_workflow": workflow.to_mapping(),
        "discovery_tag": manifest["discovery_tag"],
        "producer_commit": producer_commit,
        "producer_gate_receipt": gate_spec.to_mapping(),
        "producer_repository": producer_repository,
        "release_layers": manifest["release_layers"],
        "repository": repository,
        "schema_version": UNSIGNED_CANDIDATE_SCHEMA_VERSION,
        "strategy_coordinate": manifest["strategy_coordinate"],
    }
    envelope_bytes = canonical_json_bytes(envelope)
    envelope_digest = _sha256(envelope_bytes)
    sidecar = f"{envelope_digest}  {UNSIGNED_CANDIDATE_FILENAME}\n".encode("ascii")
    output_files = dict(first)
    output_files[UNSIGNED_CANDIDATE_FILENAME] = envelope_bytes
    output_files[UNSIGNED_CANDIDATE_SIDECAR] = sidecar
    _atomic_materialize(output, output_files)
    return envelope_digest


def load_unsigned_candidate(
    envelope_path: Path,
    *,
    repo_root: Path,
    expected_producer_commit: str,
    expected_producer_repository: str,
    expected_repository: str,
    expected_workflow_ref: str,
    expected_run_id: int,
    expected_artifact_name: str,
) -> UnsignedCandidateV1:
    """Load a frozen candidate that the publishing run itself built.

    The candidate and publish jobs run in one workflow run, so the candidate must
    name that run, its workflow, its repository and its commit.
    """

    root = envelope_path.resolve().parent
    snapshot = _stable_snapshot(root)
    envelope_bytes = snapshot.get(UNSIGNED_CANDIDATE_FILENAME)
    sidecar = snapshot.get(UNSIGNED_CANDIDATE_SIDECAR)
    if envelope_path.resolve() != root / UNSIGNED_CANDIDATE_FILENAME:
        raise UnsignedCandidateError("unsigned candidate filename differs")
    if envelope_bytes is None or sidecar is None:
        raise UnsignedCandidateError("unsigned candidate envelope or sidecar is absent")
    envelope_digest = _sha256(envelope_bytes)
    if sidecar != f"{envelope_digest}  {UNSIGNED_CANDIDATE_FILENAME}\n".encode("ascii"):
        raise UnsignedCandidateError("unsigned candidate sidecar differs")
    envelope = _closed_json(envelope_bytes, "unsigned candidate envelope")
    expected = {
        "build_manifest",
        "build_tree_sha256",
        "builds_byte_identical",
        "candidate_workflow",
        "discovery_tag",
        "producer_commit",
        "producer_gate_receipt",
        "producer_repository",
        "release_layers",
        "repository",
        "schema_version",
        "strategy_coordinate",
    }
    _exact_keys(envelope, expected, "unsigned candidate envelope")
    if canonical_json_bytes(envelope) != envelope_bytes:
        raise UnsignedCandidateError("unsigned candidate envelope is not canonical JSON")
    if (
        envelope["schema_version"] != UNSIGNED_CANDIDATE_SCHEMA_VERSION
        or envelope["builds_byte_identical"] is not True
        or envelope["producer_commit"] != expected_producer_commit
        or envelope["producer_repository"] != expected_producer_repository
        or envelope["repository"] != expected_repository
    ):
        raise UnsignedCandidateError("unsigned candidate envelope authority differs")
    workflow_value = _as_mapping(envelope["candidate_workflow"], "candidate_workflow")
    _exact_keys(
        workflow_value,
        {"artifact_name", "run_attempt", "run_id", "workflow_ref"},
        "candidate_workflow",
    )
    workflow = CandidateWorkflowV1(
        workflow_ref=_as_str(workflow_value["workflow_ref"], "workflow_ref"),
        run_id=_as_int(workflow_value["run_id"], "run_id"),
        run_attempt=_as_int(workflow_value["run_attempt"], "run_attempt"),
        artifact_name=_as_str(workflow_value["artifact_name"], "artifact_name"),
    )
    if (
        workflow.workflow_ref != expected_workflow_ref
        or workflow.run_id != expected_run_id
        or workflow.run_attempt <= 0
        or workflow.artifact_name != expected_artifact_name
    ):
        raise UnsignedCandidateError("candidate workflow provenance differs")

    build_snapshot = {
        path: content
        for path, content in snapshot.items()
        if path not in {UNSIGNED_CANDIDATE_FILENAME, UNSIGNED_CANDIDATE_SIDECAR}
    }
    manifest, layer_files, gate_spec = _validate_build_snapshot(
        build_snapshot,
        producer_commit=expected_producer_commit,
        repo_root=repo_root,
    )
    if _tree_digest(build_snapshot) != envelope["build_tree_sha256"]:
        raise UnsignedCandidateError("unsigned candidate build tree digest differs")
    build_spec, _ = _file_from_spec(
        _as_mapping(envelope["build_manifest"], "build_manifest"),
        build_snapshot,
        "build_manifest",
    )
    if build_spec.path != BUILD_INPUT_FILENAME:
        raise UnsignedCandidateError("unsigned candidate build manifest path differs")
    if envelope["release_layers"] != manifest["release_layers"]:
        raise UnsignedCandidateError("unsigned candidate release layer descriptors differ")
    if envelope["producer_gate_receipt"] != manifest["producer_gate_receipt"]:
        raise UnsignedCandidateError("unsigned candidate gate receipt descriptor differs")
    layers = tuple(
        OciBlobV1(
            role=role,
            name=_as_str(_as_mapping(raw, role)["name"], f"{role}.name"),
            media_type=RELEASE_LAYER_MEDIA_TYPES[role],
            content=build_snapshot[file_spec.path],
        )
        for role, raw, file_spec in zip(
            RELEASE_LAYER_MEDIA_TYPES,
            _as_list(manifest["release_layers"], "release_layers"),
            layer_files,
            strict=True,
        )
    )
    return UnsignedCandidateV1(
        envelope_digest=envelope_digest,
        repository=_as_str(envelope["repository"], "repository"),
        discovery_tag=_as_str(envelope["discovery_tag"], "discovery_tag"),
        producer_repository=_as_str(envelope["producer_repository"], "producer_repository"),
        producer_commit=expected_producer_commit,
        strategy_coordinate=_as_str(envelope["strategy_coordinate"], "strategy_coordinate"),
        candidate_workflow=workflow,
        build_tree_sha256=_as_str(envelope["build_tree_sha256"], "build_tree_sha256"),
        release_layers=layers,
        release_layer_files=layer_files,
        producer_gate_receipt=gate_spec,
    )

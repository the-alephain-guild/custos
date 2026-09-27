"""Load an assembled release and publish it, as the job that is running.

Who publishes is not configured: it is this GitHub Actions job, as its OIDC
token describes it. The token names the producer repository, the ref and the
publisher's workflow; the environment must agree with it, and the release being
published must have been assembled for that producer and GHCR repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from .github_oidc import read_workflow_claims, request_oidc_token
from .model import ArtifactAttestationRefV1, canonical_json_bytes
from .oci_primitives import (
    RELEASE_LAYER_MEDIA_TYPES,
    SIGSTORE_BUNDLE_MEDIA_TYPE,
    OciBlobV1,
    PublicationWorkflowIdentityV1,
)
from .oci_publication import PUBLISH_INPUT_V1_SCHEMA_VERSION, OciCandidateV1

PUBLISH_INPUT_SCHEMA_VERSION = PUBLISH_INPUT_V1_SCHEMA_VERSION
_COMMIT_LENGTHS = {40, 64}


@dataclass(frozen=True, slots=True)
class PublicationContextV1:
    producer_repository: str
    producer_commit: str
    workflow: PublicationWorkflowIdentityV1

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> PublicationContextV1:
        if environment.get("GITHUB_ACTIONS") != "true":
            raise ValueError("publishing runs in GitHub Actions only")
        commit = environment.get("GITHUB_SHA", "")
        if len(commit) not in _COMMIT_LENGTHS or any(
            character not in "0123456789abcdef" for character in commit
        ):
            raise ValueError("publisher commit differs")
        claims = read_workflow_claims(request_oidc_token(environment))
        for name, claimed in (
            ("GITHUB_REPOSITORY", claims.repository),
            ("GITHUB_REF", claims.ref),
            ("GITHUB_RUN_ID", str(claims.run_id)),
            ("GITHUB_RUN_ATTEMPT", str(claims.run_attempt)),
        ):
            if environment.get(name) != claimed:
                raise ValueError(f"{name} differs from this job's OIDC token")
        return cls(
            producer_repository=claims.repository,
            producer_commit=commit,
            workflow=claims.publication_identity(),
        )


@dataclass(frozen=True, slots=True)
class RegistryPublicationCredentialsV1:
    actor: str
    token: str

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> RegistryPublicationCredentialsV1:
        actor = environment.get("GHCR_ACTOR", "")
        token = environment.get("GHCR_TOKEN", "")
        if not actor or not token:
            raise ValueError("explicit GHCR publication credentials are absent")
        if (
            actor != actor.strip()
            or re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?",
                actor,
            )
            is None
        ):
            raise ValueError("GHCR publication actor is invalid")
        for ambient_name in ("GITHUB_TOKEN", "GH_TOKEN"):
            ambient_token = environment.get(ambient_name, "")
            if ambient_token and hmac.compare_digest(token, ambient_token):
                raise ValueError("ambient GitHub token fallback is forbidden")
        return cls(actor=actor, token=token)


def load_candidate_v1(
    manifest_path: Path,
    context: PublicationContextV1,
    *,
    repository: str,
) -> OciCandidateV1:
    root = manifest_path.resolve().parent
    value = _load_closed_json(manifest_path)
    expected = {
        "artifact_attestation_ref",
        "attestation_bundle",
        "discovery_tag",
        "producer_commit",
        "producer_repository",
        "release_layers",
        "repository",
        "schema_version",
        "strategy_coordinate",
    }
    if "artifact_ref_digest" in value:
        raise ValueError("artifact_ref_digest input is forbidden")
    _require_exact_keys(value, expected, "publish input")
    if value["schema_version"] != PUBLISH_INPUT_SCHEMA_VERSION:
        raise ValueError("publish input schema version differs")
    if value["repository"] != repository:
        raise ValueError("publish input repository differs")
    if value["producer_repository"] != context.producer_repository:
        raise ValueError("publish input producer repository differs")
    if value["producer_commit"] != context.producer_commit:
        raise ValueError("publish input producer commit differs from workflow")

    raw_layers = _as_list(value["release_layers"], "release_layers")
    if len(raw_layers) != len(RELEASE_LAYER_MEDIA_TYPES):
        raise ValueError("release layer count differs")
    release_layers = tuple(
        _load_blob(
            root,
            _as_mapping(item, "release layer"),
            expected_role=role,
            expected_media_type=RELEASE_LAYER_MEDIA_TYPES[role],
        )
        for role, item in zip(RELEASE_LAYER_MEDIA_TYPES, raw_layers, strict=True)
    )
    attestation_bundle = _load_blob(
        root,
        _as_mapping(value["attestation_bundle"], "attestation_bundle"),
        expected_role="attestation_bundle",
        expected_media_type=SIGSTORE_BUNDLE_MEDIA_TYPE,
    )

    ref_spec = _as_mapping(value["artifact_attestation_ref"], "artifact_attestation_ref")
    _require_exact_keys(
        ref_spec,
        {"path", "sha256", "size_bytes"},
        "artifact_attestation_ref",
    )
    ref_content = _read_exact_file(root, ref_spec)
    ref_value = _load_closed_json_bytes(ref_content, "artifact_attestation_ref")
    attestation_ref = ArtifactAttestationRefV1(
        schema_version=_as_str(ref_value["schema_version"], "ref.schema_version"),
        statement_coordinate=_as_str(ref_value["statement_coordinate"], "ref.statement_coordinate"),
        statement_sha256=_as_str(ref_value["statement_sha256"], "ref.statement_sha256"),
        bundle_coordinate=_as_str(ref_value["bundle_coordinate"], "ref.bundle_coordinate"),
        bundle_sha256=_as_str(ref_value["bundle_sha256"], "ref.bundle_sha256"),
        payload_type=_as_str(ref_value["payload_type"], "ref.payload_type"),
        predicate_type=_as_str(ref_value["predicate_type"], "ref.predicate_type"),
    )
    if canonical_json_bytes(asdict(attestation_ref)) != ref_content:
        raise ValueError("artifact attestation ref is not canonical JSON")

    return OciCandidateV1(
        repository=_as_str(value["repository"], "repository"),
        discovery_tag=_as_str(value["discovery_tag"], "discovery_tag"),
        producer_repository=_as_str(value["producer_repository"], "producer_repository"),
        producer_commit=_as_str(value["producer_commit"], "producer_commit"),
        strategy_coordinate=_as_str(value["strategy_coordinate"], "strategy_coordinate"),
        workflow=context.workflow,
        release_layers=release_layers,
        attestation_bundle=attestation_bundle,
        attestation_ref=attestation_ref,
    )


def _load_blob(
    root: Path,
    value: Mapping[str, object],
    *,
    expected_role: str,
    expected_media_type: str,
) -> OciBlobV1:
    _require_exact_keys(
        value,
        {"media_type", "name", "path", "role", "sha256", "size_bytes"},
        expected_role,
    )
    if value["role"] != expected_role:
        raise ValueError(f"{expected_role} role differs")
    if value["media_type"] != expected_media_type:
        raise ValueError(f"{expected_role} media type differs")
    content = _read_exact_file(root, value)
    return OciBlobV1(
        role=expected_role,
        name=_as_str(value["name"], f"{expected_role}.name"),
        media_type=expected_media_type,
        content=content,
    )


def _read_exact_file(root: Path, value: Mapping[str, object]) -> bytes:
    relative = _as_str(value["path"], "artifact path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or "\\" in relative or relative.endswith("/"):
        raise ValueError("artifact path must be a safe relative POSIX path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("artifact path must be a safe relative path") from error
    if not path.is_file() or path.is_symlink():
        raise ValueError("artifact path must resolve to one regular file")
    content = path.read_bytes()
    expected_size = _as_int(value["size_bytes"], "artifact size")
    expected_digest = _as_str(value["sha256"], "artifact digest")
    if len(content) != expected_size:
        raise ValueError("artifact size differs")
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("artifact digest differs")
    return content


def _load_closed_json(path: Path) -> dict[str, object]:
    return _load_closed_json_bytes(path.read_bytes(), str(path))


def _load_closed_json_bytes(content: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is forbidden: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields differ")


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value

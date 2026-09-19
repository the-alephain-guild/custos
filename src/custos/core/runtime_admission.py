"""Consume signed image promotion without granting deployment or business authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from custos.artifacts.policy import SigstoreIdentityV1
from custos.artifacts.sigstore_verifier import _read_stable_regular_file, verify_sigstore_blob

_REPOSITORY = "the-alephain-guild/custos"
RUNTIME_SOURCE_REVISION_FILE = Path("/usr/local/share/custos/source-revision")
_WORKFLOW = ".github/workflows/promote-runtime-candidate.yml"
_IDENTITY = SigstoreIdentityV1(
    issuer="https://token.actions.githubusercontent.com",
    workflow_identity=f"https://github.com/{_REPOSITORY}/{_WORKFLOW}@refs/heads/main",
    source_repository=f"https://github.com/{_REPOSITORY}",
)
_FIELDS = (
    "runtime_promotion_receipt",
    "runtime_promotion_bundle",
    "runtime_sigstore_trusted_root",
    "runtime_image_digest",
    "runtime_source_revision",
)


class RuntimeAdmissionError(ValueError):
    """Runtime promotion evidence is absent, invalid or bound to another deployment."""


@dataclass(frozen=True)
class RuntimeAdmission:
    image_digest: str
    source_revision: str
    receipt_sha256: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeAdmissionError(message)


def _fields(value: object, expected: str) -> dict[str, Any]:
    _require(
        isinstance(value, dict) and set(value) == set(expected.split()),
        "runtime receipt fields differ",
    )
    return value  # type: ignore[return-value]


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate runtime receipt field")
        result[key] = value
    return result


def load_runtime_admission(args: object) -> RuntimeAdmission | None:
    """Default to blocked; partial inputs never degrade to an enabled runtime.

    Image digest and revision are supplied by trusted deployment configuration.
    The receipt must match both. Signed per-deployment promotion and runner risk
    authority remain independent checks in the lifecycle supervisor.
    """
    values = [getattr(args, name, None) for name in _FIELDS]
    if not any(values):
        return None
    _require(
        all(values), "runtime admission requires complete receipt, bundle, root and image identity"
    )
    receipt_path, bundle_path, root_path, image_digest, source_revision = values
    _require(
        isinstance(image_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is not None,
        "runtime image digest is invalid",
    )
    _require(
        isinstance(source_revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None,
        "runtime source revision is invalid",
    )
    try:
        payload = _read_stable_regular_file(Path(receipt_path), "runtime promotion receipt")
        root = _read_stable_regular_file(Path(root_path), "runtime Sigstore trusted root")
        _require(len(payload) <= 1024 * 1024, "runtime receipt exceeds the size limit")
        verify_sigstore_blob(
            payload=payload,
            bundle_path=Path(bundle_path),
            trusted_root_bytes=root,
            identity=_IDENTITY,
        )
    except Exception as error:
        raise RuntimeAdmissionError("runtime admission signature verification failed") from error
    try:
        running_revision = (
            _read_stable_regular_file(RUNTIME_SOURCE_REVISION_FILE, "running image source revision")
            .decode("ascii")
            .strip()
        )
    except Exception as error:
        raise RuntimeAdmissionError("running image revision is unavailable") from error
    _require(running_revision == source_revision, "running image revision differs from deployment")
    try:
        document = json.loads(payload, object_pairs_hook=_pairs)
        doc = _fields(
            document,
            "schema_version receipt_id status candidate inputs workflow invariants promoted_at artifact_runtime_ready system_production_ready",
        )
        _require(
            type(doc["schema_version"]) is int and doc["schema_version"] == 1,
            "runtime receipt version differs",
        )
        _require(
            doc["receipt_id"] == "CUSTOS-RUNTIME-CANDIDATE-PROMOTION-V1"
            and doc["status"] == "PROMOTED_UNCHANGED",
            "runtime promotion is not accepted",
        )
        _require(
            doc["artifact_runtime_ready"] is True and doc["system_production_ready"] is False,
            "runtime receipt scope differs",
        )
        candidate = _fields(doc["candidate"], "repository digest source_revision platforms")
        _require(
            candidate["repository"] == f"ghcr.io/{_REPOSITORY}"
            and candidate["digest"] == image_digest
            and candidate["source_revision"] == source_revision,
            "runtime receipt does not bind this image and revision",
        )
        _require(
            candidate["platforms"] == ["linux/amd64", "linux/arm64"],
            "runtime image platforms differ",
        )
        invariants = _fields(
            doc["invariants"],
            "manifest_digest_before manifest_digest_after exact_digest_unchanged image_rebuilt tag_repointed registry_mutated",
        )
        _require(
            invariants["manifest_digest_before"] == image_digest
            and invariants["manifest_digest_after"] == image_digest
            and invariants["exact_digest_unchanged"] is True,
            "runtime promotion changed image bytes",
        )
        _require(
            all(
                invariants[name] is False
                for name in ("image_rebuilt", "tag_repointed", "registry_mutated")
            ),
            "runtime promotion mutated the artifact",
        )
        workflow = _fields(
            doc["workflow"],
            "repository revision run_id run_attempt workflow_file workflow_identity environment oidc_issuer",
        )
        _require(
            workflow["repository"] == _REPOSITORY
            and workflow["workflow_file"] == _WORKFLOW
            and workflow["workflow_identity"] == _IDENTITY.workflow_identity
            and workflow["oidc_issuer"] == _IDENTITY.issuer
            and workflow["environment"] == "v1-team-runtime-promotion",
            "runtime promotion workflow differs",
        )
        _require(
            isinstance(workflow["revision"], str)
            and re.fullmatch(r"[0-9a-f]{40}", workflow["revision"]) is not None,
            "runtime workflow revision is invalid",
        )
        _require(
            all(type(workflow[k]) is int and workflow[k] > 0 for k in ("run_id", "run_attempt")),
            "runtime workflow identity is incomplete",
        )
        inputs = _fields(doc["inputs"], "publication crucible_acceptance strategy_owner_acceptance")
        for name, owner in (
            ("publication", "custos"),
            ("crucible_acceptance", "crucible-rust"),
            ("strategy_owner_acceptance", "philosophers-stone"),
        ):
            item = _fields(inputs[name], "receipt_id owner sha256 size_bytes")
            _require(
                item["owner"] == owner
                and isinstance(item["receipt_id"], str)
                and bool(item["receipt_id"]),
                "runtime owner acceptance differs",
            )
            _require(
                isinstance(item["sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None
                and type(item["size_bytes"]) is int
                and item["size_bytes"] > 0,
                "runtime owner evidence binding is invalid",
            )
        promoted_at = datetime.fromisoformat(doc["promoted_at"].replace("Z", "+00:00"))
        _require(promoted_at.tzinfo is not None, "runtime promotion time has no timezone")
    except RuntimeAdmissionError:
        raise
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeAdmissionError("runtime promotion receipt is malformed") from error
    return RuntimeAdmission(image_digest, source_revision, hashlib.sha256(payload).hexdigest())

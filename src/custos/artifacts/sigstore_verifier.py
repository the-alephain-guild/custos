from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.verification_types import (
    DigestSubject,
    SigstoreVerificationEvidence,
    SigstoreVerificationRequest,
)

SIGSTORE_DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_V1 = "https://in-toto.io/Statement/v1"


@dataclass(frozen=True, slots=True)
class _SigstoreBindings:
    Bundle: Any
    TrustedRoot: Any
    RekorClient: Any
    Verifier: Any
    Identity: Any
    AllOf: Any
    GitHubWorkflowRepository: Any


class _DuplicateJsonKey(ValueError):
    pass


def _load_sigstore_bindings() -> _SigstoreBindings:
    try:
        from sigstore._internal.rekor.client import RekorClient
        from sigstore._internal.trust import TrustedRoot
        from sigstore.models import Bundle
        from sigstore.verify.policy import AllOf, GitHubWorkflowRepository, Identity
        from sigstore.verify.verifier import Verifier
    except ImportError as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_VERIFIER_UNAVAILABLE,
            "sigstore runtime dependency is not installed",
        ) from error
    return _SigstoreBindings(
        Bundle=Bundle,
        TrustedRoot=TrustedRoot,
        RekorClient=RekorClient,
        Verifier=Verifier,
        Identity=Identity,
        AllOf=AllOf,
        GitHubWorkflowRepository=GitHubWorkflowRepository,
    )


def verify_sigstore_blob(
    *, payload: bytes, bundle_path: Path, trusted_root_bytes: bytes, identity: Any
) -> None:
    """Verify exact blob bytes against a provisioned root and fixed workflow identity."""
    bindings = _load_sigstore_bindings()
    try:
        bundle_bytes = _read_stable_regular_file(bundle_path, "runtime promotion bundle")
        bundle = bindings.Bundle.from_json(bundle_bytes)
        verifier = bindings.Verifier(
            rekor=bindings.RekorClient("https://offline.invalid"),
            trusted_root=_trusted_root_from_bytes(bindings, trusted_root_bytes),
        )
        policy = bindings.AllOf(
            [
                bindings.Identity(identity=identity.workflow_identity, issuer=identity.issuer),
                bindings.GitHubWorkflowRepository(
                    _github_repository_coordinate(identity.source_repository)
                ),
            ]
        )
        verifier.verify_artifact(payload, bundle, policy)
    except Exception as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
            "runtime promotion signature verification failed",
        ) from error


def _read_stable_regular_file(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
            f"{label} is not an accessible no-follow file",
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
                f"{label} is not a regular file",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        payload = b"".join(chunks)
        if before_identity != after_identity or len(payload) != after.st_size:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
                f"{label} changed while being read",
            )
        return payload
    finally:
        os.close(descriptor)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _parse_in_toto_subjects(payload: bytes) -> tuple[DigestSubject, ...]:
    try:
        statement = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, _DuplicateJsonKey) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "verified DSSE payload is not strict UTF-8 JSON",
        ) from error
    if not isinstance(statement, dict) or statement.get("_type") != IN_TOTO_STATEMENT_V1:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "verified DSSE payload is not an in-toto Statement v1",
        )
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not subjects:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "verified in-toto statement has no subjects",
        )
    parsed: list[DigestSubject] = []
    names: set[str] = set()
    for subject in subjects:
        if not isinstance(subject, dict) or set(subject) != {"name", "digest"}:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "in-toto subject must contain only name and digest",
            )
        name = subject["name"]
        digest = subject["digest"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", name)
            or name in names
            or not isinstance(digest, dict)
            or set(digest) != {"sha256"}
            or not isinstance(digest["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest["sha256"])
        ):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "in-toto subjects are duplicate, ambiguous, or lack exact sha256 evidence",
            )
        names.add(name)
        parsed.append(DigestSubject(name, digest["sha256"]))
    return tuple(parsed)


def _github_repository_coordinate(source_repository: str) -> str:
    parsed = urlsplit(source_repository)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "v1 Sigstore policy requires an exact HTTPS GitHub repository identity",
        )
    coordinate = parsed.path.strip("/")
    if len(coordinate.split("/")) != 2 or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", coordinate
    ):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
            "v1 Sigstore repository identity is not an owner/repository coordinate",
        )
    return coordinate


def _trusted_root_from_bytes(bindings: _SigstoreBindings, trusted_root_bytes: bytes) -> Any:
    try:
        # sigstore-python 3.x cannot deserialize the PKIX_ED25519 enum used by
        # newer Rekor v2 entries in the public-good trusted-root snapshot. Keep
        # the caller-provided bytes as the policy/evidence authority, but give
        # the v3 parser a compatibility view that omits only those unsupported
        # log keys. A bundle signed by an omitted log still fails closed because
        # the verifier cannot find its log ID in this view.
        trusted_root = json.loads(
            trusted_root_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if not isinstance(trusted_root, dict):
            raise ValueError("trusted root must be a JSON object")
        tlogs = trusted_root.get("tlogs")
        if isinstance(tlogs, list):
            compatible_tlogs = [
                entry
                for entry in tlogs
                if not (
                    isinstance(entry, dict)
                    and isinstance(entry.get("publicKey"), dict)
                    and entry["publicKey"].get("keyDetails") == "PKIX_ED25519"
                )
            ]
            if tlogs and not compatible_tlogs:
                raise ValueError("trusted root has no Sigstore v3-compatible tlog")
            trusted_root["tlogs"] = compatible_tlogs
        compatible_bytes = json.dumps(
            trusted_root,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="custos-sigstore-root-") as directory:
            path = Path(directory) / "trusted-root.json"
            path.write_bytes(compatible_bytes)
            os.chmod(path, 0o600)
            return bindings.TrustedRoot.from_file(str(path))
    except ArtifactVerificationError:
        raise
    except Exception as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
            "injected Sigstore trusted-root bytes are invalid",
        ) from error


class ProductionSigstoreVerifier:
    """Offline Sigstore DSSE verifier using only caller-injected trusted-root bytes."""

    capability_id = "sigstore-python-3-offline-dsse-v1"

    def verify(self, request: SigstoreVerificationRequest) -> SigstoreVerificationEvidence:
        if not request.accepted_identities or not request.required_subjects:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "Sigstore verification requires identities and exact subjects",
            )
        expected_subjects = {
            (subject.name, subject.sha256) for subject in request.required_subjects
        }
        if len(expected_subjects) != len(request.required_subjects):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "required Sigstore subjects must be unique",
            )

        bindings = _load_sigstore_bindings()
        bundle_bytes = _read_stable_regular_file(request.bundle_path, "Sigstore bundle")
        root_digest = hashlib.sha256(request.trusted_root_bytes).hexdigest()
        bundle_digest = hashlib.sha256(bundle_bytes).hexdigest()
        try:
            trusted_root = _trusted_root_from_bytes(bindings, request.trusted_root_bytes)
            bundle = bindings.Bundle.from_json(bundle_bytes)
            # The Rekor client is required by sigstore-python's constructor but bundle
            # verification is fully offline: verify_dsse consumes the bundled proof,
            # checkpoint, SET and integrated time. This client is never called.
            verifier = bindings.Verifier(
                rekor=bindings.RekorClient("https://offline.invalid"),
                trusted_root=trusted_root,
            )
        except ArtifactVerificationError:
            raise
        except Exception as error:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
                "Sigstore bundle or injected trusted root is invalid",
            ) from error

        matched_identity = None
        payload_type = ""
        payload = b""
        for identity in request.accepted_identities:
            repository = _github_repository_coordinate(identity.source_repository)
            policy = bindings.AllOf(
                [
                    bindings.Identity(
                        identity=identity.workflow_identity,
                        issuer=identity.issuer,
                    ),
                    bindings.GitHubWorkflowRepository(repository),
                ]
            )
            try:
                payload_type, payload = verifier.verify_dsse(bundle, policy)
            except Exception:
                continue
            matched_identity = identity
            break
        if matched_identity is None:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED,
                "Sigstore certificate, identity, signature, or transparency evidence failed",
            )
        if payload_type != SIGSTORE_DSSE_PAYLOAD_TYPE:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "verified DSSE payload type is not the required in-toto media type",
            )
        verified_subjects = _parse_in_toto_subjects(payload)
        actual_subjects = {(subject.name, subject.sha256) for subject in verified_subjects}
        if actual_subjects != expected_subjects:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "verified in-toto subjects do not equal the command-bound subject set",
            )
        integrated_time = bundle.log_entry.integrated_time
        if not isinstance(integrated_time, int) or integrated_time <= 0:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH,
                "verified Rekor entry lacks a positive integrated time",
            )
        return SigstoreVerificationEvidence(
            verifier_capability_id=self.capability_id,
            bundle_sha256=bundle_digest,
            trusted_root_sha256=root_digest,
            issuer=matched_identity.issuer,
            workflow_identity=matched_identity.workflow_identity,
            source_repository=matched_identity.source_repository,
            verified_subjects=verified_subjects,
            transparency_log_verified=True,
        )

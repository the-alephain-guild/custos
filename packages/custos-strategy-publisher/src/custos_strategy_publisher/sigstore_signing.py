"""Manual, non-production Sigstore signing preparation for strategy artifact.

This module deliberately produces workflow artifacts outside the repository. It is not
a verifier, publisher, release gate, or fallback for the legacy image lane.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

CANONICAL_STATEMENT = Path("docs/authority/strategy-release-statement-v1.golden.json")
BUNDLE_FILENAME = "strategy-release-statement-v1.sigstore.json"
TRUSTED_ROOT_FILENAME = "sigstore-public-good-trusted-root.json"
PROVENANCE_FILENAME = "strategy-release-statement-v1.provenance.json"
BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
TRUSTED_ROOT_MEDIA_TYPE = "application/vnd.dev.sigstore.trustedroot+json;version=0.1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
SIGSTORE_VERSION = "4.4.0"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SigningPreparationError(RuntimeError):
    """Raised when the preparation cannot fail closed."""


@dataclass(frozen=True)
class SigningResult:
    """Opaque outputs returned by a signing backend."""

    bundle_json: str
    trusted_root_json: str
    certificate_identity: str
    oidc_issuer: str
    library_version: str


@dataclass(frozen=True)
class ValidatedSigningMaterial:
    bundle_bytes: bytes
    trusted_root_bytes: bytes
    certificate_identity: str
    oidc_issuer: str
    library_version: str


class SigningBackend(Protocol):
    """Narrow boundary mocked by tests and implemented by sigstore-python."""

    def sign_dsse(self, statement_bytes: bytes) -> SigningResult:
        """Sign the exact statement bytes without reserializing them."""


class PublicGoodSigstoreBackend:
    """Official sigstore-python public-good signing adapter."""

    def sign_dsse(self, statement_bytes: bytes) -> SigningResult:
        from sigstore import dsse
        from sigstore.models import Bundle, ClientTrustConfig
        from sigstore.oidc import IdentityToken, detect_credential
        from sigstore.sign import SigningContext

        raw_token = detect_credential()
        if raw_token is None:
            raise SigningPreparationError(
                "ambient OIDC credential is unavailable; interactive and explicit-token fallbacks are forbidden"
            )

        identity_token = IdentityToken(raw_token)
        trust_config = ClientTrustConfig.production()
        context = SigningContext.from_trust_config(trust_config)
        statement = dsse.Statement(statement_bytes)

        # The signer must retain one ephemeral key for both the Fulcio CSR and the
        # DSSE signature. The context scope prevents reuse beyond this statement.
        with context.signer(identity_token) as signer:
            bundle = signer.sign_dsse(statement)

        bundle_json = bundle.to_json()
        # Parse once through the official model before applying the stricter strategy artifact
        # structural requirements below.
        Bundle.from_json(bundle_json)

        trusted_root_inner = getattr(trust_config.trusted_root, "_inner", None)
        serialize_trusted_root = getattr(trusted_root_inner, "to_json", None)
        if not callable(serialize_trusted_root):
            raise SigningPreparationError(
                "sigstore-python no longer exposes the pinned trusted-root serialization boundary"
            )

        return SigningResult(
            bundle_json=bundle_json,
            trusted_root_json=serialize_trusted_root(),
            certificate_identity=identity_token.identity,
            oidc_issuer=identity_token.issuer,
            library_version=version("sigstore"),
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_nonempty_base64(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SigningPreparationError(f"{field} must be non-empty base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SigningPreparationError(f"{field} must be valid base64") from exc
    if not decoded:
        raise SigningPreparationError(f"{field} must decode to non-empty bytes")
    return decoded


def validate_bundle_json(bundle_json: str, statement_bytes: bytes) -> dict[str, object]:
    """Require the exact DSSE payload and every offline Rekor proof component."""

    try:
        bundle = json.loads(bundle_json)
    except json.JSONDecodeError as exc:
        raise SigningPreparationError("bundle is not valid JSON") from exc
    if not isinstance(bundle, dict):
        raise SigningPreparationError("bundle must be a JSON object")
    if bundle.get("mediaType") != BUNDLE_MEDIA_TYPE:
        raise SigningPreparationError("bundle mediaType must be Sigstore Bundle v0.3")

    envelope = bundle.get("dsseEnvelope")
    if not isinstance(envelope, dict):
        raise SigningPreparationError("bundle must contain a DSSE envelope")
    if envelope.get("payloadType") != DSSE_PAYLOAD_TYPE:
        raise SigningPreparationError("DSSE payloadType must be application/vnd.in-toto+json")
    payload = _decode_nonempty_base64(envelope.get("payload"), "dsseEnvelope.payload")
    if payload != statement_bytes:
        raise SigningPreparationError(
            "signed DSSE payload differs byte-for-byte from the golden statement"
        )
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        raise SigningPreparationError("DSSE envelope must contain exactly one signature")
    signature = signatures[0]
    if not isinstance(signature, dict):
        raise SigningPreparationError("DSSE signature must be an object")
    _decode_nonempty_base64(signature.get("sig"), "dsseEnvelope.signatures[0].sig")

    material = bundle.get("verificationMaterial")
    if not isinstance(material, dict):
        raise SigningPreparationError("bundle verificationMaterial must be an object")
    certificate = material.get("certificate")
    if not isinstance(certificate, dict):
        raise SigningPreparationError("bundle must contain a signing certificate")
    _decode_nonempty_base64(
        certificate.get("rawBytes"), "verificationMaterial.certificate.rawBytes"
    )

    entries = material.get("tlogEntries")
    if not isinstance(entries, list) or len(entries) != 1:
        raise SigningPreparationError("bundle must contain exactly one transparency-log entry")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise SigningPreparationError("transparency-log entry must be an object")
    _decode_nonempty_base64(entry.get("canonicalizedBody"), "tlogEntry.canonicalizedBody")
    log_id = entry.get("logId")
    if not isinstance(log_id, dict):
        raise SigningPreparationError("transparency-log entry must contain logId")
    _decode_nonempty_base64(log_id.get("keyId"), "tlogEntry.logId.keyId")

    promise = entry.get("inclusionPromise")
    if not isinstance(promise, dict):
        raise SigningPreparationError("transparency-log entry must contain an inclusion promise")
    _decode_nonempty_base64(
        promise.get("signedEntryTimestamp"),
        "tlogEntry.inclusionPromise.signedEntryTimestamp",
    )

    proof = entry.get("inclusionProof")
    if not isinstance(proof, dict):
        raise SigningPreparationError("transparency-log entry must contain an inclusion proof")
    _decode_nonempty_base64(proof.get("rootHash"), "tlogEntry.inclusionProof.rootHash")
    try:
        tree_size = int(proof.get("treeSize", 0))
    except (TypeError, ValueError) as exc:
        raise SigningPreparationError(
            "inclusion proof treeSize must be a positive integer"
        ) from exc
    if tree_size <= 0:
        raise SigningPreparationError("inclusion proof treeSize must be a positive integer")
    checkpoint = proof.get("checkpoint")
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("envelope"), str):
        raise SigningPreparationError("inclusion proof must contain a signed checkpoint envelope")
    if not checkpoint["envelope"].strip():
        raise SigningPreparationError("inclusion proof checkpoint envelope must be non-empty")
    return bundle


def validate_trusted_root_json(trusted_root_json: str) -> dict[str, object]:
    """Require a complete public-good snapshot without treating it as acceptance evidence."""

    try:
        trusted_root = json.loads(trusted_root_json)
    except json.JSONDecodeError as exc:
        raise SigningPreparationError("trusted-root snapshot is not valid JSON") from exc
    if not isinstance(trusted_root, dict):
        raise SigningPreparationError("trusted-root snapshot must be a JSON object")
    if trusted_root.get("mediaType") != TRUSTED_ROOT_MEDIA_TYPE:
        raise SigningPreparationError("trusted-root snapshot has an unsupported mediaType")
    for field in ("tlogs", "ctlogs", "certificateAuthorities"):
        value = trusted_root.get(field)
        if not isinstance(value, list) or not value:
            raise SigningPreparationError(f"trusted-root snapshot must contain {field}")
    return trusted_root


def sign_exact_statement(
    statement_bytes: bytes,
    *,
    backend: SigningBackend,
    expected_certificate_identity: str | None = None,
    expected_oidc_issuer: str | None = None,
) -> ValidatedSigningMaterial:
    """Sign exact bytes and validate every returned trust component before use."""

    result = backend.sign_dsse(statement_bytes)
    if result.library_version != SIGSTORE_VERSION:
        raise SigningPreparationError(
            f"signing backend version must be exactly {SIGSTORE_VERSION}, got {result.library_version}"
        )
    validate_bundle_json(result.bundle_json, statement_bytes)
    validate_trusted_root_json(result.trusted_root_json)
    if (
        expected_certificate_identity is not None
        and result.certificate_identity != expected_certificate_identity
    ):
        raise SigningPreparationError("Sigstore certificate identity differs")
    if expected_oidc_issuer is not None and result.oidc_issuer != expected_oidc_issuer:
        raise SigningPreparationError("Sigstore OIDC issuer differs")
    return ValidatedSigningMaterial(
        bundle_bytes=result.bundle_json.encode("utf-8"),
        trusted_root_bytes=result.trusted_root_json.encode("utf-8"),
        certificate_identity=result.certificate_identity,
        oidc_issuer=result.oidc_issuer,
        library_version=result.library_version,
    )


def _read_frozen_statement(repo_root: Path) -> tuple[Path, bytes]:
    statement_path = repo_root / CANONICAL_STATEMENT
    statement_bytes = statement_path.read_bytes()
    sidecar_path = statement_path.with_suffix(f"{statement_path.suffix}.sha256")
    sidecar_parts = sidecar_path.read_text(encoding="ascii").split()
    if not sidecar_parts or _SHA256_RE.fullmatch(sidecar_parts[0]) is None:
        raise SigningPreparationError("statement digest sidecar is malformed")
    if _sha256(statement_bytes) != sidecar_parts[0]:
        raise SigningPreparationError("statement bytes do not match the checked-in digest sidecar")
    return statement_path, statement_bytes


def _workflow_context(environ: dict[str, str]) -> dict[str, str]:
    keys = (
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_ACTOR_ID",
    )
    return {key.lower(): environ[key] for key in keys if environ.get(key)}


def prepare_signing_outputs(
    repo_root: Path,
    output_dir: Path,
    *,
    backend: SigningBackend,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """Sign and stage validated artifacts outside the source repository."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir == repo_root or repo_root in output_dir.parents:
        raise SigningPreparationError("signing outputs must be written outside the repository")
    if output_dir.exists():
        raise SigningPreparationError("signing output directory must not already exist")

    statement_path, statement_bytes = _read_frozen_statement(repo_root)
    material = sign_exact_statement(statement_bytes, backend=backend)
    bundle_bytes = material.bundle_bytes
    trusted_root_bytes = material.trusted_root_bytes
    provenance: dict[str, object] = {
        "schema_version": 1,
        "test_only": True,
        "non_production": True,
        "production_use": "forbidden",
        "statement": {
            "path": statement_path.relative_to(repo_root).as_posix(),
            "sha256": _sha256(statement_bytes),
        },
        "bundle": {
            "filename": BUNDLE_FILENAME,
            "media_type": BUNDLE_MEDIA_TYPE,
            "sha256": _sha256(bundle_bytes),
        },
        "trusted_root": {
            "filename": TRUSTED_ROOT_FILENAME,
            "media_type": TRUSTED_ROOT_MEDIA_TYPE,
            "sha256": _sha256(trusted_root_bytes),
            "source": "ClientTrustConfig.production",
        },
        "signer": {
            "certificate_identity": material.certificate_identity,
            "oidc_issuer": material.oidc_issuer,
            "rekor_selection": "ClientTrustConfig.production default",
            "sigstore_python_version": material.library_version,
        },
        "workflow": _workflow_context(environ if environ is not None else dict(os.environ)),
    }
    provenance_bytes = (
        json.dumps(provenance, sort_keys=True, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / BUNDLE_FILENAME).write_bytes(bundle_bytes)
    (output_dir / TRUSTED_ROOT_FILENAME).write_bytes(trusted_root_bytes)
    (output_dir / PROVENANCE_FILENAME).write_bytes(provenance_bytes)
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_signing_outputs(
        args.repo_root,
        args.output_dir,
        backend=PublicGoodSigstoreBackend(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

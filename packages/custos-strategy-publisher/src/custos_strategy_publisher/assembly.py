"""Sign a frozen candidate and assemble exactly what will be published.

Nothing is rebuilt here: the statement the candidate job froze is signed as is.
Before signing, the identity the statement was built for must be the one this
job's OIDC token carries; after signing, the certificate must name that same
workflow and the producer's repository. A mismatch at either point stops the
release before anything reaches a registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from .candidate import (
    UNSIGNED_CANDIDATE_FILENAME,
    UnsignedCandidateError,
    load_unsigned_candidate,
)
from .github_oidc import certificate_identity
from .model import (
    ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION,
    DSSE_PAYLOAD_TYPE,
    STRATEGY_RELEASE_PREDICATE_TYPE,
    ArtifactAttestationRefV1,
    canonical_json_bytes,
)
from .oci_primitives import (
    GITHUB_OIDC_ISSUER,
    SIGSTORE_BUNDLE_MEDIA_TYPE,
    OciBlobV1,
    PublicationWorkflowIdentityV1,
)
from .oci_publication import PUBLISH_INPUT_V1_SCHEMA_VERSION, OciCandidateV1
from .sigstore_signing import (
    PublicGoodSigstoreBackend,
    SigningBackend,
    sign_exact_statement,
)

PUBLICATION_SIGNING_PROVENANCE = "publication-signing-provenance-v1.json"
PUBLICATION_TRUSTED_ROOT = "sigstore-public-good-trusted-root.json"
PUBLISH_INPUT_FILENAME = "publish-input-v1.json"
ATTESTATION_BUNDLE_FILENAME = "strategy-release.bundle.json"
ATTESTATION_REF_FILENAME = "artifact-attestation-ref.json"

_ACTIONS_ARTIFACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _coordinate(repository: str, filename: str, digest: str) -> str:
    return f"oci://{repository}/{filename}@sha256:{digest}"


def _statement_identity(statement: bytes) -> tuple[str, str]:
    """The producer repository and workflow identity the statement was built for."""

    try:
        predicate = json.loads(statement)["predicate"]
        return predicate["producer_repository"], predicate["workflow_identity"]
    except (KeyError, TypeError, ValueError) as error:
        raise UnsignedCandidateError("the statement names no producer or workflow") from error


def _file_spec(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": _sha256(content),
        "size_bytes": len(content),
    }


def _atomic_materialize(output: Path, files: dict[str, bytes]) -> None:
    output = output.resolve()
    if output.exists():
        raise UnsignedCandidateError("publication assembly output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    temporary.mkdir(exist_ok=False)
    try:
        for relative, content in sorted(files.items()):
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def assemble_publication_input(
    unsigned_envelope: Path,
    output: Path,
    *,
    repo_root: Path,
    producer_commit: str,
    producer_repository: str,
    repository: str,
    candidate_workflow_ref: str,
    workflow: PublicationWorkflowIdentityV1,
    candidate_run_id: int,
    candidate_artifact_name: str,
    candidate_artifact_digest: str,
    backend: SigningBackend | None = None,
) -> OciCandidateV1:
    """Verify, sign and assemble without rebuilding any candidate leaf."""

    if _ACTIONS_ARTIFACT_DIGEST_RE.fullmatch(candidate_artifact_digest) is None:
        raise UnsignedCandidateError("GitHub Actions artifact digest differs")
    candidate = load_unsigned_candidate(
        unsigned_envelope,
        repo_root=repo_root,
        expected_producer_commit=producer_commit,
        expected_producer_repository=producer_repository,
        expected_repository=repository,
        expected_workflow_ref=candidate_workflow_ref,
        expected_run_id=candidate_run_id,
        expected_artifact_name=candidate_artifact_name,
    )
    statement = next(
        layer for layer in candidate.release_layers if layer.role == "strategy_release_statement"
    )
    built_for = _statement_identity(statement.content)
    producer_url = f"https://github.com/{producer_repository}"
    if built_for != (producer_url, workflow.workflow_identity):
        raise UnsignedCandidateError(
            f"the statement was built for {built_for[1]} in {built_for[0]}, "
            f"but this job signs as {workflow.workflow_identity} in {producer_url}"
        )
    if not workflow.oidc_subject.startswith(f"repo:{producer_repository}:"):
        raise UnsignedCandidateError("this job's OIDC subject names another repository")
    signing = sign_exact_statement(
        statement.content,
        backend=backend if backend is not None else PublicGoodSigstoreBackend(),
        expected_certificate_identity=workflow.oidc_subject,
        expected_oidc_issuer=GITHUB_OIDC_ISSUER,
    )
    signed_as = certificate_identity(signing.bundle_bytes)
    if signed_as != (workflow.workflow_identity, producer_repository):
        raise UnsignedCandidateError(
            f"the certificate names {signed_as[0]} in {signed_as[1]}, "
            f"not {workflow.workflow_identity} in {producer_repository}"
        )
    statement_digest = _sha256(statement.content)
    bundle_digest = _sha256(signing.bundle_bytes)
    attestation_ref = ArtifactAttestationRefV1(
        schema_version=ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION,
        statement_coordinate=_coordinate(candidate.repository, statement.name, statement_digest),
        statement_sha256=statement_digest,
        bundle_coordinate=_coordinate(
            candidate.repository, ATTESTATION_BUNDLE_FILENAME, bundle_digest
        ),
        bundle_sha256=bundle_digest,
        payload_type=DSSE_PAYLOAD_TYPE,
        predicate_type=STRATEGY_RELEASE_PREDICATE_TYPE,
    )
    attestation_ref_bytes = canonical_json_bytes(attestation_ref.to_mapping())
    release_layer_specs = [
        {
            "media_type": layer.media_type,
            "name": layer.name,
            **file_spec.to_mapping(),
            "role": layer.role,
        }
        for layer, file_spec in zip(
            candidate.release_layers,
            candidate.release_layer_files,
            strict=True,
        )
    ]
    publish_input = {
        "artifact_attestation_ref": _file_spec(
            ATTESTATION_REF_FILENAME,
            attestation_ref_bytes,
        ),
        "attestation_bundle": {
            "media_type": SIGSTORE_BUNDLE_MEDIA_TYPE,
            "name": ATTESTATION_BUNDLE_FILENAME,
            **_file_spec(ATTESTATION_BUNDLE_FILENAME, signing.bundle_bytes),
            "role": "attestation_bundle",
        },
        "discovery_tag": candidate.discovery_tag,
        "producer_commit": producer_commit,
        "producer_repository": candidate.producer_repository,
        "release_layers": release_layer_specs,
        "repository": candidate.repository,
        "schema_version": PUBLISH_INPUT_V1_SCHEMA_VERSION,
        "strategy_coordinate": candidate.strategy_coordinate,
    }
    publish_input_bytes = canonical_json_bytes(publish_input)
    provenance = {
        "candidate": {
            "actions_artifact_digest": candidate_artifact_digest,
            "artifact_name": candidate_artifact_name,
            "build_tree_sha256": candidate.build_tree_sha256,
            "envelope_path": UNSIGNED_CANDIDATE_FILENAME,
            "envelope_sha256": candidate.envelope_digest,
            "run_id": candidate_run_id,
            "workflow_ref": candidate.candidate_workflow.workflow_ref,
        },
        "external_publication_completed": False,
        "producer_commit": producer_commit,
        "schema_version": "alephain.strategy-artifact-publication-signing-provenance.v1",
        "signing": {
            "bundle_sha256": bundle_digest,
            "certificate_identity": signing.certificate_identity,
            "oidc_issuer": signing.oidc_issuer,
            "sigstore_python_version": signing.library_version,
            "statement_sha256": statement_digest,
            "trusted_root_sha256": _sha256(signing.trusted_root_bytes),
        },
        "workflow": asdict(workflow),
    }
    output_files = {
        file_spec.path: layer.content
        for layer, file_spec in zip(
            candidate.release_layers,
            candidate.release_layer_files,
            strict=True,
        )
    }
    output_files.update(
        {
            ATTESTATION_BUNDLE_FILENAME: signing.bundle_bytes,
            ATTESTATION_REF_FILENAME: attestation_ref_bytes,
            PUBLICATION_SIGNING_PROVENANCE: canonical_json_bytes(provenance),
            PUBLICATION_TRUSTED_ROOT: signing.trusted_root_bytes,
            PUBLISH_INPUT_FILENAME: publish_input_bytes,
        }
    )
    _atomic_materialize(output, output_files)
    return OciCandidateV1(
        repository=candidate.repository,
        discovery_tag=candidate.discovery_tag,
        producer_repository=candidate.producer_repository,
        producer_commit=producer_commit,
        strategy_coordinate=candidate.strategy_coordinate,
        workflow=workflow,
        release_layers=candidate.release_layers,
        attestation_bundle=OciBlobV1(
            role="attestation_bundle",
            name=ATTESTATION_BUNDLE_FILENAME,
            media_type=SIGSTORE_BUNDLE_MEDIA_TYPE,
            content=signing.bundle_bytes,
        ),
        attestation_ref=attestation_ref,
    )

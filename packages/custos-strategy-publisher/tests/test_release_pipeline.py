"""From two builds to a publication input, for a producer calling the reusable workflow.

Two identical builds freeze into one unsigned candidate; the publish job signs
that candidate's statement as it is and assembles what gets published. The
producer is whoever runs the workflow, so every step checks that the candidate,
the job's OIDC identity and the signing certificate all name the same producer
and the same publisher workflow.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from custos_strategy_publisher.assembly import (
    PUBLISH_INPUT_FILENAME,
    assemble_publication_input,
)
from custos_strategy_publisher.build_input import (
    BUILD_INPUT_FILENAME,
    BUILD_INPUT_SCHEMA_VERSION,
    PRODUCER_GATE_SCHEMA_VERSION,
)
from custos_strategy_publisher.candidate import (
    UNSIGNED_CANDIDATE_FILENAME,
    UNSIGNED_CANDIDATE_SIDECAR,
    UnsignedCandidateError,
    finalize_unsigned_candidate,
    load_unsigned_candidate,
)
from custos_strategy_publisher.github_oidc import (
    GITHUB_WORKFLOW_REPOSITORY_OID,
    GitHubOidcError,
    read_workflow_claims,
)
from custos_strategy_publisher.model import canonical_json_bytes
from custos_strategy_publisher.oci_primitives import (
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_ISSUER,
    RELEASE_LAYER_MEDIA_TYPES,
    PublicationWorkflowIdentityV1,
)
from custos_strategy_publisher.release import PublicationContextV1, load_candidate_v1
from custos_strategy_publisher.sigstore_signing import SigningResult

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
COMMIT = "d" * 40
PRODUCER = "example-owner/example-strategies"
REPOSITORY = "ghcr.io/example-owner/example-strategies/strategy-releases"
CALLER_WORKFLOW_REF = f"{PRODUCER}/.github/workflows/release.yml@refs/heads/main"
PUBLISHER_REF = (
    "the-alephain-guild/custos/.github/workflows/publish-strategy-release.yml@refs/tags/v0.4.0"
)
PUBLISHER_IDENTITY = f"https://github.com/{PUBLISHER_REF}"
SUBJECT = f"repo:{PRODUCER}:ref:refs/heads/main"
RUN_ID = 42
ARTIFACT_NAME = f"strategy-release-candidate-{COMMIT}-{RUN_ID}-1"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file(path: str, content: bytes) -> dict[str, object]:
    return {"path": path, "sha256": _sha256(content), "size_bytes": len(content)}


def _statement(workflow_identity: str = PUBLISHER_IDENTITY) -> bytes:
    value = json.loads((CONTRACTS / "strategy-release-statement-v1.golden.json").read_bytes())
    predicate = value["predicate"]
    for key in tuple(predicate):
        if key.endswith("_commit"):
            predicate[key] = COMMIT
    predicate["producer_repository"] = f"https://github.com/{PRODUCER}"
    predicate["workflow_identity"] = workflow_identity
    return canonical_json_bytes(value)


def _write_build(
    root: Path,
    *,
    producer: str = PRODUCER,
    fixture_marker: bool = False,
    workflow_identity: str = PUBLISHER_IDENTITY,
) -> None:
    root.mkdir(parents=True)
    contents = {
        "strategy_artifact": b"deterministic wheel bytes\n",
        "strategy_manifest": canonical_json_bytes(
            {"contract_only": True, "schema_version": 1}
            if fixture_marker
            else {"engine": "nautilus", "schema_version": 1}
        ),
        "strategy_artifact_ref": canonical_json_bytes({"schema_version": 1}),
        "strategy_release_bom": canonical_json_bytes({"producer_commit": COMMIT}),
        "strategy_sbom": canonical_json_bytes({"spdxVersion": "SPDX-2.3"}),
        "strategy_release_statement": _statement(workflow_identity),
    }
    layers = []
    for role, media_type in RELEASE_LAYER_MEDIA_TYPES.items():
        path = f"layers/{role}.bin"
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[role])
        layers.append(
            {
                "media_type": media_type,
                "name": f"{role}.bin",
                **_file(path, contents[role]),
                "role": role,
            }
        )
    gate = canonical_json_bytes(
        {
            "artifact_build_verified": True,
            "external_publication_completed": False,
            "manifest_verified": True,
            "producer_commit": COMMIT,
            "schema_version": PRODUCER_GATE_SCHEMA_VERSION,
            "statement_pre_sign_verified": True,
            "verify_before_publish": True,
            "zero_rewrite_characterization_sha256": "1" * 64,
            "zero_rewrite_semantic_diff_sha256": "2" * 64,
            "zero_rewrite_verified": True,
        }
    )
    (root / "producer-gates-v1.json").write_bytes(gate)
    (root / BUILD_INPUT_FILENAME).write_bytes(
        canonical_json_bytes(
            {
                "discovery_tag": "trend-supertrend-0.1.0",
                "producer_commit": COMMIT,
                "producer_gate_receipt": _file("producer-gates-v1.json", gate),
                "producer_repository": producer,
                "release_layers": layers,
                "repository": f"ghcr.io/{producer}/strategy-releases",
                "schema_version": BUILD_INPUT_SCHEMA_VERSION,
                "strategy_coordinate": f"strategy://github.com/{producer}/trend/supertrend@0.1.0",
            }
        )
    )


def _finalize(tmp_path: Path, **build: object) -> Path:
    first, second, output = tmp_path / "first", tmp_path / "second", tmp_path / "candidate"
    _write_build(first, **build)  # type: ignore[arg-type]
    _write_build(second, **build)  # type: ignore[arg-type]
    digest = finalize_unsigned_candidate(
        first,
        second,
        output,
        repo_root=tmp_path / "empty-repo",
        producer_commit=COMMIT,
        producer_repository=PRODUCER,
        repository=REPOSITORY,
        workflow_ref=CALLER_WORKFLOW_REF,
        run_id=RUN_ID,
        run_attempt=1,
        artifact_name=ARTIFACT_NAME,
    )
    assert (output / UNSIGNED_CANDIDATE_SIDECAR).read_text().startswith(digest)
    return output


def _certificate(identity: str, repository: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sigstore.dev")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity)]),
            critical=True,
        )
        .add_extension(
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier(GITHUB_WORKFLOW_REPOSITORY_OID), repository.encode()
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    from cryptography.hazmat.primitives.serialization import Encoding

    return certificate.public_bytes(Encoding.DER)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class FakeSigningBackend:
    """Returns a structurally complete bundle whose certificate names what it is told."""

    def __init__(
        self,
        identity: str = PUBLISHER_IDENTITY,
        repository: str = PRODUCER,
        subject: str = SUBJECT,
    ) -> None:
        self._certificate = _certificate(identity, repository)
        self._subject = subject

    def sign_dsse(self, statement_bytes: bytes) -> SigningResult:
        bundle = {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": {
                "certificate": {"rawBytes": _b64(self._certificate)},
                "tlogEntries": [
                    {
                        "canonicalizedBody": _b64(b"{}"),
                        "inclusionPromise": {"signedEntryTimestamp": _b64(b"signed-entry")},
                        "inclusionProof": {
                            "checkpoint": {"envelope": "signed checkpoint"},
                            "hashes": [],
                            "logIndex": "1",
                            "rootHash": _b64(b"root"),
                            "treeSize": "1",
                        },
                        "integratedTime": "1",
                        "kindVersion": {"kind": "dsse", "version": "0.0.1"},
                        "logId": {"keyId": _b64(b"log-id")},
                        "logIndex": "1",
                    }
                ],
            },
            "dsseEnvelope": {
                "payload": _b64(statement_bytes),
                "payloadType": "application/vnd.in-toto+json",
                "signatures": [{"sig": _b64(b"signature")}],
            },
        }
        trusted_root = {
            "mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1",
            "certificateAuthorities": [{"test": True}],
            "ctlogs": [{"test": True}],
            "tlogs": [{"test": True}],
        }
        return SigningResult(
            bundle_json=json.dumps(bundle, separators=(",", ":")),
            trusted_root_json=json.dumps(trusted_root, separators=(",", ":")),
            certificate_identity=self._subject,
            oidc_issuer=GITHUB_OIDC_ISSUER,
            library_version="4.4.0",
        )


def _workflow(**overrides: object) -> PublicationWorkflowIdentityV1:
    values: dict[str, object] = {
        "workflow_identity": PUBLISHER_IDENTITY,
        "workflow_ref": PUBLISHER_REF,
        "workflow_run_id": RUN_ID,
        "workflow_run_attempt": 1,
        "source_ref": "refs/heads/main",
        "oidc_issuer": GITHUB_OIDC_ISSUER,
        "oidc_subject": SUBJECT,
        "oidc_audience": GITHUB_OIDC_AUDIENCE,
    }
    values.update(overrides)
    return PublicationWorkflowIdentityV1(**values)  # type: ignore[arg-type]


def _assemble(candidate: Path, output: Path, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "repo_root": output.parent / "empty-repo",
        "producer_commit": COMMIT,
        "producer_repository": PRODUCER,
        "repository": REPOSITORY,
        "candidate_workflow_ref": CALLER_WORKFLOW_REF,
        "workflow": _workflow(),
        "candidate_run_id": RUN_ID,
        "candidate_artifact_name": ARTIFACT_NAME,
        "candidate_artifact_digest": "sha256:" + "f" * 64,
        "backend": FakeSigningBackend(),
    }
    arguments.update(overrides)
    return assemble_publication_input(
        candidate / UNSIGNED_CANDIDATE_FILENAME,
        output,
        **arguments,  # type: ignore[arg-type]
    )


def test_identical_builds_freeze_and_assembly_signs_the_exact_statement(tmp_path: Path) -> None:
    candidate = _finalize(tmp_path)
    unsigned = load_unsigned_candidate(
        candidate / UNSIGNED_CANDIDATE_FILENAME,
        repo_root=tmp_path / "empty-repo",
        expected_producer_commit=COMMIT,
        expected_producer_repository=PRODUCER,
        expected_repository=REPOSITORY,
        expected_workflow_ref=CALLER_WORKFLOW_REF,
        expected_run_id=RUN_ID,
        expected_artifact_name=ARTIFACT_NAME,
    )
    output = tmp_path / "assembled"
    assembled = _assemble(candidate, output)

    assert assembled.release_layers == unsigned.release_layers
    assert assembled.repository == REPOSITORY
    assert assembled.attestation_ref.statement_coordinate.startswith(f"oci://{REPOSITORY}/")
    loaded = load_candidate_v1(
        output / PUBLISH_INPUT_FILENAME,
        PublicationContextV1(
            producer_repository=PRODUCER, producer_commit=COMMIT, workflow=_workflow()
        ),
        repository=REPOSITORY,
    )
    assert loaded.release_layers == unsigned.release_layers


def test_a_build_made_for_another_producer_is_not_frozen(tmp_path: Path) -> None:
    with pytest.raises(UnsignedCandidateError, match="another producer"):
        _finalize(tmp_path, producer="someone-else/strategies")


def test_candidates_refuse_drift_extra_files_fixtures_and_overwrite(tmp_path: Path) -> None:
    def finalize(first: Path, second: Path, output: Path) -> None:
        finalize_unsigned_candidate(
            first,
            second,
            output,
            repo_root=tmp_path / "empty-repo",
            producer_commit=COMMIT,
            producer_repository=PRODUCER,
            repository=REPOSITORY,
            workflow_ref=CALLER_WORKFLOW_REF,
            run_id=RUN_ID,
            run_attempt=1,
            artifact_name=ARTIFACT_NAME,
        )

    _write_build(tmp_path / "a")
    _write_build(tmp_path / "b")
    (tmp_path / "b/layers/strategy_artifact.bin").write_bytes(b"drift")
    with pytest.raises(UnsignedCandidateError, match="differ byte-for-byte"):
        finalize(tmp_path / "a", tmp_path / "b", tmp_path / "out-a")

    _write_build(tmp_path / "c")
    _write_build(tmp_path / "d")
    for name in ("c", "d"):
        (tmp_path / name / "unreferenced").write_text("x")
    with pytest.raises(UnsignedCandidateError, match="unreferenced"):
        finalize(tmp_path / "c", tmp_path / "d", tmp_path / "out-b")

    _write_build(tmp_path / "e", fixture_marker=True)
    _write_build(tmp_path / "f", fixture_marker=True)
    with pytest.raises(UnsignedCandidateError, match="fixture"):
        finalize(tmp_path / "e", tmp_path / "f", tmp_path / "out-c")

    candidate = _finalize(tmp_path / "again")
    with pytest.raises(UnsignedCandidateError, match="must not already exist"):
        finalize(tmp_path / "again/first", tmp_path / "again/second", candidate)


def test_a_workflow_ref_that_is_not_one_is_refused(tmp_path: Path) -> None:
    _write_build(tmp_path / "a")
    _write_build(tmp_path / "b")
    with pytest.raises(UnsignedCandidateError, match="not a GitHub workflow ref"):
        finalize_unsigned_candidate(
            tmp_path / "a",
            tmp_path / "b",
            tmp_path / "out",
            repo_root=tmp_path / "empty-repo",
            producer_commit=COMMIT,
            producer_repository=PRODUCER,
            repository=REPOSITORY,
            workflow_ref="release.yml",
            run_id=RUN_ID,
            run_attempt=1,
            artifact_name=ARTIFACT_NAME,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_run_id": RUN_ID + 1}, "workflow provenance"),
        ({"expected_workflow_ref": f"{PRODUCER}/.github/workflows/other.yml@refs/heads/main"},
         "workflow provenance"),
        ({"expected_producer_repository": "someone-else/strategies"}, "authority differs"),
        ({"expected_repository": "ghcr.io/someone-else/strategies/x"}, "authority differs"),
    ],
)  # fmt: skip
def test_a_candidate_from_another_run_or_producer_is_not_loaded(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    candidate = _finalize(tmp_path)
    expected: dict[str, object] = {
        "repo_root": tmp_path / "empty-repo",
        "expected_producer_commit": COMMIT,
        "expected_producer_repository": PRODUCER,
        "expected_repository": REPOSITORY,
        "expected_workflow_ref": CALLER_WORKFLOW_REF,
        "expected_run_id": RUN_ID,
        "expected_artifact_name": ARTIFACT_NAME,
    }
    expected.update(overrides)
    with pytest.raises(UnsignedCandidateError, match=message):
        load_unsigned_candidate(candidate / UNSIGNED_CANDIDATE_FILENAME, **expected)  # type: ignore[arg-type]


def test_a_tampered_sidecar_is_refused(tmp_path: Path) -> None:
    candidate = _finalize(tmp_path)
    (candidate / UNSIGNED_CANDIDATE_SIDECAR).write_text("0" * 64)
    with pytest.raises(UnsignedCandidateError, match="sidecar"):
        load_unsigned_candidate(
            candidate / UNSIGNED_CANDIDATE_FILENAME,
            repo_root=tmp_path / "empty-repo",
            expected_producer_commit=COMMIT,
            expected_producer_repository=PRODUCER,
            expected_repository=REPOSITORY,
            expected_workflow_ref=CALLER_WORKFLOW_REF,
            expected_run_id=RUN_ID,
            expected_artifact_name=ARTIFACT_NAME,
        )


def test_a_statement_built_for_another_workflow_is_not_signed(tmp_path: Path) -> None:
    other = "https://github.com/the-alephain-guild/custos/.github/workflows/x.yml@refs/tags/v1"
    candidate = _finalize(tmp_path, workflow_identity=other)

    with pytest.raises(UnsignedCandidateError, match="the statement was built for"):
        _assemble(candidate, tmp_path / "assembled")
    assert not (tmp_path / "assembled").exists()


def test_an_immutable_subject_for_the_producer_signs(tmp_path: Path) -> None:
    candidate = _finalize(tmp_path)
    owner, name = PRODUCER.split("/")
    subject = f"repo:{owner}@90892465/{name}@1385027778:ref:refs/heads/main"

    assembled = _assemble(
        candidate,
        tmp_path / "assembled",
        workflow=_workflow(oidc_subject=subject),
        backend=FakeSigningBackend(subject=subject),
    )

    assert assembled.workflow.oidc_subject == subject


def test_a_job_whose_subject_names_another_repository_does_not_sign(tmp_path: Path) -> None:
    candidate = _finalize(tmp_path)
    workflow = _workflow(oidc_subject="repo:someone-else/strategies:ref:refs/heads/main")

    with pytest.raises(UnsignedCandidateError, match="names another repository"):
        _assemble(candidate, tmp_path / "assembled", workflow=workflow)


@pytest.mark.parametrize(
    ("identity", "repository"),
    [
        ("https://github.com/someone/else/.github/workflows/x.yml@refs/heads/main", PRODUCER),
        (PUBLISHER_IDENTITY, "someone-else/strategies"),
    ],
)
def test_a_certificate_naming_anything_else_stops_the_release(
    tmp_path: Path, identity: str, repository: str
) -> None:
    candidate = _finalize(tmp_path)

    with pytest.raises(UnsignedCandidateError, match="the certificate names"):
        _assemble(
            candidate,
            tmp_path / "assembled",
            backend=FakeSigningBackend(identity=identity, repository=repository),
        )
    assert not (tmp_path / "assembled").exists()


def _token(claims: dict[str, object]) -> str:
    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


CLAIMS: dict[str, object] = {
    "iss": GITHUB_OIDC_ISSUER,
    "sub": SUBJECT,
    "repository": PRODUCER,
    "ref": "refs/heads/main",
    "job_workflow_ref": PUBLISHER_REF,
    "run_id": str(RUN_ID),
    "run_attempt": "1",
}


def test_the_signing_identity_is_the_reusable_workflow_not_the_callers() -> None:
    claims = read_workflow_claims(_token(CLAIMS))

    assert claims.workflow_identity == PUBLISHER_IDENTITY
    assert claims.publication_identity() == _workflow()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iss": "https://issuer.example"}, "not GitHub Actions"),
        ({"job_workflow_ref": "release.yml"}, "not a workflow ref"),
        ({"run_id": "0"}, "run_id"),
        ({"repository": ""}, "repository"),
    ],
)
def test_tokens_without_a_usable_identity_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(GitHubOidcError, match=message):
        read_workflow_claims(_token({**CLAIMS, **overrides}))


def test_publishing_outside_github_actions_is_refused() -> None:
    with pytest.raises(ValueError, match="GitHub Actions only"):
        PublicationContextV1.from_environment({})

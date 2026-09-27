from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from custos_strategy_publisher.sigstore_signing import (
    BUNDLE_FILENAME,
    PROVENANCE_FILENAME,
    TRUSTED_ROOT_FILENAME,
    SigningPreparationError,
    SigningResult,
    prepare_signing_outputs,
    validate_bundle_json,
)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _bundle(statement: bytes) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {
            "certificate": {"rawBytes": _b64(b"test certificate")},
            "tlogEntries": [
                {
                    "logIndex": "1",
                    "logId": {"keyId": _b64(b"test log id")},
                    "kindVersion": {"kind": "dsse", "version": "0.0.1"},
                    "integratedTime": "1",
                    "inclusionPromise": {"signedEntryTimestamp": _b64(b"test SET")},
                    "inclusionProof": {
                        "logIndex": "1",
                        "rootHash": _b64(b"test root hash"),
                        "treeSize": "1",
                        "hashes": [],
                        "checkpoint": {"envelope": "signed checkpoint"},
                    },
                    "canonicalizedBody": _b64(b"{}"),
                }
            ],
        },
        "dsseEnvelope": {
            "payload": _b64(statement),
            "payloadType": "application/vnd.in-toto+json",
            "signatures": [{"sig": _b64(b"test signature")}],
        },
    }


def _trusted_root() -> dict[str, object]:
    return {
        "mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1",
        "tlogs": [{"test": True}],
        "ctlogs": [{"test": True}],
        "certificateAuthorities": [{"test": True}],
    }


class FakeSigningBackend:
    def __init__(self, bundle: dict[str, object]) -> None:
        self.bundle = bundle
        self.received: bytes | None = None

    def sign_dsse(self, statement_bytes: bytes) -> SigningResult:
        self.received = statement_bytes
        return SigningResult(
            bundle_json=json.dumps(self.bundle, separators=(",", ":")),
            trusted_root_json=json.dumps(_trusted_root(), separators=(",", ":")),
            certificate_identity="workflow@example.invalid",
            oidc_issuer="https://token.actions.githubusercontent.com",
            library_version="4.4.0",
        )


def _fixture_repo(tmp_path: Path, statement: bytes) -> Path:
    repo = tmp_path / "repo"
    authority = repo / "docs" / "authority"
    authority.mkdir(parents=True)
    statement_path = authority / "strategy-release-statement-v1.golden.json"
    statement_path.write_bytes(statement)
    statement_path.with_suffix(".json.sha256").write_text(
        f"{hashlib.sha256(statement).hexdigest()}  {statement_path.name}\n",
        encoding="ascii",
    )
    return repo


def test_mock_boundary_preserves_exact_statement_and_writes_only_validated_outputs(
    tmp_path: Path,
) -> None:
    statement = (
        b'{"_type":"https://in-toto.io/Statement/v1","subject":[{"name":"a","digest":{"sha256":"'
        + b"0" * 64
        + b'"}}],"predicateType":"test","predicate":{}}'
    )
    repo = _fixture_repo(tmp_path, statement)
    output_dir = tmp_path / "workflow-output"
    backend = FakeSigningBackend(_bundle(statement))

    provenance = prepare_signing_outputs(
        repo,
        output_dir,
        backend=backend,
        environ={"GITHUB_SHA": "abc123", "GITHUB_RUN_ID": "42"},
    )

    assert backend.received == statement
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(
        [BUNDLE_FILENAME, TRUSTED_ROOT_FILENAME, PROVENANCE_FILENAME]
    )
    assert provenance["test_only"] is True
    assert provenance["non_production"] is True
    assert provenance["production_use"] == "forbidden"
    assert provenance["workflow"] == {"github_run_id": "42", "github_sha": "abc123"}
    assert (
        provenance["bundle"]["sha256"]
        == hashlib.sha256((output_dir / BUNDLE_FILENAME).read_bytes()).hexdigest()
    )
    assert (
        provenance["trusted_root"]["sha256"]
        == hashlib.sha256((output_dir / TRUSTED_ROOT_FILENAME).read_bytes()).hexdigest()
    )


def test_payload_mismatch_fails_before_creating_output_directory(tmp_path: Path) -> None:
    statement = b'{"_type":"https://in-toto.io/Statement/v1","subject":[],"predicateType":"test"}'
    repo = _fixture_repo(tmp_path, statement)
    output_dir = tmp_path / "workflow-output"
    backend = FakeSigningBackend(_bundle(b"different bytes"))

    with pytest.raises(SigningPreparationError, match="differs byte-for-byte"):
        prepare_signing_outputs(repo, output_dir, backend=backend, environ={})

    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("field", "expected_error"),
    [
        ("inclusionPromise", "inclusion promise"),
        ("inclusionProof", "inclusion proof"),
        ("checkpoint", "checkpoint"),
        ("certificate", "signing certificate"),
    ],
)
def test_bundle_requires_set_proof_checkpoint_and_certificate(
    field: str,
    expected_error: str,
) -> None:
    statement = b"statement"
    bundle = copy.deepcopy(_bundle(statement))
    material = bundle["verificationMaterial"]
    assert isinstance(material, dict)
    entries = material["tlogEntries"]
    assert isinstance(entries, list)
    entry = entries[0]
    assert isinstance(entry, dict)
    if field == "checkpoint":
        proof = entry["inclusionProof"]
        assert isinstance(proof, dict)
        proof.pop("checkpoint")
    elif field == "certificate":
        material.pop("certificate")
    else:
        entry.pop(field)

    with pytest.raises(SigningPreparationError, match=expected_error):
        validate_bundle_json(json.dumps(bundle), statement)


def test_signing_outputs_cannot_be_written_inside_repository(tmp_path: Path) -> None:
    statement = b"statement"
    repo = _fixture_repo(tmp_path, statement)

    with pytest.raises(SigningPreparationError, match="outside the repository"):
        prepare_signing_outputs(
            repo,
            repo / "generated",
            backend=FakeSigningBackend(_bundle(statement)),
            environ={},
        )

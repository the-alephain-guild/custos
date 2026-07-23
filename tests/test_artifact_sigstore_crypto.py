from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest

from custos.artifacts.errors import ArtifactVerificationError
from custos.artifacts.sigstore_verifier import ProductionSigstoreVerifier

pytest.importorskip("sigstore")
pytest.importorskip("sigstore_protobuf_specs")

from tests.sigstore_crypto_fixture import (
    DIFFERENT_ISSUER,
    DIFFERENT_SUBJECT_SHA256,
    DIFFERENT_WORKFLOW_IDENTITY,
    build_offline_sigstore_fixture,
    tamper_bundle,
)


def _forbid_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("offline Sigstore verification attempted network access")


def test_real_crypto_bundle_verifies_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_offline_sigstore_fixture(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", _forbid_network)

    evidence = ProductionSigstoreVerifier().verify(fixture.request)

    assert evidence.transparency_log_verified is True
    assert evidence.issuer == fixture.identity.issuer
    assert evidence.workflow_identity == fixture.identity.workflow_identity
    assert evidence.source_repository == fixture.identity.source_repository
    assert evidence.verified_subjects == fixture.required_subjects


def test_real_crypto_rejects_command_bound_artifact_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(
        tmp_path,
        statement_sha256=DIFFERENT_SUBJECT_SHA256,
    )

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(fixture.request)


def test_real_crypto_rejects_dsse_signature_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(tmp_path)
    tamper_bundle(fixture.bundle_path, "signature")

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(fixture.request)


def test_real_crypto_rejects_certificate_identity_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(
        tmp_path,
        certificate_identity=DIFFERENT_WORKFLOW_IDENTITY,
    )

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(fixture.request)


def test_real_crypto_rejects_certificate_issuer_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(
        tmp_path,
        certificate_issuer=DIFFERENT_ISSUER,
    )

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(fixture.request)


def test_real_crypto_rejects_rekor_set_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(tmp_path)
    tamper_bundle(fixture.bundle_path, "rekor_set")

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(fixture.request)


def test_real_crypto_rejects_different_valid_trusted_root(
    tmp_path: Path,
) -> None:
    fixture = build_offline_sigstore_fixture(tmp_path / "first")
    other = build_offline_sigstore_fixture(tmp_path / "second")
    request = replace(
        fixture.request,
        trusted_root_bytes=other.request.trusted_root_bytes,
        quarantine_parent=tmp_path / "quarantine",
    )

    with pytest.raises(ArtifactVerificationError):
        ProductionSigstoreVerifier().verify(request)

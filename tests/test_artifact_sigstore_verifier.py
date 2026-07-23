from __future__ import annotations

import builtins
import hashlib
import inspect
import json
import socket
from pathlib import Path

import pytest

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import SigstoreIdentityV1
from custos.artifacts.sigstore_verifier import (
    ProductionSigstoreVerifier,
    _parse_in_toto_subjects,
)
from custos.artifacts.verification_types import DigestSubject, SigstoreVerificationRequest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "artifact_verifier"
TRUSTED_ROOT = FIXTURE_ROOT / "sigstore-production-trusted-root.json"


def _request(tmp_path: Path, *, bundle_bytes: bytes = b"{}") -> SigstoreVerificationRequest:
    bundle = tmp_path / "attestation.sigstore.json"
    bundle.write_bytes(bundle_bytes)
    return SigstoreVerificationRequest(
        bundle_path=bundle,
        trusted_root_bytes=TRUSTED_ROOT.read_bytes(),
        accepted_identities=(
            SigstoreIdentityV1(
                issuer="https://token.actions.githubusercontent.com",
                workflow_identity=(
                    "https://github.com/alchymia-labs/philosophers-stone/"
                    ".github/workflows/release-strategy.yml@refs/heads/main"
                ),
                source_repository="https://github.com/alchymia-labs/philosophers-stone",
            ),
        ),
        required_subjects=(
            DigestSubject("strategy_release_bom", "1" * 64),
            DigestSubject("supertrend.whl", "2" * 64),
            DigestSubject("strategy-manifest-v1.json", "3" * 64),
        ),
        quarantine_parent=tmp_path / "quarantine",
    )


def test_sigstore_dependency_missing_fails_closed_with_typed_code(tmp_path, monkeypatch) -> None:
    request = _request(tmp_path)
    real_import = builtins.__import__

    def deny_sigstore(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sigstore" or name.startswith("sigstore."):
            raise ImportError("sigstore deliberately unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", deny_sigstore)
    with pytest.raises(ArtifactVerificationError) as error:
        ProductionSigstoreVerifier().verify(request)

    assert error.value.code is ArtifactVerificationCode.SIGSTORE_VERIFIER_UNAVAILABLE


def test_real_trusted_root_and_invalid_bundle_reject_offline_without_network(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("sigstore")
    assert TRUSTED_ROOT.is_file()
    assert (
        hashlib.sha256(TRUSTED_ROOT.read_bytes()).hexdigest()
        == (FIXTURE_ROOT / "sigstore-production-trusted-root.json.sha256")
        .read_text(encoding="ascii")
        .strip()
    )

    def forbid_network(*args, **kwargs):
        raise AssertionError("offline verifier attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    with pytest.raises(ArtifactVerificationError) as error:
        ProductionSigstoreVerifier().verify(_request(tmp_path))

    assert error.value.code is ArtifactVerificationCode.SIGSTORE_VERIFICATION_FAILED


def test_in_toto_statement_requires_exact_sha256_subject_set() -> None:
    payload = json.dumps(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {"name": "strategy_release_bom", "digest": {"sha256": "1" * 64}},
                {"name": "supertrend.whl", "digest": {"sha256": "2" * 64}},
            ],
            "predicateType": "https://the-alephain-guild.dev/strategy-release/v1",
            "predicate": {},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    assert _parse_in_toto_subjects(payload) == (
        DigestSubject("strategy_release_bom", "1" * 64),
        DigestSubject("supertrend.whl", "2" * 64),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"_type":"https://in-toto.io/Statement/v1","subject":[]}',
        b'{"_type":"wrong","subject":[{"name":"x","digest":{"sha256":"' + b"1" * 64 + b'"}}]}',
        b'{"_type":"https://in-toto.io/Statement/v1","subject":[{"name":"x","digest":{"sha256":"bad"}}]}',
        b'{"_type":"https://in-toto.io/Statement/v1","subject":[{"name":"x","digest":{"sha256":"'
        + b"1" * 64
        + b'"}},{"name":"x","digest":{"sha256":"'
        + b"1" * 64
        + b'"}}]}',
        b'{"_type":"https://in-toto.io/Statement/v1","_type":"https://in-toto.io/Statement/v1","subject":[{"name":"x","digest":{"sha256":"'
        + b"1" * 64
        + b'"}}]}',
    ],
)
def test_in_toto_statement_rejects_ambiguous_or_invalid_subjects(payload: bytes) -> None:
    with pytest.raises(ArtifactVerificationError) as error:
        _parse_in_toto_subjects(payload)

    assert error.value.code is ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH


def test_adapter_source_has_no_network_or_default_trust_fallback() -> None:
    source = inspect.getsource(__import__("custos.artifacts.sigstore_verifier", fromlist=["*"]))

    assert "Verifier.production" not in source
    assert "TrustedRoot.production" not in source
    assert "TrustedRoot.from_tuf" not in source
    assert "requests." not in source
    assert "urlopen" not in source
    assert ".verify_dsse(" in source
    assert "TrustedRoot.from_file" in source

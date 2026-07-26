from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import SigstoreIdentityV1
from custos.artifacts.runtime import (
    ArtifactRuntimeCapabilityV1,
    _validate_sigstore_against_crucible,
    verify_execution_member_files,
)
from custos.artifacts.verification_types import (
    DigestSubject,
    SigstoreVerificationEvidence,
    SigstoreVerificationRequest,
)


def test_crucible_sigstore_evidence_uses_exact_certificate_identity(
    tmp_path: Path,
) -> None:
    issuer = "https://token.actions.githubusercontent.com"
    workflow_identity = (
        "https://github.com/alchymia-labs/philosophers-stone/"
        ".github/workflows/publish-strategy-artifact.yml@refs/heads/main"
    )
    source_repository = "https://github.com/alchymia-labs/philosophers-stone"
    trusted_root = b'{"trusted":"root"}'
    bundle = tmp_path / "release.sigstore.json"
    bundle.write_bytes(b"bundle")
    bundle_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    identity = SigstoreIdentityV1(
        issuer=issuer,
        workflow_identity=workflow_identity,
        source_repository=source_repository,
    )
    subjects = (DigestSubject(name="strategy-artifact", sha256="a" * 64),)
    request = SigstoreVerificationRequest(
        bundle_path=bundle,
        trusted_root_bytes=trusted_root,
        accepted_identities=(identity,),
        required_subjects=subjects,
        quarantine_parent=tmp_path,
    )
    evidence = SigstoreVerificationEvidence(
        verifier_capability_id="offline-sigstore-v1",
        bundle_sha256=bundle_digest,
        trusted_root_sha256=hashlib.sha256(trusted_root).hexdigest(),
        issuer=issuer,
        workflow_identity=workflow_identity,
        source_repository=source_repository,
        verified_subjects=subjects,
        transparency_log_verified=True,
    )
    authority = SimpleNamespace(
        detached_attestation_ref={"bundle_sha256": bundle_digest},
        crucible_artifact_evidence={
            "signed_producer_claims": {
                "workflow_identity": workflow_identity,
                "producer_repository": source_repository,
            },
            "sigstore_proof": {
                "bundle_sha256": bundle_digest,
                "certificate_issuer": issuer,
                "certificate_subject": workflow_identity,
            },
        },
    )

    _validate_sigstore_against_crucible(evidence, request, authority)


def test_artifact_runtime_capability_has_one_v1_shape() -> None:
    blocked = ArtifactRuntimeCapabilityV1.blocked("StrategyRelease resolver is not composed")
    ready = ArtifactRuntimeCapabilityV1.production_ready()

    assert blocked.ready is False
    assert blocked.blocked_reason == "StrategyRelease resolver is not composed"
    assert ready.ready is True
    assert ready.blocked_reason is None


def test_execution_member_verifier_binds_exact_strategy_bytes(tmp_path: Path) -> None:
    wheel = tmp_path / "strategy.whl"
    wheel.write_bytes(b"verified-wheel")
    digest = hashlib.sha256(b"verified-wheel").hexdigest()
    release_bom = {
        "members": [
            {
                "role": "strategy_wheel",
                "name": "strategy.whl",
                "media_type": "application/zip",
                "size_bytes": len(b"verified-wheel"),
                "sha256": digest,
            }
        ]
    }

    verified = verify_execution_member_files(
        release_bom,
        {"strategy.whl": wheel},
    )

    assert len(verified) == 1
    assert verified[0].sha256 == digest
    assert verified[0].path == wheel


def test_execution_member_verifier_rejects_unlisted_member(tmp_path: Path) -> None:
    wheel = tmp_path / "strategy.whl"
    wheel.write_bytes(b"verified-wheel")
    release_bom = {
        "members": [
            {
                "role": "strategy_wheel",
                "name": "strategy.whl",
                "media_type": "application/zip",
                "size_bytes": len(b"verified-wheel"),
                "sha256": hashlib.sha256(b"verified-wheel").hexdigest(),
            }
        ]
    }

    with pytest.raises(
        ArtifactVerificationError,
        match="member paths must exactly match",
    ) as captured:
        verify_execution_member_files(
            release_bom,
            {
                "strategy.whl": wheel,
                "unlisted.py": tmp_path / "unlisted.py",
            },
        )

    assert captured.value.code is ArtifactVerificationCode.MEMBER_SET_MISMATCH


def _single_member_bom(path: Path, payload: bytes) -> dict[str, object]:
    return {
        "members": [
            {
                "role": "strategy_wheel",
                "name": path.name,
                "media_type": "application/zip",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
    }


@pytest.mark.parametrize("case", ["missing", "drift", "directory", "symlink"])
def test_execution_member_verifier_rejects_non_exact_member_files(
    tmp_path: Path,
    case: str,
) -> None:
    payload = b"verified-wheel"
    member = tmp_path / "strategy.whl"
    member.write_bytes(payload)
    release_bom = _single_member_bom(member, payload)
    member_paths: dict[str, Path] = {member.name: member}
    if case == "missing":
        member_paths = {}
    elif case == "drift":
        member.write_bytes(payload + b"-drift")
    elif case == "directory":
        member.unlink()
        member.mkdir()
    else:
        target = tmp_path / "target.whl"
        target.write_bytes(payload)
        member.unlink()
        os.symlink(target, member)

    with pytest.raises(ArtifactVerificationError) as captured:
        verify_execution_member_files(release_bom, member_paths)

    assert captured.value.code in {
        ArtifactVerificationCode.MEMBER_SET_MISMATCH,
        ArtifactVerificationCode.MEMBER_UNSTABLE,
    }


def test_execution_member_verifier_rejects_duplicate_strategy_identity(tmp_path: Path) -> None:
    member = tmp_path / "strategy.whl"
    payload = b"verified-wheel"
    member.write_bytes(payload)
    release_bom = _single_member_bom(member, payload)
    release_bom["members"] = [
        release_bom["members"][0],
        dict(release_bom["members"][0]),
    ]

    with pytest.raises(ArtifactVerificationError) as captured:
        verify_execution_member_files(release_bom, {member.name: member})

    assert captured.value.code is ArtifactVerificationCode.MEMBER_SET_MISMATCH

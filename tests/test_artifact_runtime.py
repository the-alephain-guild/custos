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


@pytest.mark.parametrize(
    ("proof_shape", "accepted"),
    [("legacy", True), ("github_oidc", True), ("team_kms", False)],
)
def test_crucible_sigstore_evidence_uses_exact_certificate_identity(
    tmp_path: Path,
    proof_shape: str,
    accepted: bool,
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
    sigstore_proof = {
        "bundle_sha256": bundle_digest,
        "certificate_issuer": issuer,
        "certificate_subject": workflow_identity,
    }
    if proof_shape != "legacy":
        sigstore_proof = {
            "publisher_profile": proof_shape,
            "proof": sigstore_proof,
        }
    authority = SimpleNamespace(
        detached_attestation_ref={"bundle_sha256": bundle_digest},
        crucible_artifact_evidence={
            "signed_producer_claims": {
                "workflow_identity": workflow_identity,
                "producer_repository": source_repository,
            },
            "sigstore_proof": sigstore_proof,
        },
    )

    if accepted:
        _validate_sigstore_against_crucible(evidence, request, authority)
    else:
        with pytest.raises(ArtifactVerificationError) as error:
            _validate_sigstore_against_crucible(evidence, request, authority)
        assert error.value.code is ArtifactVerificationCode.SIGSTORE_EVIDENCE_MISMATCH


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


async def _activate(store, verified, tmp_path: Path, *, activation_id: str, build) -> object:
    """Activate one verified package through the durable activator and real loader."""
    from custos_toolkit.contracts.strategy_execution import deep_freeze_json

    from custos.artifacts.activation import (
        ArtifactActivationCandidateV1,
        DurableArtifactActivatorV1,
    )
    from custos.engines.nautilus.runtime_loader import NautilusRuntimeEntryPointLoaderV1
    from tests.engines.nautilus.test_runtime_entry_point_loader_v1 import _context

    quarantine_root = tmp_path / f"quarantine-{activation_id}"
    quarantine_root.mkdir()
    entry_point = build(quarantine_root)
    activator = DurableArtifactActivatorV1(state=store, activation_parent=tmp_path / "activations")
    digest = hashlib.sha256(activation_id.encode()).hexdigest()
    return await activator.activate(
        ArtifactActivationCandidateV1(
            command=verified.command,
            activation_id=activation_id,
            quarantine_root=quarantine_root,
            entry_point=entry_point,
            effective_config=deep_freeze_json({}),
            execution_context=_context(),
            artifact_identity_digest=digest,
            artifact_authority_digest=digest,
        ),
        loader=NautilusRuntimeEntryPointLoaderV1(),
    )


async def _activation_state(store, verified, activation_id: str):
    digest = hashlib.sha256(activation_id.encode()).hexdigest()
    return await store.load_artifact_activation(
        command=verified.command,
        activation_id=activation_id,
        artifact_identity_digest=digest,
        artifact_authority_digest=digest,
    )


@pytest.mark.asyncio
async def test_an_import_error_inside_the_activation_is_still_quarantined(tmp_path: Path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    _outbox, store = _runner_fact_store(tmp_path / "runner-state.sqlite3")
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )

    def broken_package(root: Path) -> str:
        package = root / "strategy_broken_import"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "runtime.py").write_text(
            "import strategy_broken_import.missing_member\n", encoding="utf-8"
        )
        return "strategy_broken_import.runtime:Runtime"

    with pytest.raises(RuntimeError, match="verified entry point failed"):
        await _activate(store, verified, tmp_path, activation_id="broken", build=broken_package)

    assert await _activation_state(store, verified, "broken") == {
        "state": "quarantined",
        "quarantine_reason": "verified_entry_point_load_failed",
    }


@pytest.mark.asyncio
async def test_two_sources_of_one_strategy_name_both_activate_without_quarantine(
    tmp_path: Path,
) -> None:
    pytest.importorskip("custos_toolkit_nautilus")
    from tests.engines.nautilus.test_runtime_entry_point_loader_v1 import (
        _forget_same_name,
        _write_activation,
    )
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    _outbox, store = _runner_fact_store(tmp_path / "runner-state.sqlite3")
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )

    def same_name(marker: str):
        def build(root: Path) -> str:
            # The helper creates its own directory; the quarantine root must exist
            # and hold the package, so build inside it.
            _write_activation(root / "unused", marker=marker)
            for child in (root / "unused").iterdir():
                child.rename(root / child.name)
            (root / "unused").rmdir()
            return "strategy_same_name.runtime:Runtime"

        return build

    try:
        first = await _activate(
            store, verified, tmp_path, activation_id="first", build=same_name("first")
        )
        second = await _activate(
            store, verified, tmp_path, activation_id="second", build=same_name("second")
        )

        assert first.create_strategy().marker == "first"
        assert second.create_strategy().marker == "second"
        # A restart of the first deployment rebuilds its own strategy afterwards.
        assert first.create_strategy().marker == "first"
        for activation_id in ("first", "second"):
            state = await _activation_state(store, verified, activation_id)
            assert state == {"state": "active", "quarantine_reason": None}
    finally:
        _forget_same_name()

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.runtime import (
    ArtifactRuntimeCapabilityV1,
    verify_execution_member_files,
)


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

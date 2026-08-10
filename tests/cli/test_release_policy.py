from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custos.artifacts.policy import verify_signed_release_policy
from custos.cli.subcommands import main

ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW = (
    "https://github.com/alchymia-labs/philosophers-stone/"
    ".github/workflows/publish-strategy-artifact.yml@refs/heads/main"
)
REPOSITORY = "https://github.com/alchymia-labs/philosophers-stone"


def _authority_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "authority" / "release-policy.key",
        tmp_path / "authority" / "release-policy.pub",
        tmp_path / "authority" / "authority-receipt.json",
    )


def _generate(tmp_path: Path) -> tuple[Path, Path, Path]:
    private_key, public_key, receipt = _authority_paths(tmp_path)
    assert (
        main(
            [
                "release-policy",
                "generate-development-authority",
                "--private-key-output",
                str(private_key),
                "--public-key-output",
                str(public_key),
                "--receipt-output",
                str(receipt),
            ]
        )
        == 0
    )
    return private_key, public_key, receipt


def _issue(tmp_path: Path, private_key: Path, public_key: Path, root: Path) -> list[str]:
    return [
        "release-policy",
        "issue",
        "--authority-private-key",
        str(private_key),
        "--authority-public-key",
        str(public_key),
        "--sigstore-trusted-root",
        str(root),
        "--policy-id",
        "v1-team-strategy-release",
        "--version",
        "1",
        "--not-before",
        "2026-08-10T00:00:00Z",
        "--expires-at",
        "2026-09-10T00:00:00Z",
        "--issuer",
        ISSUER,
        "--workflow-identity",
        WORKFLOW,
        "--source-repository",
        REPOSITORY,
        "--envelope-output",
        str(tmp_path / "issued" / "release-policy.json"),
        "--receipt-output",
        str(tmp_path / "issued" / "issuance-receipt.json"),
        "--environment-output",
        str(tmp_path / "issued" / "release-policy.env"),
    ]


def test_development_authority_is_explicit_and_private(tmp_path: Path) -> None:
    private_key, public_key, receipt_path = _generate(tmp_path)
    receipt = json.loads(receipt_path.read_text())

    assert receipt["production_approved"] is False
    assert receipt["status"] == "DEVELOPMENT_ONLY_NOT_PRODUCTION_APPROVED"
    assert receipt["key_id"].startswith("ed25519-")
    assert stat.S_IMODE(os.stat(private_key).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(public_key).st_mode) == 0o644


def test_issued_policy_verifies_and_environment_contains_no_private_key(tmp_path: Path) -> None:
    private_key, public_key, _ = _generate(tmp_path)
    root = tmp_path / "sigstore-trusted-root.json"
    root.write_bytes(b'{"trustedRoot":"fixture"}')

    assert main(_issue(tmp_path, private_key, public_key, root)) == 0

    envelope = tmp_path / "issued" / "release-policy.json"
    receipt = json.loads((tmp_path / "issued" / "issuance-receipt.json").read_text())
    public_bytes = base64.b64decode(public_key.read_text().strip(), validate=True)
    verified = verify_signed_release_policy(
        envelope.read_bytes(),
        authority_key_id=receipt["authority_key_id"],
        authority_public_key=Ed25519PublicKey.from_public_bytes(public_bytes),
        sigstore_trusted_root_bytes=root.read_bytes(),
        now=datetime(2026, 8, 10, 12, tzinfo=UTC),
    )
    environment = (tmp_path / "issued" / "release-policy.env").read_text()

    assert verified.policy.policy_id == "v1-team-strategy-release"
    assert verified.policy.accepted_identities[0].source_repository == REPOSITORY
    assert str(private_key) not in environment
    assert "CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE=" in environment
    assert "CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT=" in environment
    assert stat.S_IMODE(os.stat(tmp_path / "issued" / "release-policy.env").st_mode) == 0o600


def test_outputs_are_never_overwritten(tmp_path: Path) -> None:
    private_key, public_key, receipt = _generate(tmp_path)
    first_private = private_key.read_bytes()

    assert (
        main(
            [
                "release-policy",
                "generate-development-authority",
                "--private-key-output",
                str(private_key),
                "--public-key-output",
                str(public_key),
                "--receipt-output",
                str(receipt),
            ]
        )
        == 1
    )
    assert private_key.read_bytes() == first_private


def test_issue_rejects_a_mismatched_public_key(tmp_path: Path) -> None:
    private_key, _, _ = _generate(tmp_path / "first")
    _, other_public_key, _ = _generate(tmp_path / "second")
    root = tmp_path / "sigstore-trusted-root.json"
    root.write_bytes(b"trusted-root")

    assert main(_issue(tmp_path, private_key, other_public_key, root)) == 1
    assert not (tmp_path / "issued" / "release-policy.json").exists()

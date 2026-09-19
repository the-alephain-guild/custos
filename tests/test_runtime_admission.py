from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custos.core.runtime_admission import RuntimeAdmissionError, load_runtime_admission


@pytest.fixture(autouse=True)
def running_image_revision(tmp_path: Path, monkeypatch) -> Path:
    import custos.core.runtime_admission as module

    path = tmp_path / "source-revision"
    path.write_text("b" * 40 + "\n")
    monkeypatch.setattr(module, "RUNTIME_SOURCE_REVISION_FILE", path)
    return path


def promotion() -> dict:
    digest = "sha256:" + "a" * 64
    return {
        "schema_version": 1,
        "receipt_id": "CUSTOS-RUNTIME-CANDIDATE-PROMOTION-V1",
        "status": "PROMOTED_UNCHANGED",
        "candidate": {
            "repository": "ghcr.io/the-alephain-guild/custos",
            "digest": digest,
            "source_revision": "b" * 40,
            "platforms": ["linux/amd64", "linux/arm64"],
        },
        "inputs": {
            name: {"receipt_id": name, "owner": owner, "sha256": "c" * 64, "size_bytes": 123}
            for name, owner in (
                ("publication", "custos"),
                ("crucible_acceptance", "crucible-rust"),
                ("strategy_owner_acceptance", "philosophers-stone"),
            )
        },
        "workflow": {
            "repository": "the-alephain-guild/custos",
            "revision": "d" * 40,
            "run_id": 1,
            "run_attempt": 1,
            "workflow_file": ".github/workflows/promote-runtime-candidate.yml",
            "workflow_identity": "https://github.com/the-alephain-guild/custos/.github/workflows/promote-runtime-candidate.yml@refs/heads/main",
            "environment": "v1-team-runtime-promotion",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
        },
        "invariants": {
            "manifest_digest_before": digest,
            "manifest_digest_after": digest,
            "exact_digest_unchanged": True,
            "image_rebuilt": False,
            "tag_repointed": False,
            "registry_mutated": False,
        },
        "promoted_at": "2026-01-01T00:00:00Z",
        "artifact_runtime_ready": True,
        "system_production_ready": False,
    }


def inputs(tmp_path: Path, document: dict | None = None) -> SimpleNamespace:
    receipt = tmp_path / "promotion.json"
    receipt.write_text(json.dumps(document or promotion()))
    bundle = tmp_path / "promotion.sigstore.json"
    bundle.write_text("{}")
    root = tmp_path / "trusted-root.json"
    root.write_text("{}")
    return SimpleNamespace(
        runtime_promotion_receipt=receipt,
        runtime_promotion_bundle=bundle,
        runtime_sigstore_trusted_root=root,
        runtime_image_digest="sha256:" + "a" * 64,
        runtime_source_revision="b" * 40,
    )


def test_absent_runtime_receipt_keeps_live_disabled() -> None:
    assert load_runtime_admission(SimpleNamespace()) is None


def test_partial_configuration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeAdmissionError, match="complete"):
        load_runtime_admission(SimpleNamespace(runtime_promotion_receipt=tmp_path / "receipt"))


def test_wrong_running_image_cannot_claim_the_accepted_revision(
    tmp_path: Path, monkeypatch, running_image_revision: Path
) -> None:
    import custos.core.runtime_admission as module

    monkeypatch.setattr(module, "verify_sigstore_blob", lambda **kwargs: None)
    running_image_revision.write_text("e" * 40)
    with pytest.raises(RuntimeAdmissionError, match="running image revision differs"):
        load_runtime_admission(inputs(tmp_path))


def test_invalid_signature_is_rejected_before_receipt_fields_are_used(tmp_path: Path) -> None:
    with pytest.raises(RuntimeAdmissionError, match="verification"):
        load_runtime_admission(inputs(tmp_path))


def test_verified_receipt_binds_exact_bytes_and_deployment_identity(
    tmp_path: Path, monkeypatch
) -> None:
    import custos.core.runtime_admission as module

    args = inputs(tmp_path)
    seen = []
    monkeypatch.setattr(module, "verify_sigstore_blob", lambda **kwargs: seen.append(kwargs))
    result = load_runtime_admission(args)
    assert result is not None
    assert result.image_digest == args.runtime_image_digest
    assert result.source_revision == args.runtime_source_revision
    assert seen[0]["payload"] == args.runtime_promotion_receipt.read_bytes()
    assert seen[0]["identity"].workflow_identity == promotion()["workflow"]["workflow_identity"]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "status", "PENDING"),
        (None, "artifact_runtime_ready", False),
        (None, "artifact_runtime_ready", 1),
        (None, "schema_version", True),
        (None, "unexpected", True),
        ("candidate", "digest", "sha256:" + "e" * 64),
        ("candidate", "source_revision", "e" * 40),
        ("candidate", "repository", "ghcr.io/attacker/custos"),
        ("invariants", "image_rebuilt", True),
        ("invariants", "manifest_digest_after", "sha256:" + "e" * 64),
        ("workflow", "workflow_identity", "https://attacker.invalid"),
        ("workflow", "environment", "unprotected"),
        ("inputs", "crucible_acceptance", None),
    ],
)
def test_verified_but_inapplicable_receipt_cannot_enable_live(
    tmp_path: Path, monkeypatch, section: str | None, field: str, value: object
) -> None:
    import custos.core.runtime_admission as module

    document = copy.deepcopy(promotion())
    (document if section is None else document[section])[field] = value
    monkeypatch.setattr(module, "verify_sigstore_blob", lambda **kwargs: None)
    with pytest.raises(RuntimeAdmissionError):
        load_runtime_admission(inputs(tmp_path, document))


def test_real_sigstore_blob_proof_accepts_only_the_signed_receipt(tmp_path: Path) -> None:
    from custos.core.runtime_admission import _IDENTITY
    from tests.sigstore_crypto_fixture import build_offline_blob_fixture

    args = inputs(tmp_path)
    bundle, root = build_offline_blob_fixture(
        tmp_path / "crypto", args.runtime_promotion_receipt.read_bytes(), _IDENTITY
    )
    args.runtime_promotion_bundle = bundle
    args.runtime_sigstore_trusted_root.write_bytes(root)
    assert load_runtime_admission(args) is not None
    args.runtime_promotion_receipt.write_bytes(args.runtime_promotion_receipt.read_bytes() + b" ")
    with pytest.raises(RuntimeAdmissionError, match="verification"):
        load_runtime_admission(args)


def test_runtime_receipt_does_not_replace_signed_deployment_promotion() -> None:
    from uuid import uuid4

    from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
    from custos.core.engine_lifecycle import (
        EngineLifecycleBlocked,
        EngineLifecycleConfig,
        EngineLifecycleSupervisor,
    )

    engine = SimpleNamespace(
        supports_trading_mode=lambda mode: True, supports_venue=lambda venue, mode: True
    )
    supervisor = EngineLifecycleSupervisor(
        engine=engine,
        state_store=object(),
        artifact_capability=ArtifactRuntimeCapabilityV1.production_ready(),
        config=EngineLifecycleConfig(live_execution_enabled=True),
    )
    command = SimpleNamespace(
        deployment_instance_id=uuid4(),
        deployment_spec_id=uuid4(),
        deployment_spec_digest="a" * 64,
        generation=1,
        trading_mode="live",
    )
    with pytest.raises(EngineLifecycleBlocked, match="promotion evidence"):
        supervisor._require_authorized_runtime(
            SimpleNamespace(command=command),
            {"trading_mode": "live", "connector": "binance"},
            {"permission_scope": "trade_no_withdraw"},
        )


def test_offline_cannot_consume_runtime_promotion_flags(tmp_path: Path, capsys) -> None:
    from custos.cli.subcommands import main

    assert (
        main(
            [
                "start",
                "--reconcile-strategy-id",
                "local",
                "--runtime-image-digest",
                "sha256:" + "a" * 64,
            ]
        )
        == 1
    )
    assert "only to the signed lane" in capsys.readouterr().err

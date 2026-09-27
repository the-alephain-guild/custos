from __future__ import annotations

from custos_strategy_publisher.manifest import CUSTOS_ARTIFACT_REF_BINDINGS
from custos_strategy_publisher.producer import fixture_custos_contract_authority_v1


def test_fixture_authority_is_explicitly_non_publishable() -> None:
    authority = fixture_custos_contract_authority_v1()

    assert authority.owner_repository == "tesseract-trading/custos"
    assert authority.owner_commit is None
    assert authority.handoff_ready is False
    for digest in (
        authority.contract_receipt_sha256,
        authority.asset_index_sha256,
        authority.artifact_ref_schema_sha256,
        authority.artifact_ref_golden_sha256,
    ):
        assert len(digest) == 64
        assert digest == digest.lower()


def test_ps_projection_excludes_post_sign_and_lifecycle_facts() -> None:
    fields = set(CUSTOS_ARTIFACT_REF_BINDINGS)

    assert {
        "artifact_coordinate",
        "artifact_sha256",
        "manifest_sha256",
        "sbom_sha256",
        "contract_schema_sha256",
    } <= fields
    assert (
        not {
            "attestation_bundle",
            "trust_policy",
            "verification_receipt",
            "strategy_release_status",
            "deployment_instance_id",
        }
        & fields
    )

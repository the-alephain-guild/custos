from dataclasses import replace
from pathlib import Path

import pytest

from custos_strategy_publisher.model import (
    ArtifactMemberRole,
    BomMemberV1,
    canonical_json_bytes,
    parse_canonical_bom_bytes,
    sha256_hex,
)
from custos_strategy_publisher.producer import (
    CUSTOS_CONTRACT_ASSET_INDEX_FIXTURE_V1_BYTES,
    BomProductionError,
    CleanProducerEvidence,
    fixture_custos_contract_authority_v1,
    produce_canonical_strategy_release_bom,
    verify_canonical_strategy_release_bom,
)


def fixture_members(*, multiple=True):
    root = Path(__file__).resolve().parents[1]
    original = parse_canonical_bom_bytes(
        (root / "contracts/strategy-release-bom-v1.golden.json").read_bytes()
    )
    authority = fixture_custos_contract_authority_v1()
    payloads = {}
    for member in original.members:
        payload = (
            CUSTOS_CONTRACT_ASSET_INDEX_FIXTURE_V1_BYTES
            if member.role is ArtifactMemberRole.CONTRACT_SCHEMA
            else f"fixture:{member.name}".encode()
        )
        actual = replace(member, sha256=sha256_hex(payload), size_bytes=len(payload))
        payloads[actual] = payload
    if multiple:
        payload = b'{"supported_contexts":["local"]}'
        member = BomMemberV1(
            role=ArtifactMemberRole.RUNTIME_ARTIFACT,
            coordinate="fixture://local-context-support",
            name="resources/local-context-support-v1.json",
            media_type="application/json",
            size_bytes=len(payload),
            sha256=sha256_hex(payload),
        )
        payloads[member] = payload
    by_role = {member.role: member for member in payloads}
    bom = replace(
        original,
        members=tuple(payloads),
        execution_abi_schema_sha256=authority.artifact_ref_schema_sha256,
        execution_abi_golden_sha256=authority.artifact_ref_golden_sha256,
        contract_asset_index_sha256=authority.asset_index_sha256,
        toolkit_wheel_sha256=by_role[ArtifactMemberRole.NAUTILUS_WHEEL].sha256,
        toolkit_sbom_sha256=by_role[ArtifactMemberRole.TOOLKIT_SBOM].sha256,
        strategy_sbom_sha256=by_role[ArtifactMemberRole.STRATEGY_SBOM].sha256,
        strategy_source_tree_sha256=by_role[ArtifactMemberRole.SOURCE_TREE].sha256,
        strategy_artifact_sha256=by_role[ArtifactMemberRole.STRATEGY_WHEEL].sha256,
        strategy_artifact_coordinate=by_role[ArtifactMemberRole.STRATEGY_WHEEL].coordinate,
        strategy_manifest_sha256=by_role[ArtifactMemberRole.STRATEGY_MANIFEST].sha256,
    )
    producer = CleanProducerEvidence(bom.producer_repository, bom.producer_commit, True)
    return bom, payloads, producer, authority


def test_two_runtime_artifacts_retain_distinct_bytes_in_the_canonical_bom():
    bom, payloads, producer, authority = fixture_members()
    result = produce_canonical_strategy_release_bom(
        bom, payloads, producer=producer, custos_authority=authority, allow_test_fixtures=True
    )
    assert result.canonical_bytes == canonical_json_bytes(bom.to_mapping())
    assert (
        len(
            [
                member
                for member in result.bom.members
                if member.role is ArtifactMemberRole.RUNTIME_ARTIFACT
            ]
        )
        == 2
    )
    assert (
        verify_canonical_strategy_release_bom(
            result.canonical_bytes,
            payloads,
            producer=producer,
            custos_authority=authority,
            allow_test_fixtures=True,
        )
        == result
    )


@pytest.mark.parametrize("damage", ["missing", "extra", "swapped"])
def test_every_payload_is_bound_to_one_complete_member_identity(damage):
    bom, payloads, producer, authority = fixture_members()
    runtime = [
        member for member in bom.members if member.role is ArtifactMemberRole.RUNTIME_ARTIFACT
    ]
    if damage == "missing":
        payloads.pop(runtime[0])
    elif damage == "extra":
        payloads[replace(runtime[0], name="extra.json")] = payloads[runtime[0]]
    else:
        payloads[runtime[0]], payloads[runtime[1]] = payloads[runtime[1]], payloads[runtime[0]]
    reason = (
        "member digest drift for runtime_artifact"
        if damage == "swapped"
        else "member payload identities must exactly match"
    )
    with pytest.raises(BomProductionError, match=reason):
        produce_canonical_strategy_release_bom(
            bom, payloads, producer=producer, custos_authority=authority, allow_test_fixtures=True
        )


def test_a_single_runtime_artifact_keeps_the_existing_wire_shape():
    bom, payloads, producer, authority = fixture_members(multiple=False)
    result = produce_canonical_strategy_release_bom(
        bom, payloads, producer=producer, custos_authority=authority, allow_test_fixtures=True
    )
    assert result.canonical_bytes == canonical_json_bytes(bom.to_mapping())

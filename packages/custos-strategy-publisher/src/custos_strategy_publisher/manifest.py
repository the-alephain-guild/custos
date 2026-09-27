"""PS-owned projection facts for a caller-supplied Custos V1 authority."""

from __future__ import annotations

from .model import ArtifactMemberRole, StrategyReleaseBomV1

CUSTOS_CANONICALIZATION = "sha256-canonical-json-v1"
CUSTOS_ENTRY_POINT_GROUP = "alephain.strategy_runtime.v1"


CUSTOS_ARTIFACT_REF_BINDINGS = {
    "artifact_coordinate": "StrategyReleaseBomV1.strategy_artifact_coordinate",
    "artifact_sha256": "StrategyReleaseBomV1.strategy_artifact_sha256",
    "manifest_sha256": "StrategyReleaseBomV1.strategy_manifest_sha256",
    "sbom_sha256": "StrategyReleaseBomV1.members[role=strategy_sbom].sha256",
    "contract_schema_sha256": "StrategyReleaseBomV1.contract_asset_index_sha256",
    "normalized_source_tree_sha256": "StrategyReleaseBomV1.strategy_source_tree_sha256",
    "source_repository": "StrategyReleaseBomV1.producer_repository",
    "source_commit": "StrategyReleaseBomV1.strategy_source_commit",
    "engine": "StrategyReleaseBomV1.engine",
    "engine_version": "StrategyReleaseBomV1.engine_version",
    "required_runtime_artifacts": "StrategyReleaseBomV1.members[role=runtime_artifact]",
    "build_inputs": "strategy artifact exact build-lock inputs",
}

CUSTOS_MEMBER_ROLE_SOURCES = {
    "base_contracts_wheel": "StrategyReleaseBomV1.members[role=base_contracts_wheel]",
    "nautilus_wheel": "StrategyReleaseBomV1.members[role=nautilus_wheel]",
    "strategy_wheel": "StrategyReleaseBomV1.members[role=strategy_wheel]",
    "strategy_manifest": "StrategyReleaseBomV1.members[role=strategy_manifest]",
    "runtime_artifact": "StrategyReleaseBomV1.members[role=runtime_artifact]",
    "sbom": "StrategyReleaseBomV1.members[role=strategy_sbom]",
    "contract_schema": "StrategyReleaseBomV1.members[role=contract_schema]",
    "source_tree": "StrategyReleaseBomV1.members[role=source_tree]",
}


def artifact_ref_digest_bindings(
    bom: StrategyReleaseBomV1,
) -> dict[str, str]:
    """Project PS facts without parsing or recreating the Custos-owned schema."""

    return {
        "artifact_coordinate": bom.strategy_artifact_coordinate,
        "artifact_sha256": bom.strategy_artifact_sha256,
        "manifest_sha256": bom.strategy_manifest_sha256,
        "sbom_sha256": bom.member(ArtifactMemberRole.STRATEGY_SBOM).sha256,
        "contract_schema_sha256": bom.contract_asset_index_sha256,
        "normalized_source_tree_sha256": bom.strategy_source_tree_sha256,
        "source_repository": bom.producer_repository,
        "source_commit": bom.strategy_source_commit,
        "engine": bom.engine,
        "engine_version": bom.engine_version,
    }

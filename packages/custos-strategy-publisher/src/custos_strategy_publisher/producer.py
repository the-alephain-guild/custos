from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .model import (
    ArtifactMemberRole,
    StrategyReleaseBomReceiptV1,
    StrategyReleaseBomV1,
    canonical_json_bytes,
    parse_canonical_bom_bytes,
    sha256_hex,
)

CUSTOS_CONTRACT_ASSET_INDEX_FIXTURE_V1_BYTES = (
    b"fixture-only:custos-contract-asset-index-v1:not-a-release"
)

REQUIRED_BOM_MEMBER_ROLES = frozenset(
    {
        ArtifactMemberRole.BASE_CONTRACTS_WHEEL,
        ArtifactMemberRole.NAUTILUS_WHEEL,
        ArtifactMemberRole.STRATEGY_WHEEL,
        ArtifactMemberRole.STRATEGY_MANIFEST,
        ArtifactMemberRole.RUNTIME_ARTIFACT,
        ArtifactMemberRole.TOOLKIT_SBOM,
        ArtifactMemberRole.STRATEGY_SBOM,
        ArtifactMemberRole.CONTRACT_SCHEMA,
        ArtifactMemberRole.SOURCE_TREE,
    }
)


class BomProductionError(ValueError):
    """The BOM input is non-canonical, mutable, unauthoritative, or byte-drifted."""


@dataclass(frozen=True, slots=True)
class CleanProducerEvidence:
    repository: str
    commit: str
    worktree_clean: bool


@dataclass(frozen=True, slots=True)
class CustosContractAuthorityV1:
    """Exact pins supplied by a signed Custos V1 producer receipt."""

    owner_repository: str
    owner_commit: str | None
    contract_receipt_sha256: str
    asset_index_sha256: str
    artifact_ref_schema_sha256: str
    artifact_ref_golden_sha256: str
    handoff_ready: bool


@dataclass(frozen=True, slots=True)
class CanonicalStrategyReleaseBom:
    bom: StrategyReleaseBomV1
    canonical_bytes: bytes
    bom_sha256: str

    def assert_receipt_echo(self, receipt: StrategyReleaseBomReceiptV1) -> None:
        try:
            receipt.assert_matches(self.bom)
        except ValueError as error:
            raise BomProductionError(f"receipt does not losslessly echo BOM: {error}") from error
        if receipt.bom_sha256 != self.bom_sha256:
            raise BomProductionError("receipt BOM digest does not echo canonical producer digest")


def fixture_custos_contract_authority_v1() -> CustosContractAuthorityV1:
    """Return an explicitly non-publishable authority for local schema fixtures."""

    return CustosContractAuthorityV1(
        owner_repository="tesseract-trading/custos",
        owner_commit=None,
        contract_receipt_sha256=sha256_hex(
            b"fixture-only:custos-contract-receipt-v1:not-a-release"
        ),
        asset_index_sha256=sha256_hex(CUSTOS_CONTRACT_ASSET_INDEX_FIXTURE_V1_BYTES),
        artifact_ref_schema_sha256=sha256_hex(
            b"fixture-only:custos-artifact-ref-schema-v1:not-a-release"
        ),
        artifact_ref_golden_sha256=sha256_hex(
            b"fixture-only:custos-artifact-ref-golden-v1:not-a-release"
        ),
        handoff_ready=False,
    )


def _require_exact_authority(
    authority: CustosContractAuthorityV1,
    *,
    allow_test_fixtures: bool,
) -> None:
    if authority.owner_repository != "tesseract-trading/custos":
        raise BomProductionError("Custos V1 owner repository differs")
    for label, digest in (
        ("contract receipt", authority.contract_receipt_sha256),
        ("asset index", authority.asset_index_sha256),
        ("artifact-ref schema", authority.artifact_ref_schema_sha256),
        ("artifact-ref golden", authority.artifact_ref_golden_sha256),
    ):
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise BomProductionError(f"Custos V1 {label} digest is not lowercase sha256")
    if not authority.handoff_ready and not allow_test_fixtures:
        raise BomProductionError("Custos canonical V1 handoff is not ready")
    if authority.handoff_ready and authority.owner_commit is None:
        raise BomProductionError("ready Custos canonical V1 authority requires owner_commit")


def _require_clean_producer(
    bom: StrategyReleaseBomV1,
    producer: CleanProducerEvidence,
) -> None:
    if not producer.worktree_clean:
        raise BomProductionError("clean producer evidence is required")
    if producer.repository != bom.producer_repository:
        raise BomProductionError("producer repository does not match BOM")
    if producer.commit != bom.producer_commit:
        raise BomProductionError("producer commit does not match BOM")
    if bom.strategy_source_commit != producer.commit:
        raise BomProductionError("strategy source commit does not match clean producer commit")


def _require_immutable_member_coordinate(
    coordinate: str,
    digest: str,
    *,
    allow_test_fixtures: bool,
) -> None:
    lowered = coordinate.lower()
    if coordinate.startswith("/") or coordinate.startswith("file://"):
        raise BomProductionError("member immutable coordinate cannot be an absolute path")
    if "registry/strategies.yaml" in lowered or lowered.endswith(":latest"):
        raise BomProductionError("member cannot use legacy registry or mutable tag authority")
    if "strategy-release-bom" in lowered or lowered.endswith("/bom"):
        raise BomProductionError("member cannot create a BOM self-reference")
    if coordinate.startswith("fixture://"):
        if allow_test_fixtures:
            return
        raise BomProductionError("fixture coordinate is not valid release authority")
    if f"@sha256:{digest}" not in coordinate:
        raise BomProductionError("member immutable coordinate must embed its exact sha256 digest")


def _require_cross_field_bindings(
    bom: StrategyReleaseBomV1,
    by_role: Mapping[ArtifactMemberRole, object],
    authority: CustosContractAuthorityV1,
) -> None:
    members = {role: bom.member(role) for role in by_role}
    required = {
        "contract asset index digest": (
            bom.contract_asset_index_sha256,
            authority.asset_index_sha256,
        ),
        "execution schema digest": (
            bom.execution_abi_schema_sha256,
            authority.artifact_ref_schema_sha256,
        ),
        "execution golden digest": (
            bom.execution_abi_golden_sha256,
            authority.artifact_ref_golden_sha256,
        ),
        "contract schema member digest": (
            members[ArtifactMemberRole.CONTRACT_SCHEMA].sha256,
            authority.asset_index_sha256,
        ),
        "toolkit wheel digest": (
            bom.toolkit_wheel_sha256,
            members[ArtifactMemberRole.NAUTILUS_WHEEL].sha256,
        ),
        "toolkit SBOM digest": (
            bom.toolkit_sbom_sha256,
            members[ArtifactMemberRole.TOOLKIT_SBOM].sha256,
        ),
        "strategy SBOM digest": (
            bom.strategy_sbom_sha256,
            members[ArtifactMemberRole.STRATEGY_SBOM].sha256,
        ),
        "source tree digest": (
            bom.strategy_source_tree_sha256,
            members[ArtifactMemberRole.SOURCE_TREE].sha256,
        ),
        "strategy artifact coordinate": (
            bom.strategy_artifact_coordinate,
            members[ArtifactMemberRole.STRATEGY_WHEEL].coordinate,
        ),
        "strategy artifact digest": (
            bom.strategy_artifact_sha256,
            members[ArtifactMemberRole.STRATEGY_WHEEL].sha256,
        ),
        "strategy manifest digest": (
            bom.strategy_manifest_sha256,
            members[ArtifactMemberRole.STRATEGY_MANIFEST].sha256,
        ),
    }
    for label, (actual, expected) in required.items():
        if actual != expected:
            raise BomProductionError(f"{label} drift: expected {expected!r}, got {actual!r}")


def _validate_member_bytes(
    bom: StrategyReleaseBomV1,
    member_payloads: Mapping[ArtifactMemberRole, bytes],
    *,
    allow_test_fixtures: bool,
) -> None:
    bom_roles = {member.role for member in bom.members}
    payload_roles = set(member_payloads)
    if bom_roles != REQUIRED_BOM_MEMBER_ROLES:
        raise BomProductionError(
            f"BOM member roles mismatch: expected {sorted(role.value for role in REQUIRED_BOM_MEMBER_ROLES)}"
        )
    if payload_roles != REQUIRED_BOM_MEMBER_ROLES:
        raise BomProductionError(
            "member payload roles must exactly match the canonical BOM member roles"
        )
    coordinates: set[str] = set()
    names: set[str] = set()
    for member in bom.members:
        if member.coordinate in coordinates or member.name in names:
            raise BomProductionError("duplicate member coordinate or name")
        coordinates.add(member.coordinate)
        names.add(member.name)
        payload = member_payloads[member.role]
        if not isinstance(payload, bytes):
            raise BomProductionError(f"member payload must be bytes: {member.role.value}")
        actual_digest = sha256_hex(payload)
        if member.sha256 != actual_digest:
            raise BomProductionError(
                f"member digest drift for {member.role.value}: expected {member.sha256}, got {actual_digest}"
            )
        if member.size_bytes != len(payload):
            raise BomProductionError(
                f"member size drift for {member.role.value}: expected {member.size_bytes}, got {len(payload)}"
            )
        _require_immutable_member_coordinate(
            member.coordinate,
            member.sha256,
            allow_test_fixtures=allow_test_fixtures,
        )


def produce_canonical_strategy_release_bom(
    bom: StrategyReleaseBomV1,
    member_payloads: Mapping[ArtifactMemberRole, bytes],
    *,
    producer: CleanProducerEvidence,
    custos_authority: CustosContractAuthorityV1,
    allow_test_fixtures: bool = False,
) -> CanonicalStrategyReleaseBom:
    """Produce canonical BOM bytes only after all immutable evidence is verified."""

    _require_exact_authority(
        custos_authority,
        allow_test_fixtures=allow_test_fixtures,
    )
    _require_clean_producer(bom, producer)
    _validate_member_bytes(
        bom,
        member_payloads,
        allow_test_fixtures=allow_test_fixtures,
    )
    _require_cross_field_bindings(bom, member_payloads, custos_authority)
    normalized = StrategyReleaseBomV1.from_mapping(bom.to_mapping())
    payload = canonical_json_bytes(normalized.to_mapping())
    return CanonicalStrategyReleaseBom(
        bom=normalized,
        canonical_bytes=payload,
        bom_sha256=sha256_hex(payload),
    )


def verify_canonical_strategy_release_bom(
    payload: bytes,
    member_payloads: Mapping[ArtifactMemberRole, bytes],
    *,
    producer: CleanProducerEvidence,
    custos_authority: CustosContractAuthorityV1,
    allow_test_fixtures: bool = False,
) -> CanonicalStrategyReleaseBom:
    """Verify external BOM bytes without accepting normalization or field loss."""

    try:
        bom = parse_canonical_bom_bytes(payload)
    except ValueError as error:
        message = str(error)
        if message == "BOM bytes are not canonical JSON":
            raise BomProductionError(message) from error
        if "local filesystem path" in message:
            raise BomProductionError(
                f"member immutable coordinate is invalid: {message}"
            ) from error
        raise BomProductionError(f"invalid BOM: {message}") from error
    product = produce_canonical_strategy_release_bom(
        bom,
        member_payloads,
        producer=producer,
        custos_authority=custos_authority,
        allow_test_fixtures=allow_test_fixtures,
    )
    if product.canonical_bytes != payload:
        raise BomProductionError("BOM bytes are not canonical JSON")
    return product

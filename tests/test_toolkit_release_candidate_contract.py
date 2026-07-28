from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from custos_toolkit.contracts import (
    ImmutableToolkitArtifactBindingV1,
    LockedToolkitDependencyV1,
    ToolkitRcMemberRole,
    ToolkitRcMemberV1,
    ToolkitRcReceiptManifestV1,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json"
CONTRACT_INDEX = ROOT / "docs/authority/strategy-contract-assets-v1.json"
DIGESTS = tuple(f"{value:064x}" for value in range(1, 32))


def _artifact(name: str, digest: str) -> ImmutableToolkitArtifactBindingV1:
    return ImmutableToolkitArtifactBindingV1(
        coordinate=f"ghcr.io/alephain/{name}@sha256:{digest}",
        sha256=digest,
        size_bytes=1024,
    )


def _member(role: ToolkitRcMemberRole, **overrides: object) -> ToolkitRcMemberV1:
    is_base = role is ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL
    values: dict[str, object] = {
        "role": role,
        "distribution_name": (
            "custos-strategy-toolkit" if is_base else "custos-strategy-toolkit-nautilus"
        ),
        "version": "0.1.0rc1",
        "python_requires": ">=3.11" if is_base else ">=3.12,<3.13",
        "nautilus_version": None if is_base else "1.230.0",
        "top_level_modules": ("custos_toolkit",) if is_base else ("custos_toolkit_nautilus",),
        "dependencies": (
            LockedToolkitDependencyV1(
                name="pydantic",
                version="2.12.5",
                requirement="pydantic==2.12.5",
            ),
        ),
        "wheel": _artifact(f"{role.value}.whl", DIGESTS[0]),
        "sbom": _artifact(f"{role.value}.spdx.json", DIGESTS[1]),
        "contract_schema": _artifact(f"{role.value}.schema.json", DIGESTS[2]),
        "contract_asset_index": _artifact(f"{role.value}.index.json", DIGESTS[3]),
        "dependency_lock_evidence": _artifact(f"{role.value}.dependency-locks.json", DIGESTS[4]),
        "slsa_provenance": _artifact(f"{role.value}.intoto.json", DIGESTS[5]),
        "sigstore_attestation": _artifact(f"{role.value}.sigstore.json", DIGESTS[6]),
        "source_repository": "https://github.com/the-alephain-guild/custos",
        "source_commit": "a" * 40,
        "toolkit_extraction_receipt": _artifact(f"{role.value}.extraction.json", DIGESTS[7]),
        "toolkit_typing_closure_receipt": _artifact(
            f"{role.value}.typing-closure.json", DIGESTS[8]
        ),
        "pre_import_verifier_receipt": _artifact(
            f"{role.value}.pre-import-verifier.json", DIGESTS[9]
        ),
    }
    values.update(overrides)
    return ToolkitRcMemberV1.model_validate(values)


def _manifest(*members: ToolkitRcMemberV1) -> ToolkitRcReceiptManifestV1:
    return ToolkitRcReceiptManifestV1(candidate_version="0.1.0rc1", members=members)


def test_toolkit_rc_contract_is_public_and_schema_is_generated() -> None:
    expected = ToolkitRcReceiptManifestV1.model_json_schema(mode="validation")

    assert expected["$id"] == (
        "https://custos.the-alephain-guild/contracts/toolkit-rc-receipt-manifest-v1.schema.json"
    )
    assert expected["title"] == "ToolkitRcReceiptManifestV1"
    assert expected["additionalProperties"] is False
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == expected


def test_receipt_manifest_requires_exact_base_and_nautilus_member_matrix() -> None:
    base = _member(ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL)
    nautilus = _member(ToolkitRcMemberRole.NAUTILUS_WHEEL)

    manifest = _manifest(base, nautilus)
    assert manifest.candidate_version == "0.1.0rc1"
    assert tuple(member.role for member in manifest.members) == (
        ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL,
        ToolkitRcMemberRole.NAUTILUS_WHEEL,
    )

    with pytest.raises(ValidationError, match="exactly one base and one Nautilus"):
        _manifest(base, base)
    with pytest.raises(ValidationError, match="base contracts member policy"):
        _member(ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL, python_requires=">=3.12")
    with pytest.raises(ValidationError, match="Nautilus member policy"):
        _member(ToolkitRcMemberRole.NAUTILUS_WHEEL, nautilus_version="1.231.0")
    with pytest.raises(ValidationError, match="source repository identity differs"):
        _member(
            ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL,
            source_repository="https://github.com/alchymia-labs/custos",
        )


def test_member_evidence_is_complete_digest_pinned_and_registry_locked() -> None:
    with pytest.raises(ValidationError, match="digest-pinned coordinate"):
        ImmutableToolkitArtifactBindingV1(
            coordinate="ghcr.io/alephain/toolkit:0.1.0rc1",
            sha256=DIGESTS[0],
            size_bytes=1024,
        )
    with pytest.raises(ValidationError, match="coordinate digest must match sha256"):
        ImmutableToolkitArtifactBindingV1(
            coordinate=f"ghcr.io/alephain/toolkit@sha256:{DIGESTS[1]}",
            sha256=DIGESTS[0],
            size_bytes=1024,
        )
    with pytest.raises(ValidationError, match="exact registry requirement"):
        LockedToolkitDependencyV1(
            name="local-toolkit",
            version="0.1.0rc1",
            requirement="local-toolkit @ file:///workspace/local-toolkit",
        )

    complete = _member(ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL).model_dump(mode="json")
    for field in (
        "wheel",
        "sbom",
        "contract_schema",
        "contract_asset_index",
        "dependency_lock_evidence",
        "slsa_provenance",
        "sigstore_attestation",
        "source_commit",
        "toolkit_extraction_receipt",
        "toolkit_typing_closure_receipt",
        "pre_import_verifier_receipt",
    ):
        incomplete = dict(complete)
        incomplete.pop(field)
        with pytest.raises(ValidationError, match="Field required"):
            ToolkitRcMemberV1.model_validate(incomplete)


def test_contract_rejects_legacy_modules_mutability_and_non_rc_claims() -> None:
    for module in ("shared", "shared.indicators", "pandas_ta", "pandas_ta.momentum"):
        with pytest.raises(ValidationError, match="forbidden top-level toolkit module"):
            _member(
                ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL,
                top_level_modules=("custos_toolkit", module),
            )

    manifest = _manifest(
        _member(ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL),
        _member(ToolkitRcMemberRole.NAUTILUS_WHEEL),
    ).model_dump(mode="json")
    mutable = dict(manifest)
    mutable["overwrite_allowed"] = True
    with pytest.raises(ValidationError, match="Input should be False"):
        ToolkitRcReceiptManifestV1.model_validate(mutable)

    for forbidden_claim in (
        "loaded_entry_point",
        "engine_ready",
        "runtime_ready",
        "production_ready",
        "strategy_release_bom",
    ):
        claimed = dict(manifest)
        claimed[forbidden_claim] = True
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ToolkitRcReceiptManifestV1.model_validate(claimed)


def test_authority_registers_exact_v1_ready_receipt() -> None:
    manifest = json.loads((ROOT / "authority-manifest.json").read_text(encoding="utf-8"))
    assert {
        "role": "toolkit_rc_receipt_manifest_schema_v1_contract_foundation",
        "path": "docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json",
        "contract_only": False,
        "ready_receipt_published": True,
    } in manifest["authority_documents"]
    assert CONTRACT_INDEX.is_file()
    assert not any(
        re.search(
            r"strategy-contract-assets-v(?:[2-9]|[1-9][0-9]+)\.json$",
            entry.get("path", ""),
        )
        for entry in manifest["authority_documents"]
        if isinstance(entry, dict)
    )
    receipt_path = ROOT / "docs/authority/receipts/custos-toolkit-rc-authority-v1.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "READY_TOOLKIT_RC"
    assert receipt["candidate_version"] == "0.1.0rc5"
    assert receipt["source_commit"] == "a3fd88c4e7e25433b508b2fece94be876630f380"

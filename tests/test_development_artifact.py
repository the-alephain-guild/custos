from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from custos_toolkit.contracts.strategy_execution import DevelopmentSourceRefV1

from custos.artifacts.development_source import (
    DevelopmentSourceVerificationError,
    canonical_development_source_digest,
    verify_development_artifact,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _descriptor(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _store(root: Path) -> tuple[DevelopmentSourceRefV1, str, Path]:
    wheel = b"deterministic development wheel"
    strategy_manifest = _canonical(
        {
            "base_contracts_version": "0.1.0",
            "engine": "nautilus",
            "engine_toolkit_version": "0.1.0",
            "entry_point": "alephain_strategy_supertrend.runtime:SuperTrendRuntimeAdapterV1",
            "entry_point_group": "alephain.strategy_runtime.v1",
            "schema_version": 1,
        }
    )
    wheel_path = "layers/alephain_strategy_supertrend-2.1.0-py3-none-any.whl"
    strategy_manifest_path = "layers/strategy-manifest-v1.json"
    source_tree_digest = "1" * 64
    toolkit_digest = "2" * 64
    build_manifest = _canonical(
        {
            "artifacts": {
                "strategy_manifest": _descriptor(strategy_manifest_path, strategy_manifest),
                "strategy_wheel": _descriptor(wheel_path, wheel),
            },
            "entry_point_group": "alephain.strategy_runtime.v1",
            "entry_point_name": ("alephain_strategy_supertrend.runtime:SuperTrendRuntimeAdapterV1"),
            "producer_repository": "alchymia-labs/philosophers-stone",
            "schema_version": "alephain.strategy-development-build-manifest.v1",
            "strategy_coordinate": "ps://strategy/trend/supertrend@2.1.0",
            "strategy_source_tree_sha256": source_tree_digest,
            "toolkit_source_digest_profile": "sha256-canonical-custos-toolkit-source-v1",
            "toolkit_source_sha256": toolkit_digest,
            "toolkit_version": "0.1.0",
        }
    )
    files = {
        "development-build-manifest-v1.json": build_manifest,
        strategy_manifest_path: strategy_manifest,
        wheel_path: wheel,
    }
    source_digest = canonical_development_source_digest(files)
    source_root = root / "sha256" / source_digest
    for relative, content in files.items():
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    receipt = _canonical(
        {
            "build_manifest_sha256": hashlib.sha256(build_manifest).hexdigest(),
            "external_publication_completed": False,
            "producer_repository": "alchymia-labs/philosophers-stone",
            "promotable": False,
            "publication_kind": "content-addressed-directory-v1",
            "schema_version": ("alephain.strategy-artifact-development-publication-receipt.v1"),
            "source_digest_profile": "sha256-canonical-directory-v1",
            "source_sha256": source_digest,
            "strategy_coordinate": "ps://strategy/trend/supertrend@2.1.0",
            "strategy_source_tree_sha256": source_tree_digest,
            "toolkit_source_sha256": toolkit_digest,
            "toolkit_version": "0.1.0",
            "trading_mode": "sandbox",
        }
    )
    receipt_digest = hashlib.sha256(receipt).hexdigest()
    receipt_path = root / "receipts/sha256" / f"{receipt_digest}.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt)
    return (
        DevelopmentSourceRefV1(
            schema_version=1,
            source_path=str(source_root),
            source_sha256=source_digest,
            trading_mode="sandbox",
            promotable=False,
        ),
        receipt_digest,
        source_root,
    )


def test_development_artifact_verifies_minimal_local_manifest(tmp_path: Path) -> None:
    source_ref, receipt_digest, _ = _store(tmp_path)

    verified = verify_development_artifact(
        source_ref,
        publication_receipt_digest=receipt_digest,
        configured_root=tmp_path,
        runtime_mode="sandbox",
    )

    assert verified.entry_point_group == "alephain.strategy_runtime.v1"
    assert verified.strategy_manifest["engine"] == "nautilus"
    assert verified.publication_receipt["promotable"] is False


def test_development_artifact_rejects_live_and_byte_drift(tmp_path: Path) -> None:
    source_ref, receipt_digest, source_root = _store(tmp_path)
    with pytest.raises(DevelopmentSourceVerificationError, match="restricted to sandbox"):
        verify_development_artifact(
            source_ref,
            publication_receipt_digest=receipt_digest,
            configured_root=tmp_path,
            runtime_mode="live",
        )

    (source_root / "layers/strategy-manifest-v1.json").write_bytes(b"{}")
    with pytest.raises(DevelopmentSourceVerificationError, match="source digest differs"):
        verify_development_artifact(
            source_ref,
            publication_receipt_digest=receipt_digest,
            configured_root=tmp_path,
            runtime_mode="sandbox",
        )

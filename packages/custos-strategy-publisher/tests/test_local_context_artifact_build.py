"""Exercise local context declarations through the actual wheel and release builder."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_template_strategy import PRODUCER, STRATEGY, _strategy

from custos_strategy_publisher.artifact_build import (
    ArtifactBuildError,
    build_strategy_artifact_tree,
    typing_receipt_bytes,
)
from custos_strategy_publisher.model import canonical_json_bytes
from custos_strategy_publisher.template import template_source_inventory, template_strategy_spec
from custos_strategy_publisher.toolkit_authority import load_toolkit_authority
from custos_strategy_publisher.toolkit_registry import ToolkitRegistrySnapshotV1
from custos_strategy_publisher.wheel_archive import build_wheel

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = "custos_toolkit/contracts/assets/local_strategy_execution_context_v1.schema.json"
SUPPORT_NAME = "resources/local-context-support-v1.json"
SCHEMA = (ROOT / "packages/custos-strategy-toolkit/src" / SCHEMA_PATH).read_bytes()


class FixtureTypingVerifier:
    """No typing claim: this fixture isolates archive and metadata production."""

    def verify(self, request):
        return typing_receipt_bytes(
            request, verifier="fixture-not-a-typing-acceptance", verified=True
        )


def _inputs(tmp_path: Path, *, schema: bytes | None = SCHEMA, index: bytes | None = None):
    strategy = _strategy(tmp_path)
    (strategy / "config.yaml").write_text(
        "parameters:\n  atr_period:\n    value: 10\n"
        "trading:\n  connector:\n    value: papertrade\n"
        "  pairs:\n    value: [BTC-USD-PERP]\n  leverage:\n    value: 1\n"
    )
    spec = replace(template_strategy_spec(tmp_path, STRATEGY, PRODUCER), local_context_support=True)
    authority = load_toolkit_authority(
        ROOT,
        candidate_version="0.1.0rc10",
        authority_commit="0ae62c1dfe1d8de7c28bcc884e655eace3679d60",
    )
    base_files = {
        "custos_toolkit/__init__.py": b"",
        "custos_toolkit/config/base_config.yaml": b"parameters: {}\ntrading: {}\n",
    }
    if schema is not None:
        base_files[SCHEMA_PATH] = schema
    base_wheel = build_wheel(
        base_files,
        distribution="custos-strategy-toolkit",
        version=authority.candidate_version,
        requires_python=">=3.11",
        dependencies=(),
        entry_points={},
        generator="fixture",
    )
    nt_wheel = build_wheel(
        {"custos_toolkit_nautilus/__init__.py": b""},
        distribution="custos-strategy-toolkit-nautilus",
        version=authority.candidate_version,
        requires_python=">=3.12,<3.13",
        dependencies=(),
        entry_points={},
        generator="fixture",
    )
    if index is None:
        index = (ROOT / "docs/authority/strategy-contract-assets-v1.json").read_bytes()
    payloads = {
        "base_contracts_wheel": base_wheel,
        "nautilus_wheel": nt_wheel,
        "contract_asset_index": index,
        "base_contracts_sbom": b'{"fixture":"base-sbom"}',
        "nautilus_sbom": b'{"fixture":"nt-sbom"}',
    }
    replacements = {
        field: replace(
            getattr(authority, field),
            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            size_bytes=len(content),
        )
        for field, content in payloads.items()
    }
    manifest = b'{"fixture":"not-a-published-toolkit"}'
    authority = replace(
        authority,
        **replacements,
        manifest_digest=f"sha256:{hashlib.sha256(manifest).hexdigest()}",
        manifest_size_bytes=len(manifest),
    )
    snapshot = ToolkitRegistrySnapshotV1(manifest=manifest, **payloads)
    inventory = template_source_inventory(tmp_path, STRATEGY, spec)
    return spec, authority, snapshot, inventory


def _build(tmp_path, *, schema=SCHEMA, local=True, index=None):
    spec, authority, toolkit, inventory = _inputs(tmp_path, schema=schema, index=index)
    spec = replace(spec, local_context_support=local)
    build_strategy_artifact_tree(
        inventory=inventory,
        spec=spec,
        producer=PRODUCER,
        authority=authority,
        toolkit=toolkit,
        producer_commit="a" * 40,
        producer_worktree_clean=True,
        source_date_epoch=1_792_000_000,
        discovery_tag="fixture-local-context",
        output=tmp_path / "release",
        typing_verifier=FixtureTypingVerifier(),
    )
    layers = tmp_path / "release/layers"
    with zipfile.ZipFile(io.BytesIO((layers / spec.wheel_name).read_bytes())) as wheel:
        files = {name: wheel.read(name) for name in wheel.namelist()}
    return spec, files, layers


def test_local_declaration_is_bound_to_actual_wheel_manifest_ref_and_bom(tmp_path):
    spec, files, layers = _build(tmp_path)
    content = files[f"{spec.package}/{SUPPORT_NAME}"]
    support = json.loads(content)
    assert support == {
        "schema_version": 1,
        "control_source": "local",
        "execution_abi": "alephain.strategy_runtime.v1",
        "context_schema_id": json.loads(SCHEMA)["$id"],
        "context_schema_sha256": hashlib.sha256(SCHEMA).hexdigest(),
    }
    assert canonical_json_bytes(support) == content
    manifest = json.loads((layers / "strategy-manifest-v1.json").read_bytes())
    artifact_ref = json.loads((layers / "strategy-artifact-ref-v1.json").read_bytes())
    bom = json.loads((layers / "strategy-release-bom-v1.json").read_bytes())
    for members in (
        manifest["runtime_artifacts"],
        artifact_ref["required_runtime_artifacts"],
        bom["members"],
    ):
        matches = [member for member in members if member["name"] == SUPPORT_NAME]
        assert len(matches) == 1
        assert matches[0]["role"] == "runtime_artifact"
        assert matches[0]["sha256"] == hashlib.sha256(content).hexdigest()
        assert matches[0]["size_bytes"] == len(content)
    assert len(manifest["runtime_artifacts"]) == 2
    assert (
        b"execution_context: StrategyRuntimeExecutionContextV1"
        in files[f"{spec.package}/runtime.py"]
    )


@pytest.mark.parametrize(
    "schema",
    [None, b"not-json", b"{}", b'{"$id":"wrong"}', SCHEMA + b"\n"],
    ids=["absent", "invalid-json", "missing-id", "wrong-id", "noncanonical"],
)
def test_invalid_or_absent_toolkit_local_schema_prevents_production(tmp_path, schema):
    with pytest.raises(ArtifactBuildError, match="local context schema"):
        _build(tmp_path, schema=schema)
    assert not (tmp_path / "release").exists()


def test_platform_only_build_does_not_require_or_advertise_local_support(tmp_path):
    spec, files, layers = _build(tmp_path, schema=None, local=False)
    assert f"{spec.package}/{SUPPORT_NAME}" not in files
    manifest = json.loads((layers / "strategy-manifest-v1.json").read_bytes())
    assert len(manifest["runtime_artifacts"]) == 1
    assert b"execution_context: StrategyExecutionContextV1" in files[f"{spec.package}/runtime.py"]


def test_same_schema_id_with_changed_constraints_is_refused(tmp_path):
    changed = json.loads(SCHEMA)
    del changed["required"]
    with pytest.raises(ArtifactBuildError, match="local context schema"):
        _build(tmp_path, schema=canonical_json_bytes(changed))


@pytest.mark.parametrize("packaged", [False, True], ids=["gateway", "package"])
@pytest.mark.parametrize("damage", ["missing", "duplicate", "digest", "size", "boolean-size"])
def test_each_schema_index_binding_is_required_and_exact(tmp_path, packaged, damage):
    index = json.loads((ROOT / "docs/authority/strategy-contract-assets-v1.json").read_bytes())
    path = (
        f"packages/custos-strategy-toolkit/src/{SCHEMA_PATH}"
        if packaged
        else "docs/gateway-contract/v1/local_strategy_execution_context_v1.schema.json"
    )
    entry = next(asset for asset in index["assets"] if asset["path"] == path)
    if damage == "missing":
        index["assets"].remove(entry)
    elif damage == "duplicate":
        index["assets"].append(dict(entry))
    elif damage == "digest":
        entry["sha256"] = "0" * 64
    elif damage == "boolean-size":
        entry["size_bytes"] = True
    else:
        entry["size_bytes"] += 1
    with pytest.raises(ArtifactBuildError, match="local context schema"):
        _build(tmp_path, index=canonical_json_bytes(index))


def test_platform_build_accepts_historical_index_without_local_entries(tmp_path):
    index = json.loads((ROOT / "docs/authority/strategy-contract-assets-v1.json").read_bytes())
    index["assets"] = [
        asset
        for asset in index["assets"]
        if "local_strategy_execution_context" not in asset["path"]
    ]
    _build(tmp_path, schema=None, local=False, index=canonical_json_bytes(index))

"""Local material preparation consumes real producer output without importing strategy code."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest
from custos_toolkit.contracts.strategy_execution import StrategyArtifactRefV1, canonical_json_bytes

from custos.artifacts.errors import ArtifactVerificationError
from custos.artifacts.local_context import prepare_local_strategy_material
from custos.artifacts.policy import ArchiveLimitsV1


@pytest.fixture
def release(tmp_path, monkeypatch):
    producer_tests = (
        Path(__file__).resolve().parents[1] / "packages/custos-strategy-publisher/tests"
    )
    monkeypatch.syspath_prepend(str(producer_tests.parent / "src"))
    monkeypatch.syspath_prepend(str(producer_tests))
    fixture = importlib.import_module("test_local_context_artifact_build")
    spec, files, layers = fixture._build(tmp_path)
    return {
        "spec": spec,
        "files": files,
        "wheel_path": layers / spec.wheel_name,
        "manifest_bytes": (layers / "strategy-manifest-v1.json").read_bytes(),
        "artifact_ref": StrategyArtifactRefV1.model_validate_json(
            (layers / "strategy-artifact-ref-v1.json").read_bytes()
        ),
        "release_bom_bytes": (layers / "strategy-release-bom-v1.json").read_bytes(),
        "quarantine_parent": tmp_path / "quarantine",
        "limits": ArchiveLimitsV1(),
    }


def _prepare(release):
    return prepare_local_strategy_material(
        **{key: value for key, value in release.items() if key not in {"spec", "files"}}
    )


def test_actual_producer_release_prepares_without_import_or_execution_authority(release):
    package = release["spec"].package
    assert not any(name == package or name.startswith(package + ".") for name in sys.modules)
    prepared = _prepare(release)
    try:
        assert prepared.manifest.entry_point == release["spec"].entry_point
        assert prepared.support.control_source == "local"
        assert prepared.manifest_digest == hashlib.sha256(release["manifest_bytes"]).hexdigest()
        assert prepared.quarantine.root.is_dir()
        assert not hasattr(prepared, "ready")
        assert not any(name == package or name.startswith(package + ".") for name in sys.modules)
    finally:
        prepared.quarantine.destroy()


@pytest.mark.parametrize(
    "damage",
    [
        "wheel",
        "manifest",
        "runtime-member",
        "entry-point",
        "malformed-member",
        "missing-wheel",
        "wheel-top",
        "python",
    ],
)
def test_inconsistent_release_inputs_never_reach_a_prepared_directory(release, damage):
    if damage == "wheel":
        release["wheel_path"].write_bytes(release["wheel_path"].read_bytes() + b"changed")
    elif damage == "manifest":
        release["manifest_bytes"] += b" "
    elif damage == "missing-wheel":
        release["wheel_path"].unlink()
    else:
        bom = json.loads(release["release_bom_bytes"])
        if damage == "runtime-member":
            next(member for member in bom["members"] if member["role"] == "runtime_artifact")[
                "sha256"
            ] = "0" * 64
        elif damage == "entry-point":
            bom["entry_point_name"] = "wrong:Runtime"
        elif damage == "wheel-top":
            bom["strategy_artifact_sha256"] = "0" * 64
        elif damage == "python":
            bom["python_requires"] = ">=3.14"
        else:
            bom["members"].append("not-an-object")
        release["release_bom_bytes"] = canonical_json_bytes(bom)
    with pytest.raises(ArtifactVerificationError):
        _prepare(release)
    parent = release["quarantine_parent"]
    assert not parent.exists() or not any(parent.iterdir())


def test_preparation_uses_the_verified_byte_snapshot_if_the_path_is_replaced(release, monkeypatch):
    from custos.artifacts import local_context

    verify = local_context.verify_execution_member_files

    def replace_after_verification(bom, paths):
        result = verify(bom, paths)
        release["wheel_path"].write_bytes(b"unverified replacement")
        return result

    monkeypatch.setattr(local_context, "verify_execution_member_files", replace_after_verification)
    prepared = _prepare(release)
    try:
        assert prepared.strategy_wheel.verified_content != b"unverified replacement"
        assert (
            prepared.quarantine.root.joinpath(release["spec"].package, "runtime.py").read_bytes()
            == release["files"][release["spec"].package + "/runtime.py"]
        )
    finally:
        prepared.quarantine.destroy()


def _replace_support(release, content: bytes, *, declared=True):
    """Construct malformed but internally re-pinned execution metadata, without signing it."""
    from custos_strategy_publisher.wheel_archive import build_wheel

    spec = release["spec"]
    name = "resources/local-context-support-v1.json"
    package_files = {
        path: value for path, value in release["files"].items() if ".dist-info/" not in path
    }
    package_files[f"{spec.package}/{name}"] = content
    manifest = json.loads(release["manifest_bytes"])
    ref = release["artifact_ref"].model_dump(mode="json")
    bom = json.loads(release["release_bom_bytes"])
    for members in (
        manifest["runtime_artifacts"],
        ref["required_runtime_artifacts"],
        bom["members"],
    ):
        row = next(member for member in members if member["name"] == name)
        if declared:
            row["sha256"] = hashlib.sha256(content).hexdigest()
            row["size_bytes"] = len(content)
            if "coordinate" in row:
                row["coordinate"] = (
                    row["coordinate"].split("@sha256:")[0] + "@sha256:" + row["sha256"]
                )
        else:
            members.remove(row)
    wheel = build_wheel(
        package_files,
        distribution=spec.distribution,
        version=spec.version,
        requires_python=manifest["requires_python"],
        dependencies=(),
        entry_points={manifest["entry_point_group"]: {spec.registered_name: spec.entry_point}},
        generator="malformed-local-material-fixture",
    )
    wheel_digest = hashlib.sha256(wheel).hexdigest()
    ref["artifact_sha256"] = wheel_digest
    ref["artifact_size_bytes"] = len(wheel)
    ref["artifact_coordinate"] = (
        ref["artifact_coordinate"].split("@sha256:")[0] + "@sha256:" + wheel_digest
    )
    bom["strategy_artifact_sha256"] = wheel_digest
    bom["strategy_artifact_coordinate"] = ref["artifact_coordinate"]
    for member in bom["members"]:
        if member["role"] == "strategy_wheel":
            member.update(
                sha256=wheel_digest, size_bytes=len(wheel), coordinate=ref["artifact_coordinate"]
            )
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    ref["manifest_sha256"] = manifest_digest
    ref["manifest_size_bytes"] = len(manifest_bytes)
    bom["strategy_manifest_sha256"] = manifest_digest
    for member in bom["members"]:
        if member["role"] == "strategy_manifest":
            member.update(sha256=manifest_digest, size_bytes=len(manifest_bytes))
            member["coordinate"] = (
                member["coordinate"].split("@sha256:")[0] + "@sha256:" + manifest_digest
            )
    release["wheel_path"].write_bytes(wheel)
    release["manifest_bytes"] = manifest_bytes
    release["artifact_ref"] = StrategyArtifactRefV1.model_validate(ref)
    release["release_bom_bytes"] = canonical_json_bytes(bom)


@pytest.mark.parametrize(
    "damage",
    ["extra-field", "abi", "source", "schema-id", "schema-digest", "duplicate-key", "undeclared"],
)
def test_invalid_support_is_rejected_even_when_all_execution_metadata_matches(release, damage):
    name = release["spec"].package + "/resources/local-context-support-v1.json"
    value = json.loads(release["files"][name])
    if damage == "extra-field":
        value["ready"] = True
    elif damage == "abi":
        value["execution_abi"] = "different-abi"
    elif damage == "source":
        value["control_source"] = "platform"
    elif damage == "schema-id":
        value["context_schema_id"] = "https://wrong.example/schema"
    elif damage == "schema-digest":
        value["context_schema_sha256"] = "0" * 64
    content = canonical_json_bytes(value)
    if damage == "duplicate-key":
        content = content[:-1] + b',"control_source":"local"}'
    _replace_support(release, content, declared=damage != "undeclared")
    with pytest.raises(ArtifactVerificationError):
        _prepare(release)
    parent = release["quarantine_parent"]
    assert not parent.exists() or not any(parent.iterdir())


def test_prepared_material_does_not_unlock_the_local_loader(release):
    from custos_toolkit.contracts.strategy_execution import deep_freeze_json

    from custos.engines.nautilus.runtime_loader import (
        NautilusRuntimeEntryPointError,
        NautilusRuntimeEntryPointLoaderV1,
    )
    from tests.test_local_strategy_context import local_context

    prepared = _prepare(release)
    try:
        with pytest.raises(NautilusRuntimeEntryPointError, match="verified local compatibility"):
            NautilusRuntimeEntryPointLoaderV1().load(
                activation_root=prepared.quarantine.root,
                entry_point=prepared.manifest.entry_point,
                effective_config=deep_freeze_json({}),
                execution_context=local_context(),
            )
    finally:
        prepared.quarantine.destroy()


@pytest.mark.parametrize(
    "damage",
    [
        "schema-version",
        "unknown-field",
        "missing-toolkit",
        "duplicate-singleton",
        "toolkit_wheel_sha256",
        "toolkit_sbom_sha256",
        "contract_asset_index_sha256",
        "strategy_sbom_sha256",
        "strategy_source_tree_sha256",
        "producer_repository",
        "strategy_source_commit",
        "execution_abi_schema_sha256",
        "build_lock_sha256",
    ],
)
def test_owner_bom_structure_and_all_artifact_reference_bindings_are_required(release, damage):
    bom = json.loads(release["release_bom_bytes"])
    if damage == "schema-version":
        bom["schema_version"] = "unsupported-bom-version"
    elif damage == "unknown-field":
        bom["ready"] = True
    elif damage == "missing-toolkit":
        bom["members"] = [member for member in bom["members"] if member["role"] != "nautilus_wheel"]
    elif damage == "duplicate-singleton":
        member = dict(
            next(member for member in bom["members"] if member["role"] == "base_contracts_wheel")
        )
        member["name"] = "another-base-wheel.whl"
        bom["members"].append(member)
    elif damage == "producer_repository":
        bom[damage] = "https://github.com/another-owner/another-producer"
    elif damage == "strategy_source_commit":
        bom[damage] = "0" * 40
    else:
        bom[damage] = "0" * 64
    release["release_bom_bytes"] = canonical_json_bytes(bom)
    with pytest.raises(ArtifactVerificationError):
        _prepare(release)

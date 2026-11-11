"""Candidate measurement is bound to build-selected identity, never local qualification."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_native_candidate_build import source_repository
from tests.test_native_runtime_artifact import installation

__all__ = ["source_repository", "installation"]

ROOT = Path(__file__).resolve().parents[1]
BUILD = {
    "source_revision": "50e5bd4dd12bc6a8e9a866bbeae55f7e3d89670b",
    "crate_version": "0.64.0",
    "profile": "dev",
    "features": ["extension-module", "arrow"],
}


def test_fixed_candidate_export_embeds_its_approved_native_build(
    source_repository, tmp_path, monkeypatch
):
    from scripts import build_native_candidate as builder

    recipe = json.loads(
        (ROOT / "packaging/runtime-profiles/native-capture-darwin7-v1.json").read_text()
    )
    recipe["engine"]["native_build"] = BUILD
    recipe_root = tmp_path / "recipe"
    destination = recipe_root / "packaging/runtime-profiles/native-capture-darwin7-v1.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(recipe))
    monkeypatch.setattr(builder, "ROOT", recipe_root)
    exported = builder.export_candidate_source(
        repository_root=source_repository,
        source_commit=builder._run(["git", "rev-parse", "HEAD"], cwd=source_repository).strip(),
        profile_id="native-capture-darwin7-v1",
        output_root=tmp_path / "candidate",
    )
    embedded = json.loads(
        (exported.source_root / "src/custos/core/_native_build_descriptor_v1.json").read_text()
    )
    assert embedded == {
        "schema_version": 1,
        "profile_id": recipe["profile_id"],
        "source_commit": exported.source_commit,
        "engine": recipe["engine"],
    }
    assert embedded["engine"]["native_build"] == BUILD


@pytest.fixture
def bundle(installation, tmp_path, monkeypatch):
    from custos.core import native_runtime_bundle as subject
    from custos.engines.nautilus import native_runtime_artifact as measurement
    from tests.test_native_runtime_artifact import DIST_INFO

    wheel, installed, contents, archive, _ = installation
    tag = "cp312-cp312-macosx_11_0_arm64"
    contents[DIST_INFO + "/WHEEL"] = (
        f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n\n".encode()
    )
    (installed / DIST_INFO / "WHEEL").write_bytes(contents[DIST_INFO + "/WHEEL"])
    monkeypatch.setattr(measurement, "_supported_tags", lambda: frozenset({tag}))
    digest = archive()
    root = tmp_path / "bundle"
    (root / "native").mkdir(parents=True)
    archived = root / "native" / f"nautilus_trader-2.0.0rc5+papertrade.7-{tag}.whl"
    archived.write_bytes(wheel.read_bytes())
    engine = {
        "distribution": "nautilus-trader",
        "version": "2.0.0rc5+papertrade.7",
        "artifact_sha256": digest,
        "abi": "cp312",
        "platform": "macosx_11_0_arm64",
        "native_build": BUILD,
    }
    descriptor = {
        "schema_version": 1,
        "profile_id": "native-capture-darwin7-v1",
        "source_commit": "a" * 40,
        "engine": engine,
    }
    resource = root / "source/src/custos/core/_native_build_descriptor_v1.json"
    resource.parent.mkdir(parents=True)
    payload = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    resource.write_bytes(payload)
    inventory = json.dumps(
        {
            "src/custos/core/_native_build_descriptor_v1.json": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        }
    ).encode()
    monkeypatch.setattr(subject, "_installed_descriptor", lambda: descriptor, raising=False)
    profile = {
        "schema_version": "alephain.custos.native-candidate-source-profile.v1",
        "profile_id": "native-capture-darwin7-v1",
        "source_repository": "https://github.com/the-alephain-guild/custos",
        "source_commit": "a" * 40,
        "source_date_epoch": 1793865015,
        "source_root": "source",
        "source_inventory_path": "source-inventory.json",
        "source_inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        "source_version": "0.1.0.dev20261105+nt.papertrade.7",
        "engine": {**engine, "wheel_path": "native/" + archived.name},
        "distributions": {
            name: {
                "name": name,
                "version": "0.1.0.dev20261105",
                "wheel_path": f"wheels/{name}.whl",
                "sha256": "e" * 64,
                "size_bytes": 1,
                "metadata_sha256": "d" * 64,
                "requires_python": ">=3.12",
                "requires_dist": [],
            }
            for name in (
                "custos-runner",
                "custos-strategy-toolkit",
                "custos-strategy-toolkit-nautilus",
                "custos-strategy-publisher",
            )
        },
        "engine_definitions": {
            f"source/packages/{package}/src/{module}/engine_profile.py": {"sha256": "f" * 64}
            for package, module in (
                ("custos-strategy-toolkit", "custos_toolkit/contracts"),
                ("custos-strategy-publisher", "custos_strategy_publisher"),
            )
        },
        "acceptance": {"local_candidate": True, "rc_published": False, "control_ready": False},
    }
    (root / "source-inventory.json").write_bytes(inventory)
    (root / "candidate-source-profile.json").write_text(json.dumps(profile))
    return root, profile, descriptor, archived


async def test_bundle_reuses_actual_archive_measurement_without_qualifying_a_model(
    bundle, monkeypatch
):
    from custos.core import native_runtime_bundle as subject
    from custos.engines.nautilus import native_runtime_artifact as measurement

    root, _, descriptor, _ = bundle
    calls = []
    real = measurement.measure_native_runtime_archive

    async def measured(*args, **kwargs):
        calls.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(measurement, "measure_native_runtime_archive", measured)
    result = await subject.measure_candidate_native_bundle(root)
    assert result.artifact_digest == descriptor["engine"]["artifact_sha256"]
    assert result.crate_version == "0.64.0"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "build",
        "abi",
        "extra",
        "escape",
        "missing",
        "symlink",
        "writable",
        "payload",
        "source-copy",
    ],
)
async def test_bundle_cannot_substitute_another_source_or_native_payload(
    bundle, installation, change
):
    from custos.core import native_runtime_bundle as subject
    from custos.engines.nautilus.native_runtime_artifact import NativeRuntimeMeasurementError
    from tests.test_native_runtime_artifact import NATIVE

    root, profile, _, archive = bundle
    if change == "source":
        profile["source_commit"] = "b" * 40
    elif change == "build":
        profile["engine"]["native_build"] = {**BUILD, "crate_version": "1.0.0"}
    elif change == "abi":
        profile["engine"]["abi"] = "cp311"
    elif change == "extra":
        profile["engine"]["override"] = True
    elif change == "escape":
        profile["engine"]["wheel_path"] = "../outside.whl"
    elif change == "missing":
        archive.unlink()
    elif change == "symlink":
        other = root.parent / "outside.whl"
        archive.rename(other)
        archive.symlink_to(other)
    elif change == "writable":
        archive.chmod(0o666)
    elif change == "source-copy":
        (root / "source/src/custos/core/_native_build_descriptor_v1.json").unlink()
    else:
        (installation[1] / NATIVE).write_bytes(b"changed native payload")
    (root / "candidate-source-profile.json").write_text(json.dumps(profile))
    with pytest.raises((subject.NativeRuntimeBundleError, NativeRuntimeMeasurementError)):
        await subject.measure_candidate_native_bundle(root)


async def test_released_profile_cannot_be_overridden_by_a_bundle_or_environment(
    bundle, monkeypatch
):
    from custos.core import native_runtime_bundle as subject

    monkeypatch.setattr(subject, "_installed_descriptor", lambda: None)
    monkeypatch.setenv("CUSTOS_NATIVE_CRATE_VERSION", "0.64.0")
    monkeypatch.setenv("CUSTOS_ENGINE_VERSION", "2.0.0rc5+papertrade.7")
    assert await subject.measure_candidate_native_bundle(None) is None
    with pytest.raises(subject.NativeRuntimeBundleError, match="released runtime"):
        await subject.measure_candidate_native_bundle(bundle[0])


async def test_unknown_distribution_is_not_a_closed_candidate_profile(bundle):
    from custos.core import native_runtime_bundle as subject

    root, profile, _, _ = bundle
    profile["distributions"]["unapproved"] = {}
    (root / "candidate-source-profile.json").write_text(json.dumps(profile))
    with pytest.raises(subject.NativeRuntimeBundleError):
        await subject.measure_candidate_native_bundle(root)


@pytest.mark.parametrize("prefix", ["native//", "native/./"])
async def test_bundle_relative_paths_use_the_same_canonical_domain_as_the_producer(bundle, prefix):
    from custos.core import native_runtime_bundle as subject

    root, profile, _, archive = bundle
    profile["engine"]["wheel_path"] = prefix + archive.name
    (root / "candidate-source-profile.json").write_text(json.dumps(profile))
    with pytest.raises(subject.NativeRuntimeBundleError, match="path"):
        await subject.measure_candidate_native_bundle(root)


@pytest.mark.parametrize(
    "field,value", [("local_candidate", 1), ("rc_published", 0), ("control_ready", 0)]
)
async def test_integer_acceptance_flags_do_not_grant_candidate_identity(bundle, field, value):
    from custos.core import native_runtime_bundle as subject

    root, profile, _, _ = bundle
    profile["acceptance"][field] = value
    (root / "candidate-source-profile.json").write_text(json.dumps(profile))
    with pytest.raises(subject.NativeRuntimeBundleError, match="metadata"):
        await subject.measure_candidate_native_bundle(root)


def test_regular_file_replaced_by_a_fifo_cannot_block_the_protected_reader(tmp_path):
    path = tmp_path / "descriptor.json"
    path.write_text("{}")
    program = """
import os,sys
from pathlib import Path
from custos.core import native_runtime_bundle as subject
original=subject._protected_path
def replaced(path, **kwargs):
    checked=original(path, **kwargs)
    checked.unlink()
    os.mkfifo(checked)
    return checked
subject._protected_path=replaced
try:
    subject._read(Path(sys.argv[1]))
except subject.NativeRuntimeBundleError:
    raise SystemExit(0)
raise AssertionError("non-regular input was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(path)], capture_output=True, text=True, timeout=3
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "0" * 40),
        ("source_revision", "a" * 64),
        ("source_revision", "A" * 40),
        ("crate_version", "00.64.0"),
        ("crate_version", "0.64"),
        ("profile", "release"),
        ("features", []),
        ("features", ["extension-module", "extension-module"]),
        ("features", ["arrow"]),
        ("features", ["extension-module", 1]),
    ],
)
def test_native_build_descriptor_has_one_exact_acceptance_domain(field, value):
    from custos.core.native_runtime_bundle import NativeRuntimeBundleError, validate_native_engine

    engine = json.loads(
        (ROOT / "packaging/runtime-profiles/native-capture-darwin7-v1.json").read_text()
    )["engine"]
    engine["native_build"][field] = value
    with pytest.raises(NativeRuntimeBundleError):
        validate_native_engine(engine)


def test_a_local_resource_cannot_override_the_installed_released_toolkit(tmp_path, monkeypatch):
    from custos.core import native_runtime_bundle as subject

    recipe = json.loads(
        (ROOT / "packaging/runtime-profiles/native-capture-darwin7-v1.json").read_text()
    )
    (tmp_path / subject.DESCRIPTOR_RESOURCE).write_bytes(subject.descriptor_bytes(recipe, "a" * 40))
    monkeypatch.setattr(subject, "files", lambda _package: tmp_path)
    with pytest.raises(subject.NativeRuntimeBundleError, match="static engine profile"):
        subject._installed_descriptor()


@pytest.mark.parametrize("resource_kind", ["missing", "directory"])
async def test_candidate_static_identity_requires_a_regular_installed_descriptor(
    tmp_path, monkeypatch, resource_kind
):
    from custos_toolkit.contracts import engine_profile

    from custos.core import native_runtime_bundle as subject

    monkeypatch.setattr(engine_profile, "ENGINE_PROFILE_ID", subject.PROFILE_ID)
    monkeypatch.setattr(engine_profile, "ENGINE_VERSION", "2.0.0rc5+papertrade.7")
    monkeypatch.setattr(subject, "files", lambda _package: tmp_path)
    if resource_kind == "directory":
        (tmp_path / subject.DESCRIPTOR_RESOURCE).mkdir()
    with pytest.raises(subject.NativeRuntimeBundleError, match="descriptor"):
        await subject.measure_candidate_native_bundle(None)


async def test_only_the_exact_released_static_identity_can_omit_the_descriptor(
    tmp_path, monkeypatch
):
    from custos_toolkit.contracts import engine_profile

    from custos.core import native_runtime_bundle as subject

    monkeypatch.setattr(subject, "files", lambda _package: tmp_path)
    assert await subject.measure_candidate_native_bundle(None) is None
    (tmp_path / subject.DESCRIPTOR_RESOURCE).mkdir()
    with pytest.raises(subject.NativeRuntimeBundleError):
        await subject.measure_candidate_native_bundle(None)
    (tmp_path / subject.DESCRIPTOR_RESOURCE).rmdir()
    monkeypatch.setattr(engine_profile, "ENGINE_VERSION", "2.0.0rc5+papertrade.7")
    with pytest.raises(subject.NativeRuntimeBundleError, match="descriptor"):
        await subject.measure_candidate_native_bundle(None)


def test_verified_identity_reaches_the_host_without_a_string_override():
    from custos.cli._daemon import _build_host
    from custos.engines.nautilus.native_opening_inventory import NativeRuntimeArtifactIdentity
    from tests.test_main_host_selection import _host_args

    identity = NativeRuntimeArtifactIdentity("a" * 64, "0.64.0")
    host = _build_host(_host_args(), native_runtime_artifact_identity=identity)
    assert host._native_runtime_artifact_identity is identity


@pytest.mark.parametrize("failure", ["payload", "missing_descriptor", "directory_descriptor"])
async def test_daemon_refuses_bad_measurement_before_creating_outbox_or_tasks(
    tmp_path, monkeypatch, failure
):
    from argparse import Namespace
    from types import SimpleNamespace
    from uuid import UUID

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custos.cli import _daemon as daemon
    from custos.core import native_runtime_bundle as subject
    from custos.core import runtime_admission
    from custos.core.runner_fact import RunnerFactIdentity

    runner = UUID(int=4)
    public = (
        Ed25519PrivateKey.from_private_bytes(bytes([71]) * 32)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    identity = RunnerFactIdentity.from_private_bytes(
        bytes([71]) * 32, "ed25519-" + hashlib.sha256(public).hexdigest()[:32]
    )
    credential = SimpleNamespace(
        private_key_bytes=bytes([71]) * 32,
        machine_key_id=identity.key_id,
        assert_binding=lambda _metadata: None,
    )
    capability = SimpleNamespace(
        tenant_id="tenant-a",
        runner_id=runner,
        key_id=identity.key_id,
        public_key_digest=hashlib.sha256(identity.public_key_bytes).hexdigest(),
        binding_status="validated",
        capability_manifest={},
    )
    args = Namespace(
        ready_file=tmp_path / "ready",
        runner_toml_path=tmp_path / "runner.toml",
        machine_vault=tmp_path / "machine",
        runner_capability=tmp_path / "capability.json",
        tenant_id="tenant-a",
        runner_id=str(runner),
        enabled_modes=["sandbox"],
        nats_transport_vault_dir=tmp_path / "transport",
        engine="nautilus",
        native_runtime_bundle=tmp_path / "bundle",
        runner_fact_outbox=tmp_path / "outbox.sqlite3",
    )
    monkeypatch.setattr(runtime_admission, "load_runtime_admission", lambda _args: None)
    monkeypatch.setattr(
        daemon.RunnerToml, "read", lambda _path: SimpleNamespace(backend_url="http://unused")
    )
    monkeypatch.setattr(daemon.MachineCredentialVault, "load", lambda _self: credential)
    monkeypatch.setattr(
        daemon,
        "MachineCredentialHttpClient",
        lambda *_: SimpleNamespace(verify_active=lambda: None),
    )
    monkeypatch.setattr(
        daemon.RunnerNatsTransportSet, "load", lambda *_: SimpleNamespace(active=lambda _mode: None)
    )
    monkeypatch.setattr(daemon, "_transport_profile_for_mode", lambda *_: object())
    monkeypatch.setattr(daemon.RunnerCapabilityReceipt, "load", lambda _path: capability)
    monkeypatch.setattr(daemon, "observe_runtime_identity", lambda _env: object())
    monkeypatch.setattr(daemon, "require_declared_runtime", lambda *_args, **_kwargs: None)

    async def refuse(_root):
        raise subject.NativeRuntimeBundleError("measured payload differs")

    def outbox(_path):
        raise AssertionError("outbox created before native measurement")

    if failure == "payload":
        monkeypatch.setattr(subject, "measure_candidate_native_bundle", refuse)
        expected = "measured payload"
    else:
        from custos_toolkit.contracts import engine_profile

        monkeypatch.setattr(engine_profile, "ENGINE_PROFILE_ID", subject.PROFILE_ID)
        monkeypatch.setattr(engine_profile, "ENGINE_VERSION", "2.0.0rc5+papertrade.7")
        monkeypatch.setattr(subject, "files", lambda _package: tmp_path)
        if failure == "directory_descriptor":
            (tmp_path / subject.DESCRIPTOR_RESOURCE).mkdir()
        args.native_runtime_bundle = None
        expected = "descriptor"
    monkeypatch.setattr(daemon, "RunnerFactOutbox", outbox)
    with pytest.raises(subject.NativeRuntimeBundleError, match=expected):
        await daemon.run_daemon(args)

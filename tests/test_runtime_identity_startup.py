"""The signed lane starts only with a capability whose runtime is the running one."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import custos.cli._daemon as daemon
from custos.core import runtime_identity
from custos.core.runner_fact import (
    RunnerFactContractError,
    RunnerFactIdentity,
    _canonical_json_bytes,
    _sha256_hex,
)
from custos.core.runtime_admission import RuntimeAdmission
from custos.core.runtime_identity import RUNTIME_IMAGE_DIGEST_ENV, RuntimeIdentityError

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_RECEIPT_PATH = ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
PRIVATE_KEY_BYTES = bytes(range(1, 33))
ENGINE_VERSION = "2.0.0rc5+sodex.2"
IMAGE_DIGEST = "sha256:" + "a" * 64
SOURCE_REVISION = "b" * 40


class ReachedFactOutbox(Exception):
    """The startup passed every capability check and went on to open the outbox."""


def image_runtime(**changes: object) -> dict[str, object]:
    return {
        "distribution": "oci_image",
        "image_digest": IMAGE_DIGEST,
        "source_revision": SOURCE_REVISION,
        "engine": "nautilus",
        "engine_version": ENGINE_VERSION,
        **changes,
    }


def development_runtime(**changes: object) -> dict[str, object]:
    return {
        "distribution": "development",
        "image_digest": None,
        "source_revision": None,
        "engine": "nautilus",
        "engine_version": ENGINE_VERSION,
        **changes,
    }


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import custos.core.runtime_admission as admission

    revision = tmp_path / "source-revision"
    revision.write_text(SOURCE_REVISION + "\n")
    monkeypatch.setattr(admission, "RUNTIME_SOURCE_REVISION_FILE", revision)
    monkeypatch.setattr(runtime_identity, "installed_engine_version", lambda: ENGINE_VERSION)
    monkeypatch.delenv(RUNTIME_IMAGE_DIGEST_ENV, raising=False)

    golden = json.loads(CAPABILITY_RECEIPT_PATH.read_text(encoding="utf-8"))
    identity = RunnerFactIdentity.from_private_bytes(PRIVATE_KEY_BYTES, golden["key_id"])
    credential = SimpleNamespace(
        private_key_bytes=PRIVATE_KEY_BYTES,
        machine_key_id=golden["key_id"],
        assert_binding=lambda metadata: None,
    )
    monkeypatch.setattr(
        daemon,
        "RunnerToml",
        SimpleNamespace(read=lambda path: SimpleNamespace(backend_url="https://crucible.invalid")),
    )
    monkeypatch.setattr(
        daemon, "MachineCredentialVault", lambda path: SimpleNamespace(load=lambda: credential)
    )
    monkeypatch.setattr(
        daemon,
        "MachineCredentialHttpClient",
        lambda url, credential: SimpleNamespace(verify_active=lambda: None),
    )

    def outbox(path: Path) -> None:
        raise ReachedFactOutbox

    monkeypatch.setattr(daemon, "RunnerFactOutbox", outbox)
    assert identity.key_id == golden["key_id"]

    def run(runtime: object, *, admission_result: RuntimeAdmission | None = None) -> None:
        document = copy.deepcopy(golden)
        manifest = document["capability_manifest"]
        if runtime is None:
            manifest.pop("runtime", None)
        else:
            manifest["runtime"] = runtime
        document["manifest_digest"] = _sha256_hex(_canonical_json_bytes(manifest))
        receipt = tmp_path / "runner-capability.json"
        receipt.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(admission, "load_runtime_admission", lambda args: admission_result)
        args = argparse.Namespace(
            ready_file=tmp_path / "ready.json",
            runner_toml_path=tmp_path / "runner.toml",
            machine_vault=tmp_path / "vault",
            development_local_nats_url="nats://127.0.0.1:4222",
            enabled_modes=("sandbox",),
            tenant_id=document["tenant_id"],
            runner_id=document["runner_id"],
            runner_capability=receipt,
            runner_fact_outbox=tmp_path / "outbox.db",
        )
        daemon.asyncio.run(daemon.run_daemon(args))

    return run


def test_source_runner_starts_with_its_development_runtime(startup) -> None:
    with pytest.raises(ReachedFactOutbox):
        startup(development_runtime())


def test_image_runner_starts_with_its_image_runtime(startup, monkeypatch) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    with pytest.raises(ReachedFactOutbox):
        startup(image_runtime())


def test_receipt_without_runtime_refuses_the_signed_lane(startup) -> None:
    with pytest.raises(RunnerFactContractError, match="runtime_identity_missing"):
        startup(None)


@pytest.mark.parametrize(
    ("declared", "field"),
    [
        (image_runtime(image_digest="sha256:" + "c" * 64), "image_digest"),
        (image_runtime(source_revision="c" * 40), "source_revision"),
        (image_runtime(engine_version="2.0.0rc6"), "engine_version"),
        (development_runtime(), "distribution"),
    ],
)
def test_runtime_other_than_the_running_one_refuses_the_signed_lane(
    startup, monkeypatch, declared: dict[str, object], field: str
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_mismatch") as raised:
        startup(declared)
    assert field in str(raised.value)
    assert "publish the capability again" in str(raised.value)


def test_live_admission_starts_only_with_the_admitted_image(startup, monkeypatch) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    admitted = RuntimeAdmission(IMAGE_DIGEST, SOURCE_REVISION, "d" * 64)
    with pytest.raises(ReachedFactOutbox):
        startup(image_runtime(), admission_result=admitted)


def test_live_admission_refuses_a_capability_for_another_image(startup, monkeypatch) -> None:
    other = "sha256:" + "c" * 64
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, other)
    admitted = RuntimeAdmission(IMAGE_DIGEST, SOURCE_REVISION, "d" * 64)
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_not_admitted"):
        startup(image_runtime(image_digest=other), admission_result=admitted)


def test_live_admission_refuses_a_development_capability(startup) -> None:
    admitted = RuntimeAdmission(IMAGE_DIGEST, SOURCE_REVISION, "d" * 64)
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_not_admitted"):
        startup(development_runtime(), admission_result=admitted)

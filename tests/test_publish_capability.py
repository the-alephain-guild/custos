"""``arx-runner publish-capability`` declares the runtime of the process that publishes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.cli.subcommands import publish_capability
from custos.core import runtime_identity
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    _canonical_json_bytes,
    capability_binding_evidence_digest,
    normalize_capability_scope_bindings,
)
from custos.core.runtime_identity import RUNTIME_IMAGE_DIGEST_ENV

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_MANIFEST_PATH = ROOT / "docs/authority/runner-fact-capability-manifest-v1.json"
PRIVATE_KEY_BYTES = bytes(range(1, 33))
TENANT_ID = "acme"
RUNNER_ID = "10000000-0000-4000-8000-000000000001"
ENGINE_VERSION = "2.0.0rc5+sodex.2"
IMAGE_DIGEST = "sha256:" + "a" * 64
SOURCE_REVISION = "b" * 40


def image_runtime(**changes: object) -> dict[str, object]:
    return {
        "distribution": "oci_image",
        "image_digest": IMAGE_DIGEST,
        "source_revision": SOURCE_REVISION,
        "engine": "nautilus",
        "engine_version": ENGINE_VERSION,
        **changes,
    }


def development_runtime() -> dict[str, object]:
    return {
        "distribution": "development",
        "image_digest": None,
        "source_revision": None,
        "engine": "nautilus",
        "engine_version": ENGINE_VERSION,
    }


def _key_id() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = (
        Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY_BYTES)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return f"ed25519-{hashlib.sha256(public).hexdigest()[:32]}"


class Publication:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.posted: list[dict] = []
        self.revision_file = tmp_path / "source-revision"
        self.revision_file.write_text(SOURCE_REVISION + "\n")
        self.receipt_path = tmp_path / "runner-capability.json"

    def template(self) -> dict:
        manifest = json.loads(CAPABILITY_MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest.pop("runtime", None)
        return manifest

    def run(self, manifest: dict) -> int:
        path = self.tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return publish_capability.run(
            argparse.Namespace(
                manifest=path,
                runner_toml=self.tmp_path / "runner.toml",
                authority_path=self.receipt_path,
                idempotency_key=None,
                capability_version_id=None,
                capability_version=None,
            )
        )

    def client(self, url: str, credential: object) -> SimpleNamespace:
        def post(path: str, body: dict, *, correlation_id: object) -> dict:
            self.posted.append(body)
            bindings = normalize_capability_scope_bindings(body["capability_manifest"])
            return {
                "runner_id": RUNNER_ID,
                "capability_version_id": str(body["capability_version_id"]),
                "capability_version": body["capability_version"],
                "manifest_digest": body["manifest_digest"],
                "key_id": body["key_id"],
                "key_version": 1,
                "public_key_digest": hashlib.sha256(
                    base64.b64decode(body["public_key_base64"])
                ).hexdigest(),
                "valid_from": "2026-10-03T00:00:00Z",
                "key_authority_digest": "c" * 64,
                "proof_digest": "d" * 64,
                "binding_status": "validated",
                "binding_evidence_digest": capability_binding_evidence_digest(
                    TENANT_ID, RUNNER_ID, bindings
                ),
                "restart_required": True,
            }

        return SimpleNamespace(verify_active=lambda: None, post=post)


@pytest.fixture
def publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Publication:
    import custos.core.runtime_admission as admission

    state = Publication(tmp_path)
    monkeypatch.setattr(admission, "RUNTIME_SOURCE_REVISION_FILE", state.revision_file)
    monkeypatch.setattr(runtime_identity, "installed_engine_version", lambda: ENGINE_VERSION)
    monkeypatch.delenv(RUNTIME_IMAGE_DIGEST_ENV, raising=False)
    runner = SimpleNamespace(
        tenant_id=TENANT_ID,
        runner_id=RUNNER_ID,
        backend_url="https://crucible.invalid",
        machine_vault_path=str(tmp_path / "vault"),
    )
    credential = SimpleNamespace(
        private_key_bytes=PRIVATE_KEY_BYTES,
        machine_key_id=_key_id(),
        assert_binding=lambda metadata: None,
    )
    monkeypatch.setattr(publish_capability, "RunnerToml", SimpleNamespace(read=lambda path: runner))
    monkeypatch.setattr(publish_capability, "require_attested", lambda record, action: record)
    monkeypatch.setattr(
        publish_capability,
        "MachineCredentialVault",
        lambda path: SimpleNamespace(load=lambda: credential),
    )
    monkeypatch.setattr(publish_capability, "MachineCredentialHttpClient", state.client)
    return state


def _digest(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()


def test_image_publication_declares_the_configured_image_and_observed_engine(
    publication: Publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    assert publication.run(publication.template()) == 0

    [body] = publication.posted
    assert body["capability_manifest"]["runtime"] == image_runtime()
    assert body["manifest_digest"] == _digest(body["capability_manifest"])
    without_runtime = dict(body["capability_manifest"])
    without_runtime.pop("runtime")
    assert body["manifest_digest"] != _digest(without_runtime)
    receipt = RunnerCapabilityReceipt.load(publication.receipt_path)
    assert receipt.capability_manifest["runtime"] == image_runtime()
    assert receipt.manifest_digest == body["manifest_digest"]
    assert receipt.runner_id == UUID(RUNNER_ID)


@pytest.mark.parametrize("content", [None, "unversioned\n"])
def test_image_publication_without_a_commit_revision_fails_and_writes_nothing(
    publication: Publication, monkeypatch: pytest.MonkeyPatch, capsys, content: str | None
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    if content is None:
        publication.revision_file.unlink()
    else:
        publication.revision_file.write_text(content)
    assert publication.run(publication.template()) == 1
    assert publication.posted == []
    assert not publication.receipt_path.exists()
    assert "runtime_source_revision" in capsys.readouterr().err


def test_source_publication_declares_a_development_runtime(publication: Publication) -> None:
    assert publication.run(publication.template()) == 0
    [body] = publication.posted
    assert body["capability_manifest"]["runtime"] == development_runtime()
    receipt = json.loads(publication.receipt_path.read_text(encoding="utf-8"))
    assert receipt["capability_manifest"]["runtime"] == development_runtime()


def test_matching_runtime_in_the_manifest_is_signed_unchanged(
    publication: Publication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    manifest = {**publication.template(), "runtime": image_runtime()}
    assert publication.run(manifest) == 0
    [body] = publication.posted
    assert body["capability_manifest"] == manifest
    assert body["manifest_digest"] == _digest(manifest)


@pytest.mark.parametrize(
    "declared",
    [
        image_runtime(image_digest="sha256:" + "e" * 64),
        image_runtime(source_revision="e" * 40),
        image_runtime(engine_version="2.0.0rc6"),
        development_runtime(),
    ],
)
def test_runtime_in_the_manifest_that_differs_from_this_process_is_refused(
    publication: Publication, monkeypatch: pytest.MonkeyPatch, capsys, declared: dict
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, IMAGE_DIGEST)
    assert publication.run({**publication.template(), "runtime": declared}) == 1
    assert publication.posted == []
    assert not publication.receipt_path.exists()
    assert "runtime_identity_mismatch" in capsys.readouterr().err


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63],
)
def test_malformed_configured_digest_fails_publication(
    publication: Publication, monkeypatch: pytest.MonkeyPatch, capsys, digest: str
) -> None:
    monkeypatch.setenv(RUNTIME_IMAGE_DIGEST_ENV, digest)
    assert publication.run(publication.template()) == 1
    assert publication.posted == []
    assert not publication.receipt_path.exists()
    assert "runtime_image_digest_invalid" in capsys.readouterr().err

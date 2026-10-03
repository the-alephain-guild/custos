"""The runtime identity a Runner observes in its own process and declares in its capability."""

from __future__ import annotations

import copy
import importlib.metadata
import json
from pathlib import Path

import pytest

from custos.core import runtime_identity
from custos.core.runner_fact import RunnerCapabilityReceipt, RunnerFactContractError
from custos.core.runtime_admission import RuntimeAdmission
from custos.core.runtime_identity import (
    RUNTIME_IMAGE_DIGEST_ENV,
    RuntimeIdentityError,
    declare_runtime,
    observe_runtime_identity,
    require_declared_runtime,
    validate_runtime_identity,
)

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_RECEIPT_PATH = ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
ENGINE_VERSION = "2.0.0rc5+sodex.2"
IMAGE_DIGEST = "sha256:" + "a" * 64
SOURCE_REVISION = "b" * 40


@pytest.fixture
def revision_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import custos.core.runtime_admission as admission

    path = tmp_path / "source-revision"
    path.write_text(SOURCE_REVISION + "\n")
    monkeypatch.setattr(admission, "RUNTIME_SOURCE_REVISION_FILE", path)
    return path


@pytest.fixture(autouse=True)
def installed_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_identity, "installed_engine_version", lambda: ENGINE_VERSION)


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


def test_image_runtime_reads_digest_from_configuration_and_checks_the_rest(
    revision_file: Path,
) -> None:
    observed = observe_runtime_identity({RUNTIME_IMAGE_DIGEST_ENV: IMAGE_DIGEST})
    assert observed == image_runtime()


def test_source_runtime_is_a_development_build_with_no_digest_or_revision() -> None:
    assert observe_runtime_identity({}) == development_runtime()


def test_development_ignores_a_revision_file_it_did_not_ask_for(revision_file: Path) -> None:
    assert observe_runtime_identity({"UNRELATED": "1"}) == development_runtime()


@pytest.mark.parametrize("content", [None, "unversioned\n", "b" * 39 + "\n", "B" * 40 + "\n"])
def test_configured_digest_without_a_commit_revision_never_degrades_to_development(
    revision_file: Path, content: str | None
) -> None:
    if content is None:
        revision_file.unlink()
    else:
        revision_file.write_text(content)
    with pytest.raises(RuntimeIdentityError) as raised:
        observe_runtime_identity({RUNTIME_IMAGE_DIGEST_ENV: IMAGE_DIGEST})
    assert raised.value.code in {
        "runtime_source_revision_unavailable",
        "runtime_source_revision_invalid",
    }


def test_revision_file_that_is_a_symlink_is_refused(revision_file: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(SOURCE_REVISION)
    revision_file.unlink()
    revision_file.symlink_to(target)
    with pytest.raises(RuntimeIdentityError, match="runtime_source_revision_unavailable"):
        observe_runtime_identity({RUNTIME_IMAGE_DIGEST_ENV: IMAGE_DIGEST})


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "SHA256:" + "a" * 64,
        "sha256:" + "a" * 64 + "\n",
        "sha512:" + "a" * 64,
    ],
)
def test_malformed_configured_digest_is_refused(revision_file: Path, digest: str) -> None:
    with pytest.raises(RuntimeIdentityError, match="runtime_image_digest_invalid"):
        observe_runtime_identity({RUNTIME_IMAGE_DIGEST_ENV: digest})


def test_missing_engine_package_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    def absent(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(runtime_identity.importlib.metadata, "version", absent)
    with pytest.raises(RuntimeIdentityError, match="runtime_engine_unavailable"):
        observe_runtime_identity({})


def test_engine_version_comes_from_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    pytest.importorskip("nautilus_trader")
    assert runtime_identity.installed_engine_version() == importlib.metadata.version(
        "nautilus-trader"
    )


def test_observed_engine_version_outside_the_consumer_domain_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_identity, "installed_engine_version", lambda: "2.0 local")
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_invalid"):
        observe_runtime_identity({})


@pytest.mark.parametrize(
    "value",
    [
        image_runtime(),
        development_runtime(),
        image_runtime(engine_version="1"),
        image_runtime(engine_version="1.2.3a4.post5.dev6+local_7!8-9"),
        image_runtime(engine_version="x" * 64),
    ],
)
def test_runtime_shapes_the_consumer_accepts(value: dict[str, object]) -> None:
    assert validate_runtime_identity(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "oci_image",
        {k: v for k, v in image_runtime().items() if k != "image_digest"},
        {k: v for k, v in development_runtime().items() if k != "source_revision"},
        {**image_runtime(), "platform": "linux/amd64"},
        image_runtime(distribution="docker"),
        image_runtime(distribution=None),
        image_runtime(image_digest=None),
        image_runtime(source_revision=None),
        image_runtime(image_digest="a" * 64),
        image_runtime(image_digest="sha256:" + "A" * 64),
        image_runtime(image_digest="sha256:" + "a" * 63),
        image_runtime(source_revision="b" * 39),
        image_runtime(source_revision="B" * 40),
        image_runtime(source_revision="unversioned"),
        development_runtime(image_digest=IMAGE_DIGEST),
        development_runtime(source_revision=SOURCE_REVISION),
        development_runtime(image_digest=""),
        image_runtime(engine="sandbox-sim"),
        image_runtime(engine="Nautilus"),
        image_runtime(engine_version=""),
        image_runtime(engine_version="x" * 65),
        image_runtime(engine_version="2.0 rc5"),
        image_runtime(engine_version="2.0/rc5"),
        image_runtime(engine_version="2.0\n"),
        image_runtime(engine_version="\uff12.0"),
        image_runtime(engine_version=2),
        image_runtime(engine_version=None),
    ],
)
def test_runtime_shapes_the_consumer_rejects(value: object) -> None:
    with pytest.raises(RuntimeIdentityError):
        validate_runtime_identity(value)


def test_publication_writes_the_observed_runtime_when_the_manifest_has_none() -> None:
    manifest = {"schema_version": 1, "agent_version": "x"}
    declared = declare_runtime(manifest, development_runtime())
    assert declared == {**manifest, "runtime": development_runtime()}
    assert "runtime" not in manifest


def test_publication_keeps_a_matching_runtime_exactly() -> None:
    manifest = {"schema_version": 1, "runtime": image_runtime()}
    assert declare_runtime(manifest, image_runtime()) == manifest


@pytest.mark.parametrize(
    "declared",
    [
        development_runtime(),
        image_runtime(image_digest="sha256:" + "c" * 64),
        image_runtime(source_revision="c" * 40),
        image_runtime(engine_version="2.0.0rc6"),
        {**image_runtime(), "platform": "linux/amd64"},
        None,
    ],
)
def test_publication_refuses_a_runtime_that_differs_from_this_process(declared: object) -> None:
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_mismatch"):
        declare_runtime({"schema_version": 1, "runtime": declared}, image_runtime())


def test_startup_refuses_a_receipt_without_runtime() -> None:
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_missing"):
        require_declared_runtime({"schema_version": 1}, development_runtime(), admission=None)


@pytest.mark.parametrize(
    ("declared", "field"),
    [
        (image_runtime(image_digest="sha256:" + "c" * 64), "image_digest"),
        (image_runtime(source_revision="c" * 40), "source_revision"),
        (image_runtime(engine_version="2.0.0rc6"), "engine_version"),
        (development_runtime(), "distribution"),
    ],
)
def test_startup_refuses_a_runtime_other_than_the_observed_one(
    declared: dict[str, object], field: str
) -> None:
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_mismatch") as raised:
        require_declared_runtime({"runtime": declared}, image_runtime(), admission=None)
    assert field in str(raised.value)
    assert "publish the capability again" in str(raised.value)


def test_startup_accepts_the_observed_runtime_without_live_admission() -> None:
    require_declared_runtime(
        {"runtime": development_runtime()}, development_runtime(), admission=None
    )
    require_declared_runtime({"runtime": image_runtime()}, image_runtime(), admission=None)


def test_live_admission_requires_the_admitted_image() -> None:
    admitted = RuntimeAdmission(IMAGE_DIGEST, SOURCE_REVISION, "d" * 64)
    require_declared_runtime({"runtime": image_runtime()}, image_runtime(), admission=admitted)
    other = image_runtime(image_digest="sha256:" + "c" * 64)
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_not_admitted"):
        require_declared_runtime({"runtime": other}, other, admission=admitted)
    with pytest.raises(RuntimeIdentityError, match="runtime_identity_not_admitted"):
        require_declared_runtime(
            {"runtime": development_runtime()}, development_runtime(), admission=admitted
        )


def _receipt_with_manifest(tmp_path: Path, mutate) -> Path:
    from custos.core.runner_fact import _canonical_json_bytes, _sha256_hex

    document = json.loads(CAPABILITY_RECEIPT_PATH.read_text(encoding="utf-8"))
    manifest = copy.deepcopy(document["capability_manifest"])
    mutate(manifest)
    document["capability_manifest"] = manifest
    document["manifest_digest"] = _sha256_hex(_canonical_json_bytes(manifest))
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_golden_receipt_declares_a_runtime_the_consumer_accepts() -> None:
    receipt = RunnerCapabilityReceipt.load(CAPABILITY_RECEIPT_PATH)
    validate_runtime_identity(receipt.capability_manifest["runtime"])


def test_receipt_without_runtime_is_refused_on_load(tmp_path: Path) -> None:
    path = _receipt_with_manifest(tmp_path, lambda manifest: manifest.pop("runtime", None))
    with pytest.raises(RunnerFactContractError, match="runtime_identity_missing"):
        RunnerCapabilityReceipt.load(path)


@pytest.mark.parametrize(
    "runtime",
    [
        development_runtime(image_digest=IMAGE_DIGEST),
        {**image_runtime(), "platform": "linux/amd64"},
        image_runtime(engine="sandbox-sim"),
    ],
)
def test_receipt_with_an_invalid_runtime_is_refused_on_load(
    tmp_path: Path, runtime: dict[str, object]
) -> None:
    path = _receipt_with_manifest(tmp_path, lambda manifest: manifest.update(runtime=runtime))
    with pytest.raises(RunnerFactContractError, match="runtime_identity_invalid"):
        RunnerCapabilityReceipt.load(path)

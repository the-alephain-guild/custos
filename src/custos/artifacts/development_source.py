"""Custos-owned verification for sandbox-only content-addressed development artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from custos_toolkit.contracts.strategy_execution import DevelopmentSourceRefV1

DEVELOPMENT_SOURCE_DIGEST_PROFILE = "sha256-canonical-directory-v1"
_DIGEST_DOMAIN = b"CUSTOS-DEVELOPMENT-SOURCE-DIRECTORY-V1\0"
_BUILD_INPUT_FILENAME = "development-build-manifest-v1.json"
_BUILD_SCHEMA = "alephain.strategy-development-build-manifest.v1"
_RECEIPT_SCHEMA = "alephain.strategy-artifact-development-publication-receipt.v1"
_PUBLICATION_KIND = "content-addressed-directory-v1"
_PRODUCER_REPOSITORY = "alchymia-labs/philosophers-stone"
_TOOLKIT_DIGEST_PROFILE = "sha256-canonical-custos-toolkit-source-v1"
_ARTIFACT_ROLES = frozenset({"strategy_manifest", "strategy_wheel"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ENTRY_POINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class DevelopmentSourceVerificationError(ValueError):
    """A development source violates root, mode, immutability, or digest policy."""


@dataclass(frozen=True, slots=True)
class VerifiedDevelopmentSourceV1:
    source_ref: DevelopmentSourceRefV1
    root: Path
    files: Mapping[str, bytes]
    digest_profile: str = DEVELOPMENT_SOURCE_DIGEST_PROFILE


@dataclass(frozen=True, slots=True)
class VerifiedDevelopmentArtifactV1:
    source: VerifiedDevelopmentSourceV1
    publication_receipt: Mapping[str, object]
    publication_receipt_digest: str
    strategy_wheel_path: Path
    strategy_manifest: Mapping[str, object]
    entry_point_group: str
    entry_point: str


def canonical_development_source_digest(files: Mapping[str, bytes]) -> str:
    """Hash a closed relative-path to byte mapping with the Custos V1 profile."""

    if not files:
        raise DevelopmentSourceVerificationError("development source must not be empty")
    digest = hashlib.sha256()
    digest.update(_DIGEST_DOMAIN)
    for relative, content in sorted(files.items(), key=lambda item: item[0].encode("utf-8")):
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in relative
            or relative.endswith("/")
        ):
            raise DevelopmentSourceVerificationError("development source path is unsafe")
        if not isinstance(content, bytes):
            raise DevelopmentSourceVerificationError("development source content must be bytes")
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _stable_snapshot(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise DevelopmentSourceVerificationError(
            "development source root must be one regular directory"
        )
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise DevelopmentSourceVerificationError(
                f"development source contains symlink: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise DevelopmentSourceVerificationError(
                f"development source contains non-file: {relative}"
            )
        before = path.stat(follow_symlinks=False)
        content = path.read_bytes()
        after = path.stat(follow_symlinks=False)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise DevelopmentSourceVerificationError(
                f"development source changed while read: {relative}"
            )
        files[relative] = content
    return files


def verify_development_source(
    source_ref: DevelopmentSourceRefV1,
    *,
    configured_root: Path,
    runtime_mode: str,
) -> VerifiedDevelopmentSourceV1:
    """Resolve and verify a local ref without permitting production fallback."""

    if runtime_mode != "sandbox" or source_ref.trading_mode != "sandbox":
        raise DevelopmentSourceVerificationError(
            "development source is restricted to sandbox runtime"
        )
    if source_ref.promotable:
        raise DevelopmentSourceVerificationError("development source must not be promotable")

    root = configured_root.expanduser().resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise DevelopmentSourceVerificationError(
            "configured development artifact root must be one regular directory"
        )
    raw_source = Path(source_ref.source_path).expanduser()
    if not raw_source.is_absolute() or raw_source.is_symlink():
        raise DevelopmentSourceVerificationError(
            "development source path must be absolute and not a symlink"
        )
    source = raw_source.resolve(strict=True)
    try:
        source.relative_to(root)
    except ValueError as error:
        raise DevelopmentSourceVerificationError(
            "development source escapes the configured artifact root"
        ) from error
    if source.parent.name != "sha256" or source.name != source_ref.source_sha256:
        raise DevelopmentSourceVerificationError(
            "development source coordinate is not digest-addressed"
        )
    files = _stable_snapshot(source)
    actual = canonical_development_source_digest(files)
    if actual != source_ref.source_sha256:
        raise DevelopmentSourceVerificationError("development source digest differs")
    return VerifiedDevelopmentSourceV1(
        source_ref=source_ref,
        root=source,
        files=MappingProxyType(files),
    )


def verify_development_artifact(
    source_ref: DevelopmentSourceRefV1,
    *,
    publication_receipt_digest: str,
    configured_root: Path,
    runtime_mode: str,
) -> VerifiedDevelopmentArtifactV1:
    source = verify_development_source(
        source_ref,
        configured_root=configured_root,
        runtime_mode=runtime_mode,
    )
    if _SHA256.fullmatch(publication_receipt_digest) is None:
        raise DevelopmentSourceVerificationError("publication receipt digest is invalid")
    receipt_path = (
        configured_root.expanduser().resolve(strict=True)
        / "receipts"
        / "sha256"
        / f"{publication_receipt_digest}.json"
    )
    receipt_bytes = _stable_file(receipt_path, "development publication receipt")
    if hashlib.sha256(receipt_bytes).hexdigest() != publication_receipt_digest:
        raise DevelopmentSourceVerificationError("publication receipt bytes differ")
    receipt = _canonical_object(receipt_bytes, "development publication receipt")
    _exact_keys(
        receipt,
        {
            "schema_version",
            "publication_kind",
            "source_digest_profile",
            "producer_repository",
            "strategy_coordinate",
            "source_sha256",
            "build_manifest_sha256",
            "strategy_source_tree_sha256",
            "toolkit_source_sha256",
            "toolkit_version",
            "trading_mode",
            "promotable",
            "external_publication_completed",
        },
        "development publication receipt",
    )
    if (
        receipt["schema_version"] != _RECEIPT_SCHEMA
        or receipt["publication_kind"] != _PUBLICATION_KIND
        or receipt["source_digest_profile"] != DEVELOPMENT_SOURCE_DIGEST_PROFILE
        or receipt["producer_repository"] != _PRODUCER_REPOSITORY
        or receipt["source_sha256"] != source_ref.source_sha256
        or receipt["trading_mode"] != "sandbox"
        or receipt["promotable"] is not False
        or receipt["external_publication_completed"] is not False
        or not isinstance(receipt["strategy_coordinate"], str)
        or not receipt["strategy_coordinate"]
        or not isinstance(receipt["toolkit_version"], str)
        or not receipt["toolkit_version"]
    ):
        raise DevelopmentSourceVerificationError("publication receipt authority differs")
    for field in (
        "build_manifest_sha256",
        "strategy_source_tree_sha256",
        "toolkit_source_sha256",
    ):
        value = receipt[field]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise DevelopmentSourceVerificationError(f"receipt {field} is invalid")

    manifest_bytes = source.files.get(_BUILD_INPUT_FILENAME)
    if manifest_bytes is None:
        raise DevelopmentSourceVerificationError("development build input is absent")
    if hashlib.sha256(manifest_bytes).hexdigest() != receipt["build_manifest_sha256"]:
        raise DevelopmentSourceVerificationError("build input differs from receipt")
    manifest = _canonical_object(manifest_bytes, "development build manifest")
    _exact_keys(
        manifest,
        {
            "artifacts",
            "entry_point_group",
            "entry_point_name",
            "producer_repository",
            "schema_version",
            "strategy_coordinate",
            "strategy_source_tree_sha256",
            "toolkit_source_digest_profile",
            "toolkit_source_sha256",
            "toolkit_version",
        },
        "development build manifest",
    )
    if (
        manifest["schema_version"] != _BUILD_SCHEMA
        or manifest["producer_repository"] != receipt["producer_repository"]
        or manifest["strategy_coordinate"] != receipt["strategy_coordinate"]
        or manifest["strategy_source_tree_sha256"] != receipt["strategy_source_tree_sha256"]
        or manifest["toolkit_source_sha256"] != receipt["toolkit_source_sha256"]
        or manifest["toolkit_version"] != receipt["toolkit_version"]
        or manifest["toolkit_source_digest_profile"] != _TOOLKIT_DIGEST_PROFILE
    ):
        raise DevelopmentSourceVerificationError("development build authority differs")
    raw_artifacts = manifest["artifacts"]
    if not isinstance(raw_artifacts, Mapping) or set(raw_artifacts) != _ARTIFACT_ROLES:
        raise DevelopmentSourceVerificationError("development artifact set differs")
    artifacts: dict[str, tuple[Path, bytes]] = {}
    allowed = {_BUILD_INPUT_FILENAME}
    for role, raw in raw_artifacts.items():
        if not isinstance(raw, Mapping):
            raise DevelopmentSourceVerificationError("development artifact is invalid")
        _exact_keys(
            raw,
            {"path", "sha256", "size_bytes"},
            "development artifact",
        )
        relative = raw["path"]
        digest = raw["sha256"]
        size = raw["size_bytes"]
        if (
            not isinstance(role, str)
            or role not in _ARTIFACT_ROLES
            or not isinstance(relative, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            raise DevelopmentSourceVerificationError("development artifact differs")
        safe = _safe_relative(relative)
        content = source.files.get(safe)
        if content is None or len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise DevelopmentSourceVerificationError("development artifact bytes differ")
        allowed.add(safe)
        artifacts[role] = (source.root.joinpath(*PurePosixPath(safe).parts), content)
    if set(source.files) != allowed:
        raise DevelopmentSourceVerificationError("development artifact file set is open")

    wheel_path, _ = artifacts["strategy_wheel"]
    _, strategy_manifest_bytes = artifacts["strategy_manifest"]
    strategy_manifest = _canonical_object(strategy_manifest_bytes, "strategy manifest")
    entry_group = manifest.get("entry_point_group")
    entry_point = manifest.get("entry_point_name")
    if (
        not wheel_path.name.endswith(".whl")
        or not isinstance(entry_group, str)
        or not entry_group
        or not isinstance(entry_point, str)
        or _ENTRY_POINT.fullmatch(entry_point) is None
        or strategy_manifest.get("schema_version") != 1
        or strategy_manifest.get("engine") != "nautilus"
        or strategy_manifest.get("entry_point_group") != entry_group
        or strategy_manifest.get("entry_point") != entry_point
        or strategy_manifest.get("base_contracts_version") != receipt["toolkit_version"]
        or strategy_manifest.get("engine_toolkit_version") != receipt["toolkit_version"]
    ):
        raise DevelopmentSourceVerificationError("development execution metadata differs")
    return VerifiedDevelopmentArtifactV1(
        source=source,
        publication_receipt=MappingProxyType(dict(receipt)),
        publication_receipt_digest=publication_receipt_digest,
        strategy_wheel_path=wheel_path,
        strategy_manifest=MappingProxyType(dict(strategy_manifest)),
        entry_point_group=entry_group,
        entry_point=entry_point,
    )


def _stable_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DevelopmentSourceVerificationError(f"{label} must be one regular file")
    before = path.stat(follow_symlinks=False)
    content = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DevelopmentSourceVerificationError(f"{label} changed while read")
    return content


def _canonical_object(content: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise DevelopmentSourceVerificationError(f"{label} has duplicate keys")
            value[key] = item
        return value

    try:
        value = json.loads(content, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DevelopmentSourceVerificationError(f"{label} must be JSON") from error
    if not isinstance(value, dict):
        raise DevelopmentSourceVerificationError(f"{label} must be an object")
    canonical = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if canonical != content:
        raise DevelopmentSourceVerificationError(f"{label} must be canonical JSON")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise DevelopmentSourceVerificationError(f"{label} fields differ")


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or value.endswith("/")
    ):
        raise DevelopmentSourceVerificationError("development artifact path is unsafe")
    return path.as_posix()


__all__ = [
    "DEVELOPMENT_SOURCE_DIGEST_PROFILE",
    "DevelopmentSourceVerificationError",
    "VerifiedDevelopmentArtifactV1",
    "VerifiedDevelopmentSourceV1",
    "canonical_development_source_digest",
    "verify_development_artifact",
    "verify_development_source",
]

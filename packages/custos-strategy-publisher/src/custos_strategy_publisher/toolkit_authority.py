"""Exact-byte consumer for the immutable Custos toolkit authority V1."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from .model import ENGINE_VERSION
from .producer import CustosContractAuthorityV1

# Custos keeps each toolkit release candidate's authority receipt, with a sha256
# sidecar, under this path; the publisher reads it from the Custos checkout it runs
# from rather than from a copy each producer would have to keep.
AUTHORITY_RECEIPT_PATH = "docs/authority/receipts/custos-toolkit-{release}-authority-v1.json"
OWNER_REPOSITORY = "tesseract-trading/custos"
AUTHORITY_REPOSITORY = "https://github.com/the-alephain-guild/custos"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CANDIDATE_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)$")


class ToolkitAuthorityError(ValueError):
    """Custos external evidence is absent, mutable, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ExternalAuthorityReceiptV1:
    path: str
    sha256: str
    size_bytes: int
    content: bytes


@dataclass(frozen=True, slots=True)
class RegistryArtifactV1:
    role: str
    title: str
    media_type: str
    digest: str
    size_bytes: int
    source_coordinate: str | None = None

    @property
    def sha256(self) -> str:
        return self.digest.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class CustosToolkitAuthorityV1:
    owner_repository: str
    authority_repository: str
    authority_commit: str
    source_commit: str
    candidate_version: str
    registry: str
    repository: str
    manifest_digest: str
    manifest_size_bytes: int
    authority_receipt: ExternalAuthorityReceiptV1
    publication_config: RegistryArtifactV1
    publication_artifacts: tuple[RegistryArtifactV1, ...]
    contract_asset_index: RegistryArtifactV1
    base_contracts_wheel: RegistryArtifactV1
    nautilus_wheel: RegistryArtifactV1
    base_contracts_sbom: RegistryArtifactV1
    nautilus_sbom: RegistryArtifactV1

    @property
    def oci_coordinate(self) -> str:
        return f"{self.registry}/{self.repository}@{self.manifest_digest}"


def _strict_json(content: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ToolkitAuthorityError(f"{label} contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        parsed = json.loads(
            content,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ToolkitAuthorityError(f"{label} contains non-finite number: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ToolkitAuthorityError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise ToolkitAuthorityError(f"{label} must be an object")
    return cast(dict[str, object], parsed)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ToolkitAuthorityError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ToolkitAuthorityError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ToolkitAuthorityError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolkitAuthorityError(f"{label} must be a positive integer")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ToolkitAuthorityError(f"{label} fields differ")


def _safe_path(repo_root: Path, value: str) -> Path:
    logical = PurePosixPath(value)
    if logical.is_absolute() or not logical.parts or ".." in logical.parts or "\\" in value:
        raise ToolkitAuthorityError("external authority receipt path is unsafe")
    current = repo_root
    for part in logical.parts:
        current /= part
        if current.is_symlink():
            raise ToolkitAuthorityError("external authority receipt cannot be a symlink")
    return current


def _load_receipt(custos_root: Path, candidate_version: str) -> ExternalAuthorityReceiptV1:
    release = _CANDIDATE_RE.fullmatch(candidate_version)
    if release is None:
        raise ToolkitAuthorityError(
            f"toolkit version {candidate_version!r} is not a release candidate with a receipt"
        )
    path = AUTHORITY_RECEIPT_PATH.format(release=release.group(1))
    physical = _safe_path(custos_root, path)
    sidecar = _safe_path(custos_root, f"{path}.sha256")
    if not physical.is_file() or not sidecar.is_file():
        raise ToolkitAuthorityError(f"Custos has no authority receipt for {candidate_version}")
    content = physical.read_bytes()
    recorded = sidecar.read_text(encoding="ascii").split()
    digest = hashlib.sha256(content).hexdigest()
    if not recorded or recorded[0] != digest:
        raise ToolkitAuthorityError("authority receipt bytes differ from its sha256 sidecar")
    return ExternalAuthorityReceiptV1(
        path=path,
        sha256=digest,
        size_bytes=len(content),
        content=content,
    )


def _publication_artifact(
    receipt: Mapping[str, object],
    *,
    role: str,
    title: str,
) -> RegistryArtifactV1:
    publication = _mapping(receipt.get("publication_receipt"), "publication receipt")
    matches = [
        _mapping(raw, "publication receipt layer")
        for raw in _array(publication.get("layers"), "publication receipt layers")
        if _mapping(raw, "publication receipt layer").get("role") == role
        and _mapping(raw, "publication receipt layer").get("title") == title
    ]
    if len(matches) != 1:
        raise ToolkitAuthorityError(f"publication artifact differs: {role}")
    layer = matches[0]
    digest = _string(layer.get("digest"), f"{role} digest")
    if _OCI_DIGEST_RE.fullmatch(digest) is None:
        raise ToolkitAuthorityError(f"publication artifact digest differs: {role}")
    return RegistryArtifactV1(
        role=role,
        title=title,
        media_type=_string(layer.get("media_type"), f"{role} media type"),
        digest=digest,
        size_bytes=_integer(layer.get("size_bytes"), f"{role} size"),
        source_coordinate=_string(layer.get("source_coordinate"), f"{role} source coordinate"),
    )


def _publication_descriptor(
    value: Mapping[str, object],
    label: str,
) -> RegistryArtifactV1:
    digest = _string(value.get("digest"), f"{label} digest")
    if _OCI_DIGEST_RE.fullmatch(digest) is None:
        raise ToolkitAuthorityError(f"{label} digest differs")
    source_coordinate_value = value.get("source_coordinate")
    source_coordinate = (
        None
        if source_coordinate_value is None
        else _string(source_coordinate_value, f"{label} source coordinate")
    )
    return RegistryArtifactV1(
        role=_string(value.get("role"), f"{label} role"),
        title=_string(value.get("title"), f"{label} title"),
        media_type=_string(value.get("media_type"), f"{label} media type"),
        digest=digest,
        size_bytes=_integer(value.get("size_bytes"), f"{label} size"),
        source_coordinate=source_coordinate,
    )


def _publication_descriptors(
    receipt: Mapping[str, object],
) -> tuple[RegistryArtifactV1, tuple[RegistryArtifactV1, ...]]:
    publication = _mapping(receipt.get("publication_receipt"), "publication receipt")
    config = _publication_descriptor(
        _mapping(publication.get("config"), "publication config"),
        "publication config",
    )
    layers = tuple(
        _publication_descriptor(_mapping(raw, "publication layer"), "publication layer")
        for raw in _array(publication.get("layers"), "publication layers")
    )
    if not layers:
        raise ToolkitAuthorityError("publication descriptor matrix is empty")
    return config, layers


def _member(receipt: Mapping[str, object], role: str) -> Mapping[str, object]:
    manifest = _mapping(receipt.get("toolkit_manifest"), "toolkit manifest")
    matches = [
        _mapping(raw, "toolkit member")
        for raw in _array(manifest.get("members"), "toolkit members")
        if _mapping(raw, "toolkit member").get("role") == role
    ]
    if len(matches) != 1:
        raise ToolkitAuthorityError(f"toolkit member differs: {role}")
    return matches[0]


def _verify_member_artifact(
    member: Mapping[str, object],
    field: str,
    artifact: RegistryArtifactV1,
) -> None:
    evidence = _mapping(member.get(field), f"toolkit member {field}")
    if (
        evidence.get("sha256") != artifact.sha256
        or evidence.get("size_bytes") != artifact.size_bytes
        or not str(evidence.get("coordinate", "")).endswith(f"@sha256:{artifact.sha256}")
    ):
        raise ToolkitAuthorityError(f"toolkit member {field} differs from publication layer")


def _indexed_digest(index: Mapping[str, object], path: str) -> str:
    matches = [
        _mapping(raw, "contract asset index asset")
        for raw in _array(index.get("assets"), "contract asset index assets")
        if _mapping(raw, "contract asset index asset").get("path") == path
    ]
    if len(matches) != 1:
        raise ToolkitAuthorityError(f"contract asset index entry differs: {path}")
    digest = _string(matches[0].get("sha256"), f"{path} sha256")
    if _SHA256_RE.fullmatch(digest) is None:
        raise ToolkitAuthorityError(f"contract asset index digest differs: {path}")
    return digest


def load_toolkit_authority(
    custos_root: Path,
    *,
    candidate_version: str,
    authority_commit: str,
) -> CustosToolkitAuthorityV1:
    """Verify Custos's receipt for one toolkit version and project its registry descriptors.

    `authority_commit` is the Custos commit that registered the receipt; the caller
    reads it from the checkout's history, since the receipt cannot name the commit
    that adds it.
    """

    root = custos_root.resolve()
    if _COMMIT_RE.fullmatch(authority_commit) is None:
        raise ToolkitAuthorityError("Custos authority commit identity differs")
    authority_receipt = _load_receipt(root, candidate_version)
    receipt = _strict_json(authority_receipt.content, "Custos authority receipt")
    source_commit = _string(receipt.get("source_commit"), "source commit")
    if _COMMIT_RE.fullmatch(source_commit) is None:
        raise ToolkitAuthorityError("Custos authority commit identity differs")
    if not all(
        receipt.get(field) is True
        for field in (
            "authority_registered",
            "handoff_ready",
            "production_signature_verified",
            "ready",
            "remote_publication_verified",
        )
    ):
        raise ToolkitAuthorityError("Custos toolkit authority is not handoff-ready")
    if (
        receipt.get("contract_version") != "alephain.custos.toolkit-rc-authority-receipt.v1"
        or receipt.get("receipt_schema_version") != 1
        or receipt.get("candidate_version") != candidate_version
        or receipt.get("source_commit") != source_commit
    ):
        raise ToolkitAuthorityError("Custos toolkit authority identity differs")

    publication = _mapping(receipt.get("publication_receipt"), "publication receipt")
    manifest_digest = _string(publication.get("manifest_digest"), "manifest digest")
    manifest_size_bytes = _integer(publication.get("manifest_size_bytes"), "manifest size")
    if _OCI_DIGEST_RE.fullmatch(manifest_digest) is None:
        raise ToolkitAuthorityError("Custos toolkit manifest digest differs")
    registry = _string(publication.get("registry"), "registry")
    repository = _string(publication.get("repository"), "repository")
    publication_manifest = _mapping(receipt.get("publication_manifest"), "publication manifest")
    if (
        publication_manifest.get("sha256") != manifest_digest.removeprefix("sha256:")
        or publication_manifest.get("size_bytes") != manifest_size_bytes
        or publication_manifest.get("coordinate") != f"{registry}/{repository}@{manifest_digest}"
    ):
        raise ToolkitAuthorityError("Custos toolkit publication manifest differs")

    contract_asset_index = _publication_artifact(
        receipt,
        role="base_contracts_wheel.contract_asset_index",
        title="strategy-contract-assets-v1.json",
    )
    base_wheel = _publication_artifact(
        receipt,
        role="base_contracts_wheel.wheel",
        title=f"custos_strategy_toolkit-{candidate_version}-py3-none-any.whl",
    )
    nautilus_wheel = _publication_artifact(
        receipt,
        role="nautilus_wheel.wheel",
        title=f"custos_strategy_toolkit_nautilus-{candidate_version}-py3-none-any.whl",
    )
    base_sbom = _publication_artifact(
        receipt,
        role="base_contracts_wheel.sbom",
        title="custos-strategy-toolkit.cdx.json",
    )
    nautilus_sbom = _publication_artifact(
        receipt,
        role="nautilus_wheel.sbom",
        title="custos-strategy-toolkit-nautilus.cdx.json",
    )
    base_member = _member(receipt, "base_contracts_wheel")
    nautilus_member = _member(receipt, "nautilus_wheel")
    publication_config, publication_artifacts = _publication_descriptors(receipt)
    if (
        base_member.get("version") != candidate_version
        or base_member.get("python_requires") != ">=3.11"
        or base_member.get("top_level_modules") != ["custos_toolkit"]
        or nautilus_member.get("version") != candidate_version
        or nautilus_member.get("python_requires") != ">=3.12,<3.13"
        or nautilus_member.get("nautilus_version") != ENGINE_VERSION
        or nautilus_member.get("top_level_modules") != ["custos_toolkit_nautilus"]
    ):
        raise ToolkitAuthorityError("Custos toolkit member compatibility differs")
    for member, field, artifact in (
        (base_member, "contract_asset_index", contract_asset_index),
        (nautilus_member, "contract_asset_index", contract_asset_index),
        (base_member, "wheel", base_wheel),
        (nautilus_member, "wheel", nautilus_wheel),
        (base_member, "sbom", base_sbom),
        (nautilus_member, "sbom", nautilus_sbom),
    ):
        _verify_member_artifact(member, field, artifact)

    return CustosToolkitAuthorityV1(
        owner_repository=OWNER_REPOSITORY,
        authority_repository=AUTHORITY_REPOSITORY,
        authority_commit=authority_commit,
        source_commit=source_commit,
        candidate_version=candidate_version,
        registry=registry,
        repository=repository,
        manifest_digest=manifest_digest,
        manifest_size_bytes=manifest_size_bytes,
        authority_receipt=authority_receipt,
        publication_config=publication_config,
        publication_artifacts=publication_artifacts,
        contract_asset_index=contract_asset_index,
        base_contracts_wheel=base_wheel,
        nautilus_wheel=nautilus_wheel,
        base_contracts_sbom=base_sbom,
        nautilus_sbom=nautilus_sbom,
    )


def bind_contract_authority(
    authority: CustosToolkitAuthorityV1,
    contract_asset_index: bytes,
) -> CustosContractAuthorityV1:
    """Bind registry-fetched asset-index bytes without retaining an owner schema copy."""

    if (
        len(contract_asset_index) != authority.contract_asset_index.size_bytes
        or hashlib.sha256(contract_asset_index).hexdigest() != authority.contract_asset_index.sha256
    ):
        raise ToolkitAuthorityError("registry contract asset index bytes differ")
    index = _strict_json(contract_asset_index, "Custos contract asset index")
    if index.get("asset_index_schema_version") != 1 or index.get("status") != (
        "CANONICAL_V1_CONTRACT_ASSETS_PUBLISHED"
    ):
        raise ToolkitAuthorityError("Custos contract asset index is not canonical V1")
    schema_digest = _indexed_digest(
        index,
        "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json",
    )
    golden_digest = _indexed_digest(
        index,
        "docs/authority/strategy-artifact-ref-v1.golden.json",
    )
    return CustosContractAuthorityV1(
        owner_repository=authority.owner_repository,
        owner_commit=authority.authority_commit,
        contract_receipt_sha256=authority.authority_receipt.sha256,
        asset_index_sha256=authority.contract_asset_index.sha256,
        artifact_ref_schema_sha256=schema_digest,
        artifact_ref_golden_sha256=golden_digest,
        handoff_ready=True,
    )


__all__ = [
    "AUTHORITY_RECEIPT_PATH",
    "CustosToolkitAuthorityV1",
    "ExternalAuthorityReceiptV1",
    "RegistryArtifactV1",
    "ToolkitAuthorityError",
    "bind_contract_authority",
    "load_toolkit_authority",
]

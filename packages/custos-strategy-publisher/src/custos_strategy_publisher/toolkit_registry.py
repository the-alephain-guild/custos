"""Read-only GHCR importer for receipt-pinned Custos toolkit artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

from .ghcr_registry import HttpResponse, HttpTransport, UrllibTransport
from .toolkit_authority import CustosToolkitAuthorityV1, RegistryArtifactV1

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
TOOLKIT_ARTIFACT_TYPE = "application/vnd.alephain.custos.strategy-toolkit.rc.v1"
OCI_TITLE_ANNOTATION = "org.opencontainers.image.title"
OCI_ROLE_ANNOTATION = "io.alephain.custos.toolkit.role"
OCI_SOURCE_COORDINATE_ANNOTATION = "io.alephain.custos.source.coordinate"
_REGISTRY_ORIGIN = "https://ghcr.io"


class ToolkitRegistryError(RuntimeError):
    """The receipt-pinned registry object was absent or byte-drifted."""


@dataclass(frozen=True, slots=True)
class ToolkitRegistrySnapshotV1:
    manifest: bytes
    contract_asset_index: bytes
    base_contracts_wheel: bytes
    nautilus_wheel: bytes
    base_contracts_sbom: bytes
    nautilus_sbom: bytes


class ToolkitRegistryV1:
    """Minimal pull-only client scoped to one immutable Custos authority."""

    def __init__(
        self,
        authority: CustosToolkitAuthorityV1,
        *,
        actor: str,
        token: str,
        transport: HttpTransport | None = None,
    ) -> None:
        if not actor or not token:
            raise ToolkitRegistryError("GHCR actor and token must not be empty")
        if authority.registry != "ghcr.io" or not authority.repository:
            raise ToolkitRegistryError("Custos toolkit registry identity differs")
        self._authority = authority
        self._actor = actor
        self._token = token
        self._transport = transport or UrllibTransport()
        self._bearer_token: str | None = None

    def fetch(self) -> ToolkitRegistrySnapshotV1:
        manifest = self._fetch_manifest()
        return ToolkitRegistrySnapshotV1(
            manifest=manifest,
            contract_asset_index=self._fetch_blob(self._authority.contract_asset_index),
            base_contracts_wheel=self._fetch_blob(self._authority.base_contracts_wheel),
            nautilus_wheel=self._fetch_blob(self._authority.nautilus_wheel),
            base_contracts_sbom=self._fetch_blob(self._authority.base_contracts_sbom),
            nautilus_sbom=self._fetch_blob(self._authority.nautilus_sbom),
        )

    def _fetch_manifest(self) -> bytes:
        authority = self._authority
        response = self._request(
            "GET",
            f"{_REGISTRY_ORIGIN}/v2/{authority.repository}/manifests/{authority.manifest_digest}",
            headers={"Accept": OCI_MANIFEST_MEDIA_TYPE},
        )
        self._require_response(response, 200, "toolkit manifest")
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != OCI_MANIFEST_MEDIA_TYPE:
            raise ToolkitRegistryError("toolkit manifest media type differs")
        self._require_content(
            response,
            authority.manifest_digest,
            authority.manifest_size_bytes,
            "toolkit manifest",
            require_digest_header=True,
        )
        document = _strict_json(response.body, "toolkit manifest")
        _exact_keys(
            document,
            {"annotations", "artifactType", "config", "layers", "mediaType", "schemaVersion"},
            "toolkit manifest",
        )
        if (
            document["schemaVersion"] != 2
            or document["mediaType"] != OCI_MANIFEST_MEDIA_TYPE
            or document["artifactType"] != TOOLKIT_ARTIFACT_TYPE
        ):
            raise ToolkitRegistryError("toolkit manifest identity differs")
        annotations = _mapping(document["annotations"], "toolkit manifest annotations")
        if annotations != {
            "org.opencontainers.image.revision": authority.source_commit,
            "org.opencontainers.image.source": authority.authority_repository,
            "org.opencontainers.image.version": authority.candidate_version,
        }:
            raise ToolkitRegistryError("toolkit manifest provenance differs")
        _require_descriptor(
            _mapping(document["config"], "toolkit manifest config"),
            authority.publication_config,
            "toolkit manifest config",
        )
        raw_layers = _array(document["layers"], "toolkit manifest layers")
        if len(raw_layers) != len(authority.publication_artifacts):
            raise ToolkitRegistryError("toolkit manifest layer count differs")
        for raw, expected in zip(raw_layers, authority.publication_artifacts, strict=True):
            _require_descriptor(
                _mapping(raw, "toolkit manifest layer"),
                expected,
                f"toolkit manifest layer {expected.role}",
            )
        return response.body

    def _fetch_blob(self, artifact: RegistryArtifactV1) -> bytes:
        response = self._request(
            "GET",
            f"{_REGISTRY_ORIGIN}/v2/{self._authority.repository}/blobs/{artifact.digest}",
        )
        self._require_response(response, 200, artifact.role)
        self._require_content(
            response,
            artifact.digest,
            artifact.size_bytes,
            artifact.role,
            require_digest_header=False,
        )
        return response.body

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        request_headers = {"Authorization": f"Bearer {self._bearer()}"}
        if headers:
            request_headers.update(headers)
        return self._transport.request(method, url, request_headers)

    def _bearer(self) -> str:
        if self._bearer_token is not None:
            return self._bearer_token
        query = urlencode(
            {
                "scope": f"repository:{self._authority.repository}:pull",
                "service": "ghcr.io",
            }
        )
        basic = base64.b64encode(f"{self._actor}:{self._token}".encode()).decode("ascii")
        response = self._transport.request(
            "GET",
            f"{_REGISTRY_ORIGIN}/token?{query}",
            {"Authorization": f"Basic {basic}"},
        )
        self._require_response(response, 200, "GHCR bearer token")
        document = _strict_json(response.body, "GHCR bearer token")
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not token:
            raise ToolkitRegistryError("GHCR bearer response omitted token")
        self._bearer_token = token
        return token

    @staticmethod
    def _require_response(response: HttpResponse, expected: int, label: str) -> None:
        if response.status != expected:
            raise ToolkitRegistryError(f"{label} request failed with HTTP {response.status}")

    @staticmethod
    def _require_content(
        response: HttpResponse,
        digest: str,
        size_bytes: int,
        label: str,
        *,
        require_digest_header: bool,
    ) -> None:
        header = response.headers.get("docker-content-digest")
        if require_digest_header and header is None:
            raise ToolkitRegistryError(f"{label} omitted Docker-Content-Digest")
        if header is not None and header != digest:
            raise ToolkitRegistryError(f"{label} digest header differs")
        if len(response.body) != size_bytes:
            raise ToolkitRegistryError(f"{label} size differs")
        if f"sha256:{hashlib.sha256(response.body).hexdigest()}" != digest:
            raise ToolkitRegistryError(f"{label} content digest differs")


def _strict_json(content: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ToolkitRegistryError(f"{label} contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        parsed = json.loads(content, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ToolkitRegistryError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise ToolkitRegistryError(f"{label} must be an object")
    return cast(dict[str, object], parsed)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ToolkitRegistryError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ToolkitRegistryError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ToolkitRegistryError(f"{label} fields differ")


def _require_descriptor(
    value: Mapping[str, object],
    expected: RegistryArtifactV1,
    label: str,
) -> None:
    _exact_keys(value, {"annotations", "digest", "mediaType", "size"}, label)
    expected_annotations = {
        OCI_ROLE_ANNOTATION: expected.role,
        OCI_TITLE_ANNOTATION: expected.title,
    }
    if expected.source_coordinate is not None:
        expected_annotations[OCI_SOURCE_COORDINATE_ANNOTATION] = expected.source_coordinate
    if (
        value["digest"] != expected.digest
        or value["mediaType"] != expected.media_type
        or value["size"] != expected.size_bytes
        or value["annotations"] != expected_annotations
    ):
        raise ToolkitRegistryError(f"{label} descriptor differs")


__all__ = [
    "ToolkitRegistryError",
    "ToolkitRegistrySnapshotV1",
    "ToolkitRegistryV1",
]

"""Small fail-closed GHCR Distribution adapter for strategy artifact publication V1."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .oci_primitives import (
    OCI_MANIFEST_MEDIA_TYPE,
    OciBlobReadbackV1,
    OciDescriptorV1,
    OciManifestReadbackV1,
    OciPublicationError,
    require_ghcr_repository,
)

_REGISTRY_ORIGIN = "https://ghcr.io"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                return HttpResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                body=error.read(),
            )


class GhcrRegistryV1:
    """Authenticated adapter with digest checks on every upload and readback."""

    def __init__(
        self,
        *,
        actor: str,
        token: str,
        repository: str,
        transport: HttpTransport | None = None,
    ) -> None:
        """A client for one GHCR repository, the only one its token is asked for."""

        if not actor or not token:
            raise ValueError("GHCR actor and token must not be empty")
        self._repository = require_ghcr_repository(repository)
        self._actor = actor
        self._token = token
        self._transport = transport or UrllibTransport()
        self._bearer_token: str | None = None
        self._blob_descriptors: dict[str, OciDescriptorV1] = {}

    def put_blob(
        self,
        repository: str,
        descriptor: OciDescriptorV1,
        content: bytes,
    ) -> OciDescriptorV1:
        name = self._repository_name(repository)
        if descriptor.size_bytes != len(content):
            raise OciPublicationError("blob descriptor size differs before upload")
        blob_url = f"{_REGISTRY_ORIGIN}/v2/{name}/blobs/{descriptor.digest}"
        existing = self._registry_request("HEAD", blob_url)
        if existing.status == 200:
            self._require_digest_header(existing, descriptor.digest, "existing blob")
            self._blob_descriptors[descriptor.digest] = descriptor
            return descriptor
        if existing.status != 404:
            self._raise_registry("blob existence probe", existing)

        start = self._registry_request(
            "POST",
            f"{_REGISTRY_ORIGIN}/v2/{name}/blobs/uploads/",
            headers={"Content-Length": "0"},
            body=b"",
        )
        if start.status != 202:
            self._raise_registry("blob upload start", start)
        location = start.headers.get("location")
        if not location:
            raise OciPublicationError("blob upload response omitted Location")
        upload_url = self._with_digest(urljoin(_REGISTRY_ORIGIN, location), descriptor.digest)
        complete = self._registry_request(
            "PUT",
            upload_url,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/octet-stream",
            },
            body=content,
        )
        if complete.status != 201:
            self._raise_registry("blob upload completion", complete)
        self._require_digest_header(complete, descriptor.digest, "uploaded blob")
        self._blob_descriptors[descriptor.digest] = descriptor
        return descriptor

    def get_blob(self, repository: str, digest: str) -> OciBlobReadbackV1:
        name = self._repository_name(repository)
        descriptor = self._blob_descriptors.get(digest)
        if descriptor is None:
            raise OciPublicationError("blob readback lacks the uploaded descriptor")
        response = self._registry_request("GET", f"{_REGISTRY_ORIGIN}/v2/{name}/blobs/{digest}")
        if response.status != 200:
            self._raise_registry("blob readback", response)
        self._require_digest_header(response, digest, "blob readback")
        if len(response.body) != descriptor.size_bytes:
            raise OciPublicationError("blob readback size differs")
        return OciBlobReadbackV1(descriptor=descriptor, content=response.body)

    def resolve_manifest(
        self,
        repository: str,
        reference: str,
    ) -> OciManifestReadbackV1 | None:
        name = self._repository_name(repository)
        response = self._registry_request(
            "GET",
            f"{_REGISTRY_ORIGIN}/v2/{name}/manifests/{reference}",
            headers={"Accept": OCI_MANIFEST_MEDIA_TYPE},
        )
        if response.status == 404:
            return None
        if response.status != 200:
            self._raise_registry("manifest readback", response)
        digest = response.headers.get("docker-content-digest")
        if digest is None:
            raise OciPublicationError("manifest readback omitted Docker-Content-Digest")
        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if media_type != OCI_MANIFEST_MEDIA_TYPE:
            raise OciPublicationError("manifest readback media type differs")
        value = self._json_object(response.body, "manifest readback")
        artifact_type = value.get("artifactType")
        if artifact_type is not None and not isinstance(artifact_type, str):
            raise OciPublicationError("manifest artifact type differs")
        descriptor = OciDescriptorV1(
            media_type=media_type,
            digest=digest,
            size_bytes=len(response.body),
            artifact_type=cast(str | None, artifact_type),
        )
        return OciManifestReadbackV1(descriptor=descriptor, content=response.body)

    def put_manifest(
        self,
        repository: str,
        reference: str,
        media_type: str,
        content: bytes,
    ) -> OciDescriptorV1:
        name = self._repository_name(repository)
        headers = {
            "Content-Length": str(len(content)),
            "Content-Type": media_type,
        }
        if not reference.startswith("sha256:"):
            headers["If-None-Match"] = "*"
        response = self._registry_request(
            "PUT",
            f"{_REGISTRY_ORIGIN}/v2/{name}/manifests/{reference}",
            headers=headers,
            body=content,
        )
        if response.status in {409, 412}:
            raise OciPublicationError("immutable discovery tag already exists")
        if response.status != 201:
            self._raise_registry("manifest upload", response)
        digest = response.headers.get("docker-content-digest")
        if digest is None:
            raise OciPublicationError("manifest upload omitted Docker-Content-Digest")
        value = self._json_object(content, "manifest upload")
        artifact_type = value.get("artifactType")
        if artifact_type is not None and not isinstance(artifact_type, str):
            raise OciPublicationError("manifest artifact type differs")
        return OciDescriptorV1(
            media_type=media_type,
            digest=digest,
            size_bytes=len(content),
            artifact_type=cast(str | None, artifact_type),
        )

    def get_manifest(self, repository: str, digest: str) -> OciManifestReadbackV1:
        result = self.resolve_manifest(repository, digest)
        if result is None:
            raise OciPublicationError("manifest readback returned 404")
        return result

    def _registry_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        bearer = self._get_bearer_token()
        request_headers = {"Authorization": f"Bearer {bearer}"}
        if headers:
            request_headers.update(headers)
        return self._transport.request(method, url, request_headers, body)

    def _get_bearer_token(self) -> str:
        if self._bearer_token is not None:
            return self._bearer_token
        name = self._repository_name(self._repository)
        query = urlencode(
            {
                "scope": f"repository:{name}:pull,push",
                "service": "ghcr.io",
            }
        )
        basic = base64.b64encode(f"{self._actor}:{self._token}".encode()).decode("ascii")
        response = self._transport.request(
            "GET",
            f"{_REGISTRY_ORIGIN}/token?{query}",
            {"Authorization": f"Basic {basic}"},
        )
        if response.status != 200:
            self._raise_registry("GHCR bearer token", response)
        value = self._json_object(response.body, "GHCR bearer token")
        token = value.get("token") or value.get("access_token")
        if not isinstance(token, str) or not token:
            raise OciPublicationError("GHCR bearer response omitted token")
        self._bearer_token = token
        return token

    def _repository_name(self, repository: str) -> str:
        if repository != self._repository:
            raise OciPublicationError("GHCR repository differs from the one this client is for")
        return repository.removeprefix("ghcr.io/")

    @staticmethod
    def _with_digest(url: str, digest: str) -> str:
        parsed = urlparse(url)
        query = parsed.query
        digest_query = urlencode({"digest": digest})
        combined = f"{query}&{digest_query}" if query else digest_query
        return urlunparse(parsed._replace(query=combined))

    @staticmethod
    def _require_digest_header(
        response: HttpResponse,
        expected: str,
        label: str,
    ) -> None:
        actual = response.headers.get("docker-content-digest")
        if actual is not None and actual != expected:
            raise OciPublicationError(f"{label} digest header differs")

    @staticmethod
    def _json_object(content: bytes, label: str) -> dict[str, object]:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OciPublicationError(f"{label} is not JSON") from error
        if not isinstance(value, dict):
            raise OciPublicationError(f"{label} must be an object")
        return cast(dict[str, object], value)

    @staticmethod
    def _required_string(value: Mapping[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise OciPublicationError(f"referrer {key} differs")
        return item

    @staticmethod
    def _optional_string(value: Mapping[str, object], key: str) -> str | None:
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise OciPublicationError(f"referrer {key} differs")
        return cast(str | None, item)

    @staticmethod
    def _required_int(value: Mapping[str, object], key: str) -> int:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise OciPublicationError(f"referrer {key} differs")
        return item

    @staticmethod
    def _raise_registry(label: str, response: HttpResponse) -> None:
        raise OciPublicationError(
            f"{label} failed with HTTP {response.status}: "
            f"{response.body[:256].decode('utf-8', errors='replace')}"
        )


__all__ = [
    "GhcrRegistryV1",
    "HttpResponse",
    "HttpTransport",
    "UrllibTransport",
]

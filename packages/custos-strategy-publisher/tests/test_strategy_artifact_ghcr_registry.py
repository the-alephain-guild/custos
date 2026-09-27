from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from example_producer import GHCR_REPOSITORY

ROOT = Path(__file__).resolve().parents[1]

from custos_strategy_publisher.ghcr_registry import (  # noqa: E402
    GhcrRegistryV1,
    HttpResponse,
)
from custos_strategy_publisher.oci_publication import (  # noqa: E402
    OCI_MANIFEST_MEDIA_TYPE,
    OciDescriptorV1,
    OciPublicationError,
)


class QueueTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)


def _token_response() -> HttpResponse:
    return HttpResponse(status=200, headers={}, body=b'{"token":"bearer"}')


def test_manifest_tag_put_uses_conditional_create_and_rejects_conflict() -> None:
    transport = QueueTransport(
        _token_response(),
        HttpResponse(status=412, headers={}, body=b"tag exists"),
    )
    registry = GhcrRegistryV1(
        actor="actor", token="token", repository=GHCR_REPOSITORY, transport=transport
    )
    manifest = json.dumps(
        {
            "artifactType": "application/vnd.alephain.strategy-release.v1",
            "config": {},
            "layers": [],
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with pytest.raises(OciPublicationError, match="already exists"):
        registry.put_manifest(
            GHCR_REPOSITORY,
            "trend-supertrend-0.1.0rc1",
            OCI_MANIFEST_MEDIA_TYPE,
            manifest,
        )

    _, _, headers, _ = transport.calls[-1]
    assert headers["Authorization"] == "Bearer bearer"
    assert headers["If-None-Match"] == "*"


def test_blob_upload_rejects_registry_digest_header_drift() -> None:
    content = b"exact blob"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    descriptor = OciDescriptorV1(
        media_type="application/octet-stream",
        digest=digest,
        size_bytes=len(content),
    )
    transport = QueueTransport(
        _token_response(),
        HttpResponse(status=404, headers={}, body=b""),
        HttpResponse(
            status=202,
            headers={"location": "/v2/upload/session"},
            body=b"",
        ),
        HttpResponse(
            status=201,
            headers={"docker-content-digest": "sha256:" + ("0" * 64)},
            body=b"",
        ),
    )
    registry = GhcrRegistryV1(
        actor="actor", token="token", repository=GHCR_REPOSITORY, transport=transport
    )

    with pytest.raises(OciPublicationError, match="digest header differs"):
        registry.put_blob(GHCR_REPOSITORY, descriptor, content)


def test_manifest_readback_requires_digest_and_exact_media_type() -> None:
    content = b'{"artifactType":"application/vnd.alephain.strategy-release.v1"}'
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    transport = QueueTransport(
        _token_response(),
        HttpResponse(
            status=200,
            headers={
                "content-type": OCI_MANIFEST_MEDIA_TYPE,
                "docker-content-digest": digest,
            },
            body=content,
        ),
    )
    registry = GhcrRegistryV1(
        actor="actor", token="token", repository=GHCR_REPOSITORY, transport=transport
    )

    result = registry.resolve_manifest(
        GHCR_REPOSITORY,
        digest,
    )

    assert result is not None
    assert result.descriptor.digest == digest
    assert result.content == content

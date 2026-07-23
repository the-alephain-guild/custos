"""Digest-pinned OCI materialization for a Crucible-owned StrategyRelease."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from custos.artifacts.release_resolver import (
    MaterializedStrategyReleaseArtifactV1,
    StrategyReleaseResolutionRejected,
    StrategyReleaseResolutionUnavailable,
)
from custos.artifacts.runtime import StrategyReleaseArtifactAuthorityV1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126})$")
_BEARER_PARAMETER = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)="([^"]*)"')


@dataclass(frozen=True, slots=True)
class RegistryPullCredentialV1:
    username: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.username or self.username != self.username.strip():
            raise ValueError("registry pull username is invalid")
        if not self.token or self.token != self.token.strip():
            raise ValueError("registry pull token is invalid")


class OciBlobTransportV1(Protocol):
    def fetch_blob(
        self,
        *,
        registry: str,
        repository: str,
        digest: str,
        max_bytes: int,
    ) -> bytes: ...


class HttpOciBlobTransportV1:
    """Minimal pull-only OCI Distribution client with scoped Bearer auth."""

    def __init__(
        self,
        *,
        allowed_registries: tuple[str, ...],
        credentials: Mapping[str, RegistryPullCredentialV1] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        normalized = tuple(registry.lower() for registry in allowed_registries)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("allowed OCI registries must be non-empty and unique")
        if any(_REGISTRY.fullmatch(registry) is None for registry in normalized):
            raise ValueError("allowed OCI registry hostname is invalid")
        if timeout_seconds <= 0:
            raise ValueError("OCI registry timeout must be positive")
        self._allowed_registries = frozenset(normalized)
        self._credentials = {
            registry.lower(): credential
            for registry, credential in (credentials or {}).items()
        }
        if not set(self._credentials).issubset(self._allowed_registries):
            raise ValueError("OCI credential registry is not allowlisted")
        self._timeout_seconds = timeout_seconds

    def fetch_blob(
        self,
        *,
        registry: str,
        repository: str,
        digest: str,
        max_bytes: int,
    ) -> bytes:
        registry = registry.lower()
        if registry not in self._allowed_registries:
            raise StrategyReleaseResolutionRejected("OCI registry is not allowlisted")
        if _SHA256.fullmatch(digest) is None or max_bytes <= 0:
            raise StrategyReleaseResolutionRejected("OCI blob request is invalid")
        encoded_repository = quote(repository, safe="/")
        url = f"https://{registry}/v2/{encoded_repository}/blobs/sha256:{digest}"
        try:
            return self._request_bytes(url, {}, max_bytes)
        except HTTPError as error:
            if error.code != 401:
                raise StrategyReleaseResolutionUnavailable(
                    f"OCI registry blob request failed with HTTP {error.code}"
                ) from error
            challenge = error.headers.get("WWW-Authenticate", "")
        token = self._bearer_token(
            registry=registry,
            repository=repository,
            challenge=challenge,
        )
        try:
            return self._request_bytes(
                url,
                {"Authorization": f"Bearer {token}"},
                max_bytes,
            )
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise StrategyReleaseResolutionUnavailable(
                "authenticated OCI registry blob request failed"
            ) from error

    def _bearer_token(self, *, registry: str, repository: str, challenge: str) -> str:
        if not challenge.startswith("Bearer "):
            raise StrategyReleaseResolutionRejected(
                "OCI registry did not return a Bearer challenge"
            )
        parameters = dict(_BEARER_PARAMETER.findall(challenge[7:]))
        expected_scope = f"repository:{repository}:pull"
        realm = parameters.get("realm", "")
        service = parameters.get("service", "")
        scope = parameters.get("scope", "")
        parsed = urlsplit(realm)
        if (
            parsed.scheme != "https"
            or parsed.hostname != registry
            or parsed.username is not None
            or parsed.password is not None
            or service != registry
            or scope != expected_scope
        ):
            raise StrategyReleaseResolutionRejected(
                "OCI registry Bearer challenge authority differs"
            )
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((("service", service), ("scope", scope)))
        token_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
        )
        headers: dict[str, str] = {}
        credential = self._credentials.get(registry)
        if credential is not None:
            encoded = base64.b64encode(
                f"{credential.username}:{credential.token}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        try:
            payload = self._request_bytes(token_url, headers, 64 * 1024)
            document = json.loads(payload)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise StrategyReleaseResolutionUnavailable(
                "OCI registry token exchange failed"
            ) from error
        if not isinstance(document, dict):
            raise StrategyReleaseResolutionRejected("OCI registry token response is invalid")
        token = document.get("token", document.get("access_token"))
        if not isinstance(token, str) or not token:
            raise StrategyReleaseResolutionRejected("OCI registry token response has no token")
        return token

    def _request_bytes(
        self,
        url: str,
        headers: Mapping[str, str],
        max_bytes: int,
    ) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/octet-stream, application/json",
                "User-Agent": "custos-runner/strategy-release-v1",
                **headers,
            },
            method="GET",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as error:
                    raise StrategyReleaseResolutionRejected(
                        "OCI response Content-Length is invalid"
                    ) from error
                if declared < 0 or declared > max_bytes:
                    raise StrategyReleaseResolutionRejected(
                        "OCI response exceeds the materialization limit"
                    )
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise StrategyReleaseResolutionRejected(
                "OCI response exceeds the materialization limit"
            )
        return payload


@dataclass(frozen=True, slots=True)
class _OciBlobCoordinate:
    registry: str
    repository: str
    digest: str


class RegistryStrategyReleaseMaterializerV1:
    """Materialize only the immutable bytes imported by the strategy runtime."""

    def __init__(
        self,
        *,
        cache_root: Path,
        transport: OciBlobTransportV1,
        max_blob_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if not cache_root.is_absolute():
            raise ValueError("artifact material cache root must be absolute")
        if max_blob_bytes <= 0:
            raise ValueError("artifact material byte limit must be positive")
        self._cache_root = cache_root
        self._transport = transport
        self._max_blob_bytes = max_blob_bytes

    async def materialize(
        self,
        *,
        release_authority: StrategyReleaseArtifactAuthorityV1,
        authority_statement_bytes: bytes,
    ) -> MaterializedStrategyReleaseArtifactV1:
        detached = release_authority.detached_attestation_ref
        statement_coordinate = self._coordinate(
            detached.get("statement_coordinate"),
            detached.get("statement_sha256"),
            "release statement",
        )
        bundle_coordinate = self._coordinate(
            detached.get("bundle_coordinate"),
            detached.get("bundle_sha256"),
            "detached bundle",
        )
        if (
            statement_coordinate.registry,
            statement_coordinate.repository,
        ) != (bundle_coordinate.registry, bundle_coordinate.repository):
            raise StrategyReleaseResolutionRejected(
                "statement and bundle OCI repositories differ"
            )
        strategy_member = self._strategy_member(release_authority.release_bom)
        artifact_ref = release_authority.artifact_ref
        statement_path, bundle_path, wheel_path = await asyncio.gather(
            asyncio.to_thread(
                self._materialize_blob,
                statement_coordinate,
                None,
            ),
            asyncio.to_thread(
                self._materialize_blob,
                bundle_coordinate,
                None,
            ),
            asyncio.to_thread(
                self._materialize_blob,
                _OciBlobCoordinate(
                    registry=statement_coordinate.registry,
                    repository=statement_coordinate.repository,
                    digest=artifact_ref.artifact_sha256,
                ),
                artifact_ref.artifact_size_bytes,
            ),
        )
        if self._read_cached(statement_path, None) != authority_statement_bytes:
            raise StrategyReleaseResolutionRejected(
                "materialized statement differs from Crucible authority"
            )
        return MaterializedStrategyReleaseArtifactV1(
            release_statement_bytes=authority_statement_bytes,
            detached_bundle_path=bundle_path,
            member_paths={str(strategy_member["name"]): wheel_path},
            verified_at=datetime.now(UTC),
        )

    def _coordinate(
        self,
        coordinate: object,
        expected_digest: object,
        label: str,
    ) -> _OciBlobCoordinate:
        if (
            not isinstance(coordinate, str)
            or not isinstance(expected_digest, str)
            or _SHA256.fullmatch(expected_digest) is None
        ):
            raise StrategyReleaseResolutionRejected(f"{label} coordinate is invalid")
        parsed = urlsplit(coordinate)
        try:
            port = parsed.port
        except ValueError as error:
            raise StrategyReleaseResolutionRejected(
                f"{label} coordinate port is invalid"
            ) from error
        if (
            parsed.scheme != "oci"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            raise StrategyReleaseResolutionRejected(f"{label} coordinate is not immutable OCI")
        registry = parsed.hostname.lower()
        if _REGISTRY.fullmatch(registry) is None:
            raise StrategyReleaseResolutionRejected(f"{label} registry is invalid")
        raw_path = parsed.path.removeprefix("/")
        object_path, separator, digest = raw_path.rpartition("@sha256:")
        segments = object_path.split("/")
        if (
            separator == ""
            or digest != expected_digest
            or len(segments) < 3
            or any(_REPOSITORY_SEGMENT.fullmatch(segment) is None for segment in segments)
        ):
            raise StrategyReleaseResolutionRejected(f"{label} coordinate binding differs")
        return _OciBlobCoordinate(
            registry=registry,
            repository="/".join(segments[:-1]),
            digest=digest,
        )

    @staticmethod
    def _strategy_member(release_bom: Mapping[str, object]) -> Mapping[str, object]:
        members = release_bom.get("members")
        if not isinstance(members, list):
            raise StrategyReleaseResolutionRejected("release BOM members are invalid")
        strategy_members = [
            member
            for member in members
            if isinstance(member, Mapping) and member.get("role") == "strategy_wheel"
        ]
        if len(strategy_members) != 1 or not isinstance(
            strategy_members[0].get("name"), str
        ):
            raise StrategyReleaseResolutionRejected(
                "release BOM has no unique strategy wheel"
            )
        return strategy_members[0]

    def _materialize_blob(
        self,
        coordinate: _OciBlobCoordinate,
        expected_size: int | None,
    ) -> Path:
        path = self._cache_root / "sha256" / coordinate.digest
        if path.exists() or path.is_symlink():
            self._read_cached(path, expected_size)
            return path
        try:
            payload = self._transport.fetch_blob(
                registry=coordinate.registry,
                repository=coordinate.repository,
                digest=coordinate.digest,
                max_bytes=self._max_blob_bytes,
            )
        except (StrategyReleaseResolutionRejected, StrategyReleaseResolutionUnavailable):
            raise
        except Exception as error:
            raise StrategyReleaseResolutionUnavailable(
                "OCI blob transport failed"
            ) from error
        self._verify_payload(payload, coordinate.digest, expected_size)
        digest_root = path.parent
        if self._cache_root.is_symlink() or digest_root.is_symlink():
            raise StrategyReleaseResolutionRejected("artifact cache root is a symlink")
        try:
            digest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".material-",
                dir=digest_root,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o400)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as error:
            raise StrategyReleaseResolutionUnavailable(
                "artifact cache materialization failed"
            ) from error
        self._read_cached(path, expected_size)
        return path

    def _read_cached(self, path: Path, expected_size: int | None) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise StrategyReleaseResolutionRejected(
                "artifact cache entry is not a regular file"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                payload = handle.read(self._max_blob_bytes + 1)
                after = os.fstat(handle.fileno())
        except OSError as error:
            raise StrategyReleaseResolutionUnavailable(
                "artifact cache entry cannot be read"
            ) from error
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
            raise StrategyReleaseResolutionRejected(
                "artifact cache entry changed while being read"
            )
        digest = path.name
        self._verify_payload(payload, digest, expected_size)
        return payload

    def _verify_payload(
        self,
        payload: bytes,
        digest: str,
        expected_size: int | None,
    ) -> None:
        if len(payload) > self._max_blob_bytes:
            raise StrategyReleaseResolutionRejected(
                "artifact material exceeds the byte limit"
            )
        if expected_size is not None and len(payload) != expected_size:
            raise StrategyReleaseResolutionRejected("artifact material size differs")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StrategyReleaseResolutionRejected("artifact material digest differs")


__all__ = [
    "HttpOciBlobTransportV1",
    "OciBlobTransportV1",
    "RegistryPullCredentialV1",
    "RegistryStrategyReleaseMaterializerV1",
]

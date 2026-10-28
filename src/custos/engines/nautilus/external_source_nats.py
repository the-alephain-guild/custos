"""Separate TLS/JWT broker connection for one authorized source subscriber."""

from __future__ import annotations

import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import nats
from nats.aio.client import RawCredentials

from custos.engines.nautilus.external_source_authorization import (
    AuthorizedSourceSubscription,
    ExternalSourceAuthorizationError,
    read_bound_nats_credentials,
)


@dataclass(frozen=True, slots=True)
class ExternalSourceNatsProfile:
    grant: AuthorizedSourceSubscription
    url: str
    ca_path: Path
    server_name: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if (
            parsed.scheme not in {"nats", "tls"}
            or not parsed.hostname
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("source subscriber NATS URL is invalid")
        if not self.server_name or any(c.isspace() or c == "/" for c in self.server_name):
            raise ValueError("source subscriber TLS server name is invalid")

    async def connect(
        self,
        *,
        disconnected_cb: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Never reuse runner-control transport or connect with changed credentials."""
        credentials = read_bound_nats_credentials(self.grant)
        try:
            raw = RawCredentials(credentials.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ExternalSourceAuthorizationError("source credential is not UTF-8") from error
        context = ssl.create_default_context(cafile=str(self.ca_path))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return await nats.connect(
            servers=[self.url],
            name=f"custos-source-subscription-{self.grant.spec.durable_name}",
            user_credentials=raw,
            tls=context,
            tls_hostname=self.server_name,
            connect_timeout=2,
            allow_reconnect=False,
            disconnected_cb=disconnected_cb,
            closed_cb=disconnected_cb,
        )

"""The forward proxy an operator routes venue traffic through.

The address may carry credentials. It is read from the environment rather than
the command line, which other users of the machine can list, and only its
scheme, host and port ever reach a log line or an error message.

Only ``http`` and ``https`` proxies are accepted: the engine's WebSocket
clients tunnel through ``CONNECT`` and do not implement SOCKS.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

VENUE_PROXY_ENV = "CUSTOS_VENUE_PROXY_URL"

_SCHEMES = frozenset({"http", "https"})


class VenueProxyError(ValueError):
    """The configured venue proxy cannot be used; the message never repeats it."""


@dataclass(frozen=True, slots=True)
class VenueProxy:
    url: str = field(repr=False)
    redacted: str

    @classmethod
    def parse(cls, value: str) -> VenueProxy:
        try:
            parts = urlsplit(value.strip())
            port = parts.port
        except ValueError as error:
            raise VenueProxyError(f"{VENUE_PROXY_ENV} is not a valid URL") from error
        if parts.scheme not in _SCHEMES:
            raise VenueProxyError(f"{VENUE_PROXY_ENV} must use http:// or https://")
        if not parts.hostname:
            raise VenueProxyError(f"{VENUE_PROXY_ENV} must name a proxy host")
        redacted = f"{parts.scheme}://{parts.hostname}"
        if port is not None:
            redacted = f"{redacted}:{port}"
        return cls(url=value.strip(), redacted=redacted)

    def __str__(self) -> str:
        return self.redacted


def venue_proxy_from_environment(environ: Mapping[str, str]) -> VenueProxy | None:
    """The proxy named by ``CUSTOS_VENUE_PROXY_URL``, or ``None`` when it is unset.

    General variables such as ``HTTPS_PROXY`` are deliberately ignored: a proxy set
    for the whole machine is not necessarily one the operator wants exchange
    credentials to cross.
    """
    value = environ.get(VENUE_PROXY_ENV, "").strip()
    return VenueProxy.parse(value) if value else None

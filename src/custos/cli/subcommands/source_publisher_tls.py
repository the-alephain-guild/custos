"""Private TLS/JWT source publisher; its HTTP listener remains on IPv4 loopback."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from custos.cli.subcommands.source_webhook import (
    SourcePublisherService,
    SourceWebhookConfig,
    SourceWebhookIngress,
    create_source_webhook_server,
)
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_transport import (
    ExternalSourceTransportError,
    SourcePublicationWorker,
    signal_subject,
)
from custos.engines.nautilus.external_source_authorization import (
    read_private_source_file,
    read_source_nats_credentials,
)
from custos.engines.nautilus.external_source_publisher_nats import SourcePublisherNatsProfile

_FIELDS = frozenset(
    {
        "schemaVersion",
        "tenantId",
        "sourceId",
        "allowedSymbols",
        "signalTokenSha256",
        "natsCredentialsSha256",
    }
)
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourcePublisherTLSSettings:
    authorization_path: Path
    authorization_sha256: str
    ingress: SourceWebhookConfig
    profile: SourcePublisherNatsProfile
    outbox: Path
    listen_port: int

    def revalidate(self) -> None:
        current = read_private_source_file(self.authorization_path, max_bytes=4096)
        if hashlib.sha256(current).hexdigest() != self.authorization_sha256:
            raise ValueError("source publisher authorization changed after startup")
        read_source_nats_credentials(self.profile.credentials_path, self.profile.credentials_sha256)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a target-neutral source publisher with a separate TLS/JWT broker identity.",
    )
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--nats-credentials", type=Path, required=True)
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--nats-ca", type=Path, required=True)
    parser.add_argument("--nats-server-name", required=True)
    parser.add_argument("--listen-port", type=int, default=8765)
    return parser


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("source publisher authorization fields are duplicated")
        value[key] = item
    return value


def build_settings(args: argparse.Namespace) -> SourcePublisherTLSSettings:
    raw = read_private_source_file(args.authorization, max_bytes=4096)
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("source publisher authorization is invalid JSON") from error
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise ValueError("source publisher authorization has an invalid shape")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise ValueError("source publisher authorization version is unsupported")
    tenant_id, source_id = document["tenantId"], document["sourceId"]
    try:
        signal_subject(tenant_id, source_id)
    except ExternalSourceTransportError as error:
        raise ValueError("source publisher identity is invalid") from error
    symbols = document["allowedSymbols"]
    if (
        not isinstance(symbols, list)
        or not 1 <= len(symbols) <= 32
        or any(not isinstance(item, str) or not _SYMBOL.fullmatch(item) for item in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise ValueError("source publisher symbol scope is invalid")
    token_digest = document["signalTokenSha256"]
    credential_digest = document["natsCredentialsSha256"]
    if not isinstance(token_digest, str) or not _DIGEST.fullmatch(token_digest):
        raise ValueError("source publisher signal token digest is invalid")
    if not isinstance(credential_digest, str) or not _DIGEST.fullmatch(credential_digest):
        raise ValueError("source publisher credential digest is invalid")
    read_source_nats_credentials(args.nats_credentials, credential_digest)
    if type(args.listen_port) is not int or not 0 <= args.listen_port <= 65535:
        raise ValueError("source publisher listen port is invalid")
    profile = SourcePublisherNatsProfile(
        tenant_id=tenant_id,
        source_id=source_id,
        credentials_path=args.nats_credentials,
        credentials_sha256=credential_digest,
        url=args.nats_url,
        ca_path=args.nats_ca,
        server_name=args.nats_server_name,
    )
    return SourcePublisherTLSSettings(
        authorization_path=args.authorization,
        authorization_sha256=hashlib.sha256(raw).hexdigest(),
        ingress=SourceWebhookConfig(
            tenant_id=tenant_id,
            source_id=source_id,
            allowed_symbols=frozenset(symbols),
            signal_token_sha256=bytes.fromhex(token_digest),
        ),
        profile=profile,
        outbox=args.outbox,
        listen_port=args.listen_port,
    )


class _AuthorizedPublicationWorker:
    def __init__(self, settings: SourcePublisherTLSSettings, worker: SourcePublicationWorker):
        self.settings = settings
        self.worker = worker

    async def publish_once(self, *, at: datetime) -> bool:
        self.settings.revalidate()
        return await self.worker.publish_once(at=at)


async def run(args: argparse.Namespace) -> int:
    """Connect to the dedicated broker before accepting any local HTTP request."""
    settings = build_settings(args)
    lost = asyncio.Event()

    async def disconnected() -> None:
        lost.set()

    connection = await settings.profile.connect(disconnected_cb=disconnected)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    service_task: asyncio.Task | None = None
    lost_task: asyncio.Task | None = None
    try:
        outbox = ExternalSourceOutbox(settings.outbox)
        ingress = SourceWebhookIngress(settings.ingress, outbox)
        server = create_source_webhook_server(("127.0.0.1", settings.listen_port), ingress)
        worker = _AuthorizedPublicationWorker(
            settings,
            SourcePublicationWorker(
                outbox, connection.jetstream(), tenant_id=settings.ingress.tenant_id
            ),
        )
        service = SourcePublisherService(server, worker)
        service_task = asyncio.create_task(service.run(stop))
        lost_task = asyncio.create_task(lost.wait())
        await service.started.wait()
        print(f"source publisher tls listening on 127.0.0.1:{server.server_port}")
        done, _ = await asyncio.wait({service_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)
        if lost_task in done and not stop.is_set():
            raise RuntimeError("source publisher broker connection was lost")
        await service_task
    finally:
        stop.set()
        if service_task is not None:
            await service_task
        if lost_task is not None:
            lost_task.cancel()
            await asyncio.gather(lost_task, return_exceptions=True)
        if not connection.is_closed:
            await connection.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(_build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

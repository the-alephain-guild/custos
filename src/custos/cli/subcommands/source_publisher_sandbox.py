"""Explicit non-promotable source publisher on an isolated loopback NATS server."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import nats

from custos.cli.subcommands.source_webhook import (
    SourcePublisherService,
    SourceWebhookConfig,
    SourceWebhookIngress,
    create_source_webhook_server,
)
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    ExternalSourceTransportError,
    SourcePublicationWorker,
    signal_subject,
)

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}\Z")
_DIGEST = re.compile(rb"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourcePublisherSandboxSettings:
    ingress: SourceWebhookConfig
    outbox: Path
    nats_url: str
    listen_port: int


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a target-neutral source receiver on isolated loopback NATS only.",
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--allow-symbol", action="append", required=True)
    parser.add_argument("--token-digest-file", type=Path, required=True)
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--listen-port", type=int, default=8765)
    return parser


def _read_private_digest(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("source digest file must be private") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("source digest file must be private")
        value = os.read(descriptor, 128).rstrip(b"\n")
        if not _DIGEST.fullmatch(value):
            raise ValueError("source token digest is invalid")
        return bytes.fromhex(value.decode("ascii"))
    finally:
        os.close(descriptor)


def build_settings(args: argparse.Namespace) -> SourcePublisherSandboxSettings:
    parsed = urlparse(args.nats_url)
    if (
        parsed.scheme != "nats"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("sandbox NATS must be an unauthenticated IPv4 loopback URL")
    if not 0 <= args.listen_port <= 65535:
        raise ValueError("source publisher listen port is invalid")
    try:
        signal_subject(args.tenant_id, args.source_id)
    except ExternalSourceTransportError as error:
        raise ValueError("source publisher identity is invalid") from error
    symbols = frozenset(args.allow_symbol)
    if not symbols or any(
        not isinstance(item, str) or not _SYMBOL.fullmatch(item) for item in symbols
    ):
        raise ValueError("source publisher symbol scope is invalid")
    return SourcePublisherSandboxSettings(
        ingress=SourceWebhookConfig(
            tenant_id=args.tenant_id,
            source_id=args.source_id,
            allowed_symbols=symbols,
            signal_token_sha256=_read_private_digest(args.token_digest_file),
        ),
        outbox=args.outbox,
        nats_url=args.nats_url,
        listen_port=args.listen_port,
    )


async def run(args: argparse.Namespace) -> int:
    """Keep HTTP intake open only while the isolated broker connection is healthy."""
    settings = build_settings(args)
    lost = asyncio.Event()

    async def disconnected() -> None:
        lost.set()

    connection = await nats.connect(
        settings.nats_url,
        name="custos-source-publisher-sandbox",
        connect_timeout=2,
        allow_reconnect=False,
        disconnected_cb=disconnected,
        closed_cb=disconnected,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    service_task: asyncio.Task | None = None
    lost_task: asyncio.Task | None = None
    try:
        jetstream = connection.jetstream()
        await jetstream.stream_info(SIGNAL_STREAM_NAME)
        outbox = ExternalSourceOutbox(settings.outbox)
        ingress = SourceWebhookIngress(settings.ingress, outbox)
        server = create_source_webhook_server(("127.0.0.1", settings.listen_port), ingress)
        service = SourcePublisherService(
            server, SourcePublicationWorker(outbox, jetstream, tenant_id=settings.ingress.tenant_id)
        )
        service_task = asyncio.create_task(service.run(stop))
        lost_task = asyncio.create_task(lost.wait())
        await service.started.wait()
        print(f"source publisher sandbox listening on 127.0.0.1:{server.server_port}")
        done, _ = await asyncio.wait({service_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)
        if lost_task in done and not stop.is_set():
            raise RuntimeError("source publisher NATS connection was lost")
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

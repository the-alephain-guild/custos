"""Prepare a local-only TLS/JWT source broker for two exact testnet scopes.

This is an integration harness, not a production credential issuer. Its signing
helpers are shared with the real-NATS tests and its output belongs in ignored
``dist/``. Never use synthetic identities as signed deployment authority.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import UUID, uuid4

import nats
import nkeys  # type: ignore[import-untyped]
from nats.aio.client import RawCredentials
from nats.js.errors import NotFoundError
from test_external_source_jwt import _credentials
from test_nats_revocation import (
    _account_jwt,
    _encode_nats_jwt,
    _keypair,
    _server_config,
    _unrestricted_user_jwt,
)

from custos.cli.subcommands.source_publisher_tls import build_settings
from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    bind_signal_consumer,
    publisher_permissions,
    signal_subject,
    subscriber_permissions,
    subscription_spec,
)
from custos.engines.nautilus.external_source_authorization import (
    load_source_subscription,
    read_private_source_file,
)
from custos.engines.nautilus.external_source_nats import ExternalSourceNatsProfile
from custos.engines.nautilus.external_source_publisher_nats import SourcePublisherNatsProfile
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}\Z")
_CONNECTORS = ("okx_perpetual", "binance_perpetual")
_IMAGE = "nats:2.10-alpine"


def _private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _scoped_user_jwt(
    *,
    account_key: object,
    public_key: str,
    permissions: dict,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return _encode_nats_jwt(
        signer=account_key,
        subject=public_key,
        issued_at=issued_at,
        expires_at=expires_at,
        nats_claims={
            "pub": {
                "allow": permissions["publish_allow"],
                "deny": permissions["publish_deny"],
            },
            "sub": {
                "allow": permissions["subscribe_allow"],
                "deny": permissions["subscribe_deny"],
            },
            "subs": -1,
            "data": -1,
            "payload": -1,
            "type": "user",
            "version": 2,
        },
    )


def _synthetic_manifest() -> dict[str, object]:
    return {
        "tenantId": "synthetic-source-testnet",
        "sourceId": "synthetic-supertrend",
        "sourceSymbol": "ETH-USDT-SWAP",
        "targetPair": "ETH-USDT",
        "subscribers": [
            {
                "accountAlias": f"synthetic-{connector}",
                "connector": connector,
                "runnerId": str(uuid4()),
                "deploymentInstanceId": str(uuid4()),
                "deploymentSpecId": str(uuid4()),
                "deploymentSpecDigest": secrets.token_hex(32),
                "generation": 1,
                "tradingMode": "testnet",
            }
            for connector in _CONNECTORS
        ],
    }


def _validate_manifest(manifest: dict[str, object]) -> tuple[str, str, str, str, list[dict]]:
    if set(manifest) != {"tenantId", "sourceId", "sourceSymbol", "targetPair", "subscribers"}:
        raise ValueError("source broker manifest shape is invalid")
    tenant, source = manifest["tenantId"], manifest["sourceId"]
    if not isinstance(tenant, str) or not isinstance(source, str):
        raise ValueError("source broker identity is invalid")
    signal_subject(tenant, source)
    symbol, pair = manifest["sourceSymbol"], manifest["targetPair"]
    if (
        not isinstance(symbol, str)
        or not isinstance(pair, str)
        or not _SYMBOL.fullmatch(symbol)
        or not _SYMBOL.fullmatch(pair)
        or symbol != f"{pair}-SWAP"
    ):
        raise ValueError("source broker instrument mapping is invalid")
    entries = manifest["subscribers"]
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("source broker requires exactly two subscribers")
    if {entry.get("connector") for entry in entries if isinstance(entry, dict)} != set(_CONNECTORS):
        raise ValueError("source broker requires independent OKX and Binance scopes")
    seen_runners: set[UUID] = set()
    seen_instances: set[UUID] = set()
    seen_accounts: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "accountAlias",
            "connector",
            "runnerId",
            "deploymentInstanceId",
            "deploymentSpecId",
            "deploymentSpecDigest",
            "generation",
            "tradingMode",
        }:
            raise ValueError("source subscriber manifest shape is invalid")
        alias = entry["accountAlias"]
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias):
            raise ValueError("source subscriber account alias is invalid")
        try:
            runner = UUID(entry["runnerId"])
            instance = UUID(entry["deploymentInstanceId"])
            spec_id = UUID(entry["deploymentSpecId"])
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("source subscriber identity is invalid") from error
        if any(
            str(value) != entry[field]
            for value, field in (
                (runner, "runnerId"),
                (instance, "deploymentInstanceId"),
                (spec_id, "deploymentSpecId"),
            )
        ):
            raise ValueError("source subscriber identity is not canonical")
        if (
            type(entry["generation"]) is not int
            or entry["generation"] < 1
            or entry["tradingMode"] != "testnet"
            or not isinstance(entry["deploymentSpecDigest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["deploymentSpecDigest"])
        ):
            raise ValueError("source subscriber authority is invalid")
        if runner in seen_runners or instance in seen_instances or alias in seen_accounts:
            raise ValueError("source subscriber identities must be distinct")
        seen_runners.add(runner)
        seen_instances.add(instance)
        seen_accounts.add(alias)
    return tenant, source, symbol, pair, entries


def _certificate(output: Path) -> None:
    _private_write(
        output / "openssl.cnf",
        b"[req]\ndistinguished_name=dn\nx509_extensions=v3_req\nprompt=no\n"
        b"[dn]\nCN=localhost\n[v3_req]\nsubjectAltName=@alt_names\n"
        b"[alt_names]\nDNS.1=localhost\nIP.1=127.0.0.1\n",
    )
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "7",
            "-keyout",
            str(output / "server.key"),
            "-out",
            str(output / "server.crt"),
            "-config",
            str(output / "openssl.cnf"),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("local broker certificate generation failed")
    (output / "server.key").chmod(0o600)
    (output / "server.crt").chmod(0o600)


def prepare_environment(
    output: Path,
    *,
    synthetic: bool = False,
    manifest: dict[str, object] | None = None,
    port: int = 44223,
) -> dict[str, object]:
    """Generate fresh private files; never overwrite an existing identity set."""
    if synthetic == (manifest is not None):
        raise ValueError("choose exactly one of synthetic or an exact deployment manifest")
    if not 1024 <= port <= 65535:
        raise ValueError("local broker port is invalid")
    chosen = _synthetic_manifest() if synthetic else manifest
    assert chosen is not None
    tenant, source, symbol, pair, entries = _validate_manifest(chosen)
    if output.exists() or output.is_symlink():
        raise ValueError("source broker output already exists")
    output.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    now = datetime.now(UTC).replace(microsecond=0)
    expires = now + timedelta(days=2)
    operator_seed, operator_key, operator_public = _keypair(nkeys.PREFIX_BYTE_OPERATOR)
    system_seed, system_key, system_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    account_seed, account_key, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    try:
        operator_jwt = _encode_nats_jwt(
            signer=operator_key,
            subject=operator_public,
            issued_at=now - timedelta(seconds=20),
            nats_claims={"system_account": system_public, "type": "operator", "version": 2},
        )
        system_jwt = _account_jwt(
            operator_pair=operator_key,
            account_public=system_public,
            issued_at=now - timedelta(seconds=15),
            jetstream_enabled=False,
        )
        account_jwt = _account_jwt(
            operator_pair=operator_key,
            account_public=account_public,
            issued_at=now - timedelta(seconds=15),
        )
        _private_write(output / "operator.jwt", operator_jwt.encode())
        _private_write(
            output / "nats.conf",
            _server_config(
                system_account_public=system_public,
                system_account_jwt=system_jwt,
                account_public=account_public,
                account_jwt=account_jwt,
            )
            .replace("/tmp/nats/jetstream", "/data/jetstream")
            .encode(),
        )
        _certificate(output)
        token = secrets.token_urlsafe(32).encode("ascii")
        _private_write(output / "source-token", token)
        admin_seed, admin_key, admin_public = _keypair(nkeys.PREFIX_BYTE_USER)
        try:
            admin_jwt = _unrestricted_user_jwt(
                account_pair=account_key,
                user_public=admin_public,
                issued_at=now - timedelta(seconds=5),
                expires_at=expires,
            )
            _private_write(output / "admin.creds", _credentials(admin_jwt, admin_seed))
        finally:
            admin_key.wipe()
        publisher_seed, publisher_key, publisher_public = _keypair(nkeys.PREFIX_BYTE_USER)
        try:
            publisher_jwt = _scoped_user_jwt(
                account_key=account_key,
                public_key=publisher_public,
                permissions=publisher_permissions(tenant, source),
                issued_at=now - timedelta(seconds=5),
                expires_at=expires,
            )
            publisher_credentials = _credentials(publisher_jwt, publisher_seed)
            _private_write(output / "publisher.creds", publisher_credentials)
        finally:
            publisher_key.wipe()
        _private_write(
            output / "publisher-authorization.json",
            _json_bytes(
                {
                    "schemaVersion": 1,
                    "tenantId": tenant,
                    "sourceId": source,
                    "allowedSymbols": [symbol],
                    "signalTokenSha256": hashlib.sha256(token).hexdigest(),
                    "natsCredentialsSha256": hashlib.sha256(publisher_credentials).hexdigest(),
                }
            ),
        )
        rendered = []
        for index, entry in enumerate(entries):
            authority = EngineLifecycleAuthority(
                deployment_instance_id=UUID(entry["deploymentInstanceId"]),
                deployment_spec_id=UUID(entry["deploymentSpecId"]),
                deployment_spec_digest=entry["deploymentSpecDigest"],
                generation=entry["generation"],
                trading_mode="testnet",
            )
            spec = subscription_spec(
                tenant_id=tenant,
                runner_id=UUID(entry["runnerId"]),
                authority=authority,
                source_id=source,
            )
            subscriber_seed, subscriber_key, subscriber_public = _keypair(nkeys.PREFIX_BYTE_USER)
            try:
                subscriber_jwt = _scoped_user_jwt(
                    account_key=account_key,
                    public_key=subscriber_public,
                    permissions=subscriber_permissions(spec),
                    issued_at=now - timedelta(seconds=5),
                    expires_at=expires,
                )
                subscriber_credentials = _credentials(subscriber_jwt, subscriber_seed)
                _private_write(output / f"subscriber-{index}.creds", subscriber_credentials)
            finally:
                subscriber_key.wipe()
            _private_write(
                output / f"subscription-{index}.json",
                _json_bytes(
                    {
                        "schemaVersion": 1,
                        "tenantId": tenant,
                        "runnerId": entry["runnerId"],
                        "deploymentInstanceId": entry["deploymentInstanceId"],
                        "deploymentSpecId": entry["deploymentSpecId"],
                        "deploymentSpecDigest": entry["deploymentSpecDigest"],
                        "generation": entry["generation"],
                        "tradingMode": "testnet",
                        "sourceId": source,
                        "sourceSymbol": symbol,
                        "targetPair": pair,
                        "natsCredentialsSha256": hashlib.sha256(subscriber_credentials).hexdigest(),
                    }
                ),
            )
            rendered.append(
                {
                    **entry,
                    "durableName": spec.durable_name,
                    "filterSubject": spec.filter_subject,
                    "deliverySubject": spec.delivery_subject,
                    "credentialsSha256": hashlib.sha256(subscriber_credentials).hexdigest(),
                }
            )
        compose_name = f"custos-source-{hashlib.sha256(str(output).encode()).hexdigest()[:10]}"
        _private_write(
            output / "compose.yaml",
            (
                f"name: {compose_name}\nservices:\n  broker:\n    image: {_IMAGE}\n"
                '    command: ["-c", "/config/nats.conf"]\n'
                f'    ports: ["127.0.0.1:{port}:4222"]\n'
                f"    volumes:\n      - {json.dumps(str(output.absolute()) + ':/config:ro')}\n"
                "      - source-broker-store:/data\n"
                "volumes:\n  source-broker-store:\n"
            ).encode(),
        )
        _private_write(output / "manifest.json", _json_bytes(chosen))
        receipt: dict[str, object] = {
            "evidenceGrade": "synthetic_local_only" if synthetic else "unverified_local_broker",
            "brokerUrl": f"nats://127.0.0.1:{port}",
            "serverName": "localhost",
            "streamName": SIGNAL_STREAM_NAME,
            "sourceSubject": signal_subject(tenant, source),
            "operatorPublicKey": operator_public,
            "accountPublicKey": account_public,
            "jwtExpiresAt": expires.isoformat(),
            "tlsExpiresAfterDays": 7,
            "publisherCredentialsSha256": hashlib.sha256(publisher_credentials).hexdigest(),
            "serverConfigSha256": hashlib.sha256((output / "nats.conf").read_bytes()).hexdigest(),
            "composeSha256": hashlib.sha256((output / "compose.yaml").read_bytes()).hexdigest(),
            "subscribers": rendered,
        }
        _private_write(output / "readiness.json", _json_bytes(receipt))
        return receipt
    finally:
        operator_key.wipe()
        system_key.wipe()
        account_key.wipe()
        del operator_seed, system_seed, account_seed
        os.umask(old_umask)


async def start_environment(output: Path) -> dict[str, object]:
    """Start the prepared loopback broker and provision exact stream/consumers."""
    receipt = json.loads(read_private_source_file(output / "readiness.json", max_bytes=16384))
    manifest = json.loads(read_private_source_file(output / "manifest.json", max_bytes=8192))
    tenant, source, symbol, pair, entries = _validate_manifest(manifest)
    broker_url = urlparse(receipt["brokerUrl"])
    if (
        receipt["sourceSubject"] != signal_subject(tenant, source)
        or broker_url.scheme != "nats"
        or broker_url.hostname != "127.0.0.1"
        or broker_url.port is None
        or broker_url.username is not None
        or broker_url.password is not None
        or broker_url.path not in {"", "/"}
        or broker_url.query
        or broker_url.fragment
        or hashlib.sha256(
            read_private_source_file(output / "nats.conf", max_bytes=16384)
        ).hexdigest()
        != receipt["serverConfigSha256"]
        or hashlib.sha256(
            read_private_source_file(output / "compose.yaml", max_bytes=4096)
        ).hexdigest()
        != receipt["composeSha256"]
    ):
        raise ValueError("source broker prepared configuration differs")
    publisher_bytes = read_private_source_file(output / "publisher.creds", max_bytes=65536)
    if hashlib.sha256(publisher_bytes).hexdigest() != receipt["publisherCredentialsSha256"]:
        raise ValueError("source publisher credential differs from preparation")
    publisher = build_settings(
        SimpleNamespace(
            authorization=output / "publisher-authorization.json",
            nats_credentials=output / "publisher.creds",
            outbox=output / "source.sqlite",
            nats_url=receipt["brokerUrl"],
            nats_ca=output / "server.crt",
            nats_server_name="localhost",
            listen_port=8765,
        )
    )
    token = read_private_source_file(output / "source-token", max_bytes=512)
    if (
        publisher.ingress.tenant_id != tenant
        or publisher.ingress.source_id != source
        or publisher.ingress.allowed_symbols != frozenset({symbol})
        or publisher.ingress.signal_token_sha256 != bytes.fromhex(hashlib.sha256(token).hexdigest())
    ):
        raise ValueError("source publisher authorization differs from prepared source")
    specs = []
    for index, entry in enumerate(entries):
        authority = EngineLifecycleAuthority(
            UUID(entry["deploymentInstanceId"]),
            UUID(entry["deploymentSpecId"]),
            entry["deploymentSpecDigest"],
            entry["generation"],
            "testnet",
        )
        scope = ExternalSourceRuntimeScope(
            authority=authority,
            connector=entry["connector"],
            allowed_pairs=frozenset({pair}),
            authorized_leverage=1,
        )
        grant = load_source_subscription(
            output / f"subscription-{index}.json",
            credentials_path=output / f"subscriber-{index}.creds",
            expected_tenant_id=tenant,
            expected_runner_id=UUID(entry["runnerId"]),
            active_scope=scope,
        )
        expected = receipt["subscribers"][index]
        if (
            expected["durableName"] != grant.spec.durable_name
            or expected["filterSubject"] != grant.spec.filter_subject
            or expected["deliverySubject"] != grant.spec.delivery_subject
            or expected["credentialsSha256"] != grant.credentials_sha256
            or entry["accountAlias"] != expected["accountAlias"]
        ):
            raise ValueError("source subscriber differs from prepared authority")
        specs.append(grant)
    if symbol != f"{pair}-SWAP":
        raise ValueError("source broker instrument mapping differs")
    started = subprocess.run(
        ["docker", "compose", "-f", str(output / "compose.yaml"), "up", "-d"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if started.returncode != 0:
        raise RuntimeError("local source broker could not start")
    context = ssl.create_default_context(cafile=str(output / "server.crt"))
    context.check_hostname = True
    connection = None
    last_error: Exception | None = None
    for _ in range(20):
        try:
            connection = await nats.connect(
                servers=[receipt["brokerUrl"]],
                user_credentials=RawCredentials((output / "admin.creds").read_text()),
                tls=context,
                tls_hostname="localhost",
                allow_reconnect=False,
                connect_timeout=1,
            )
            break
        except Exception as error:  # noqa: BLE001 - bounded local readiness retry
            last_error = error
            await asyncio.sleep(0.2)
    if connection is None:
        raise RuntimeError("local source broker did not authenticate") from last_error
    try:
        jetstream = connection.jetstream()
        try:
            info = await jetstream.stream_info(SIGNAL_STREAM_NAME)
        except NotFoundError:
            info = await jetstream.add_stream(
                name=SIGNAL_STREAM_NAME, subjects=["custos.external-source.v1.>"]
            )
        if info.config.subjects != ["custos.external-source.v1.>"]:
            raise RuntimeError("local source stream subject differs")
        for grant in specs:
            spec = grant.spec
            try:
                consumer = await jetstream.consumer_info(SIGNAL_STREAM_NAME, spec.durable_name)
            except NotFoundError:
                consumer = await jetstream.add_consumer(
                    SIGNAL_STREAM_NAME, config=spec.consumer_config
                )
            actual = consumer.config
            if (
                actual.durable_name != spec.durable_name
                or actual.deliver_subject != spec.delivery_subject
                or actual.filter_subject != spec.filter_subject
                or actual.ack_policy != spec.consumer_config.ack_policy
                or actual.deliver_policy != spec.consumer_config.deliver_policy
            ):
                raise RuntimeError("local source durable differs from prepared authority")
    finally:
        await connection.close()
    publisher = await SourcePublisherNatsProfile(
        tenant_id=tenant,
        source_id=source,
        credentials_path=output / "publisher.creds",
        credentials_sha256=receipt["publisherCredentialsSha256"],
        url=receipt["brokerUrl"],
        ca_path=output / "server.crt",
        server_name="localhost",
    ).connect()
    try:
        for grant in specs:
            subscriber = await ExternalSourceNatsProfile(
                grant, receipt["brokerUrl"], output / "server.crt", "localhost"
            ).connect()
            try:
                bound = await bind_signal_consumer(subscriber.jetstream(), grant.spec)
                await bound.unsubscribe()
            finally:
                await subscriber.close()
    finally:
        await publisher.close()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare one local-only source test broker.")
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--output", type=Path, required=True)
    group = prepare.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic", action="store_true")
    group.add_argument("--manifest", type=Path)
    prepare.add_argument("--port", type=int, default=44223)
    start = actions.add_parser("start")
    start.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository_dist = Path(__file__).resolve().parents[2] / "dist"
    if not args.output.resolve().is_relative_to(repository_dist.resolve()):
        raise ValueError("local broker output must stay under this repository's ignored dist/")
    if args.action == "prepare":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        manifest = json.loads(args.manifest.read_text()) if args.manifest else None
        result = prepare_environment(
            args.output, synthetic=args.synthetic, manifest=manifest, port=args.port
        )
    else:
        result = asyncio.run(start_environment(args.output))
    print(
        json.dumps(
            {
                "evidenceGrade": result["evidenceGrade"],
                "brokerUrl": result["brokerUrl"],
                "streamName": result["streamName"],
                "durableNames": [entry["durableName"] for entry in result["subscribers"]],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

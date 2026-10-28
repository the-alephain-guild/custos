"""A real isolated NATS server enforces separate source publisher and target users."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import nats
import nkeys  # type: ignore[import-untyped]
import pytest
from nats.aio.client import RawCredentials
from test_nats_revocation import (
    _account_jwt,
    _connect_admin,
    _encode_nats_jwt,
    _keypair,
    _run,
    _server_config,
    _unrestricted_user_jwt,
    _wait_ready,
    _write_tls_material,
)

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    SourcePublicationWorker,
    publisher_permissions,
    signal_subject,
    subscriber_permissions,
    subscription_spec,
)
from custos.engines.nautilus.external_source_authorization import load_source_subscription
from custos.engines.nautilus.external_source_nats import ExternalSourceNatsProfile
from custos.engines.nautilus.external_source_runner_session import ExternalSourceRunnerSession
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

IMAGE = "nats:2.10-alpine"


def _user_jwt(account_pair, user_public: str, permissions: dict, now: datetime) -> str:
    return _encode_nats_jwt(
        signer=account_pair,
        subject=user_public,
        issued_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(hours=1),
        nats_claims={
            "pub": {"allow": permissions["publish_allow"], "deny": permissions["publish_deny"]},
            "sub": {"allow": permissions["subscribe_allow"], "deny": permissions["subscribe_deny"]},
            "subs": -1,
            "data": -1,
            "payload": -1,
            "type": "user",
            "version": 2,
        },
    )


def _credentials(jwt: str, seed: bytes) -> bytes:
    return (
        "-----BEGIN NATS USER JWT-----\n"
        f"{jwt}\n"
        "------END NATS USER JWT------\n\n"
        "-----BEGIN USER NKEY SEED-----\n"
        f"{seed.decode('ascii')}\n"
        "------END USER NKEY SEED------\n"
    ).encode()


class Host:
    def __init__(self, scope: ExternalSourceRuntimeScope) -> None:
        self.scope = scope
        self.queued = []

    def external_source_runtime_scope(self, instance_id: str) -> ExternalSourceRuntimeScope:
        assert instance_id == str(self.scope.authority.deployment_instance_id)
        return self.scope

    def external_source_flat_reader(self, authority):
        assert authority == self.scope.authority
        return None

    async def external_source_flat_local_clear(self, authority, pair):
        assert authority == self.scope.authority and pair in self.scope.allowed_pairs
        return True

    def queue_external_source_signal(self, authority, signal) -> bool:
        assert authority == self.scope.authority
        self.queued.append(signal)
        return True

    async def request_external_source_flatten(self, authority, *, risk_action=None) -> int:
        raise AssertionError("entry must not flatten")


async def _exercise(
    root: Path,
    *,
    url: str,
    certificate: Path,
    admin_jwt: str,
    admin_seed: bytes,
    account_pair,
) -> None:
    tenant = f"synthetic-{uuid4().hex[:10]}"
    source = "synthetic-supertrend"
    now = datetime.now(UTC)
    bar = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    runner_ids = [uuid4(), uuid4()]
    authorities = [
        EngineLifecycleAuthority(uuid4(), uuid4(), "a" * 64, 1, "testnet"),
        EngineLifecycleAuthority(uuid4(), uuid4(), "b" * 64, 1, "testnet"),
    ]
    specs = [
        subscription_spec(tenant_id=tenant, runner_id=runner, authority=authority, source_id=source)
        for runner, authority in zip(runner_ids, authorities, strict=True)
    ]
    pub_seed, pub_pair, pub_public = _keypair(nkeys.PREFIX_BYTE_USER)
    sub_keys = [_keypair(nkeys.PREFIX_BYTE_USER) for _ in range(2)]
    try:
        publisher_jwt = _user_jwt(
            account_pair, pub_public, publisher_permissions(tenant, source), now
        )
        subscriber_jwts = [
            _user_jwt(account_pair, key[2], subscriber_permissions(spec), now)
            for key, spec in zip(sub_keys, specs, strict=True)
        ]
    finally:
        pub_pair.wipe()
        for _, pair, _ in sub_keys:
            pair.wipe()

    admin = await _connect_admin(
        user_jwt=admin_jwt, user_seed=admin_seed, nats_url=url, certificate=certificate
    )
    stream = admin.jetstream()
    await stream.add_stream(name=SIGNAL_STREAM_NAME, subjects=["custos.external-source.v1.>"])
    for spec in specs:
        await stream.add_consumer(SIGNAL_STREAM_NAME, config=spec.consumer_config)

    tls = ssl.create_default_context(cafile=str(certificate))
    publisher = await nats.connect(
        servers=[url],
        user_credentials=RawCredentials(_credentials(publisher_jwt, pub_seed).decode()),
        tls=tls,
        tls_hostname="localhost",
        allow_reconnect=False,
    )
    subscribers = []
    try:
        for index, (runner, authority, _spec) in enumerate(
            zip(runner_ids, authorities, specs, strict=True)
        ):
            credential_path = root / f"subscriber-{index}.creds"
            credential_path.write_bytes(_credentials(subscriber_jwts[index], sub_keys[index][0]))
            credential_path.chmod(0o600)
            authorization_path = root / f"subscription-{index}.json"
            authorization_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "tenantId": tenant,
                        "runnerId": str(runner),
                        "deploymentInstanceId": str(authority.deployment_instance_id),
                        "deploymentSpecId": str(authority.deployment_spec_id),
                        "deploymentSpecDigest": authority.deployment_spec_digest,
                        "generation": authority.generation,
                        "tradingMode": authority.trading_mode,
                        "sourceId": source,
                        "sourceSymbol": "ETH-USDT-SWAP",
                        "targetPair": "ETH-USDT",
                        "natsCredentialsSha256": hashlib.sha256(
                            credential_path.read_bytes()
                        ).hexdigest(),
                    }
                )
            )
            authorization_path.chmod(0o600)
            scope = ExternalSourceRuntimeScope(
                authority=authority,
                connector=("okx_perpetual", "binance_perpetual")[index],
                allowed_pairs=frozenset({"ETH-USDT"}),
                authorized_leverage=3,
            )
            grant = load_source_subscription(
                authorization_path,
                credentials_path=credential_path,
                expected_tenant_id=tenant,
                expected_runner_id=runner,
                active_scope=scope,
            )
            connection = await ExternalSourceNatsProfile(
                grant, url, certificate, "localhost"
            ).connect()
            host = Host(scope)
            session = ExternalSourceRunnerSession(
                authorization_path=authorization_path,
                credentials_path=credential_path,
                tenant_id=tenant,
                runner_id=runner,
                authority=authority,
                host=host,
                inbox=ExternalTargetInbox(root / f"target-{index}.sqlite"),
                broker=connection.jetstream(),
            )
            await session.bind(at=now)
            subscribers.append((connection, session, host))

        event_id = make_source_event_id(
            source_id=source,
            strategy_revision="v1",
            symbol="ETH-USDT-SWAP",
            timeframe="30",
            bar_close_time=bar,
            target="LONG",
            risk_action=None,
        )
        event = parse_external_source_signal(
            {
                "schemaVersion": 1,
                "sourceId": source,
                "strategyRevision": "v1",
                "symbol": "ETH-USDT-SWAP",
                "timeframe": "30",
                "barCloseTime": bar.isoformat(),
                "timestamp": now.isoformat(),
                "maxLag": 3600,
                "target": "LONG",
                "eventId": event_id,
                "signalToken": "synthetic-only-never-brokered-0123456789",
            },
            tenant_id=tenant,
            expected_source_id=source,
            allowed_symbols=frozenset({"ETH-USDT-SWAP"}),
            now=now,
        )
        outbox = ExternalSourceOutbox(root / "source.sqlite")
        outbox.accept(event, received_at=now)
        assert await SourcePublicationWorker(
            outbox, publisher.jetstream(), tenant_id=tenant
        ).publish_once(at=now)
        deliveries = []
        for _connection, session, host in subscribers:
            message = await session.subscription.next_msg(timeout=3)
            assert b"signalToken" not in message.data
            receipt = await session.handle_message(message, at=datetime.now(UTC))
            assert receipt is not None and receipt.status == "queued"
            assert len(host.queued) == 1 and host.queued[0].amount is None
            deliveries.append(receipt.delivery_id)
        assert len(set(deliveries)) == 2
        assert (await stream.stream_info(SIGNAL_STREAM_NAME)).state.messages == 1
        with pytest.raises((nats.errors.Error, TimeoutError)):
            await (
                subscribers[0][0]
                .jetstream()
                .publish(
                    signal_subject(tenant, source),
                    b"unauthorized",
                    timeout=0.5,
                    stream=SIGNAL_STREAM_NAME,
                )
            )
        assert (await stream.stream_info(SIGNAL_STREAM_NAME)).state.messages == 1
    finally:
        for connection, session, _ in subscribers:
            if session.subscription is not None:
                await session.subscription.unsubscribe()
            await connection.close()
        await publisher.close()
        await admin.close()


@pytest.mark.integration
@pytest.mark.docker
def test_isolated_jwt_broker_fans_out_to_two_exact_subscribers(tmp_path: Path) -> None:
    if os.environ.get("CUSTOS_RUN_REAL_EXTERNAL_SOURCE") != "1":
        pytest.skip("set CUSTOS_RUN_REAL_EXTERNAL_SOURCE=1 for isolated JWT broker")
    if shutil.which("docker") is None or shutil.which("openssl") is None:
        pytest.fail("Docker and OpenSSL are required for the isolated JWT broker")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.fail("preload the pinned NATS test image")
    now = datetime.now(UTC).replace(microsecond=0)
    operator_seed, operator_pair, operator_public = _keypair(nkeys.PREFIX_BYTE_OPERATOR)
    system_seed, system_pair, system_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    admin_seed, admin_pair, admin_public = _keypair(nkeys.PREFIX_BYTE_USER)
    try:
        operator_jwt = _encode_nats_jwt(
            signer=operator_pair,
            subject=operator_public,
            issued_at=now - timedelta(seconds=20),
            nats_claims={"system_account": system_public, "type": "operator", "version": 2},
        )
        system_jwt = _account_jwt(
            operator_pair=operator_pair,
            account_public=system_public,
            issued_at=now - timedelta(seconds=15),
            jetstream_enabled=False,
        )
        account_jwt = _account_jwt(
            operator_pair=operator_pair,
            account_public=account_public,
            issued_at=now - timedelta(seconds=15),
        )
        admin_jwt = _unrestricted_user_jwt(
            account_pair=account_pair,
            user_public=admin_public,
            issued_at=now - timedelta(seconds=5),
            expires_at=now + timedelta(hours=1),
        )
        certificate = _write_tls_material(tmp_path)
        (tmp_path / "operator.jwt").write_text(operator_jwt)
        (tmp_path / "nats.conf").write_text(
            _server_config(
                system_account_public=system_public,
                system_account_jwt=system_jwt,
                account_public=account_public,
                account_jwt=account_jwt,
            )
        )
        tmp_path.chmod(0o755)
        certificate.chmod(0o644)
        (tmp_path / "operator.jwt").chmod(0o644)
        (tmp_path / "nats.conf").chmod(0o644)
        container = f"custos-source-jwt-{uuid4().hex}"
        _run(
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-p",
            "127.0.0.1::4222",
            "-v",
            f"{tmp_path}:/config",
            IMAGE,
            "-c",
            "/config/nats.conf",
        )
        try:
            _wait_ready(container)
            port = int(
                _run("docker", "port", container, "4222/tcp").stdout.strip().rsplit(":", 1)[1]
            )
            asyncio.run(
                _exercise(
                    tmp_path,
                    url=f"tls://localhost:{port}",
                    certificate=certificate,
                    admin_jwt=admin_jwt,
                    admin_seed=admin_seed,
                    account_pair=account_pair,
                )
            )
        finally:
            _run("docker", "stop", "-t", "1", container, check=False)
    finally:
        for pair in (operator_pair, system_pair, account_pair, admin_pair):
            pair.wipe()
        del operator_seed, system_seed, account_seed

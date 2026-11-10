"""Inbound-only runner-control command and policy durable binding tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, ReplayPolicy

from custos.core.nats_client import CrucibleNatsClient
from custos.core.nats_transport import RunnerNatsTransportError

_RUNNER_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass
class _MachineCredential:
    tenant_id: str = "acme"
    runner_id: UUID = _RUNNER_ID
    active: bool = True

    def assert_active(self) -> None:
        if not self.active:
            raise RuntimeError("machine credential is inactive")


@dataclass
class _TransportProfile:
    tenant_id: str = "acme"
    runner_id: UUID = _RUNNER_ID
    active: bool = True
    trading_mode: str = "sandbox"
    durable_config: dict[str, object] = field(
        default_factory=lambda: {
            "transport_domain": "sim",
            "stream_name": "CRUCIBLE_RUNNER_CONTROL_SIM_V1",
            "durable_name": f"custos-control-v1-acme-{_RUNNER_ID}-sandbox",
            "delivery_subject": (f"custos.runner.control.v1.delivery.acme.{_RUNNER_ID}.sandbox"),
            "filter_subjects": [
                f"crucible.runner.command.v1.acme.{_RUNNER_ID}.sandbox",
                f"crucible.runner.policy.v1.acme.{_RUNNER_ID}.sandbox",
            ],
        }
    )

    def assert_active(self) -> None:
        if not self.active:
            raise RunnerNatsTransportError("transport inactive")


def _client(
    credential: _MachineCredential | None = None,
    profile: _TransportProfile | None = None,
) -> CrucibleNatsClient:
    return CrucibleNatsClient(
        connection_profile=profile or _TransportProfile(),  # type: ignore[arg-type]
        tenant_id="acme",
        runner_id=str(_RUNNER_ID),
        machine_credential=credential or _MachineCredential(),
    )


def test_client_rejects_machine_authority_binding_mismatch() -> None:
    credential = _MachineCredential(tenant_id="other-tenant")
    with pytest.raises(ValueError, match="authority does not match"):
        _client(credential)


def test_client_rejects_transport_authority_binding_mismatch() -> None:
    profile = _TransportProfile(tenant_id="other-tenant")
    with pytest.raises(ValueError, match="authority does not match"):
        _client(profile=profile)


def test_client_rejects_inactive_machine_credential() -> None:
    with pytest.raises(RuntimeError, match="inactive"):
        _client(_MachineCredential(active=False))


@pytest.mark.asyncio
async def test_subscribe_binds_existing_exact_tenant_runner_durable() -> None:
    client = _client()
    expected = client.connection_profile.durable_config
    config = ConsumerConfig(
        durable_name=str(expected["durable_name"]),
        deliver_subject=str(expected["delivery_subject"]),
        filter_subjects=list(expected["filter_subjects"]),
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        replay_policy=ReplayPolicy.INSTANT,
        max_ack_pending=1,
    )
    subscription = object()
    client._jsm = MagicMock(  # noqa: SLF001 - isolated broker management seam
        consumer_info=AsyncMock(return_value=SimpleNamespace(config=config))
    )
    client._js = MagicMock(  # noqa: SLF001 - isolated JetStream binding seam
        subscribe_bind=AsyncMock(return_value=subscription)
    )

    result = await client.subscribe_control()

    assert result is subscription
    client._jsm.consumer_info.assert_awaited_once_with(  # noqa: SLF001
        "CRUCIBLE_RUNNER_CONTROL_SIM_V1",
        f"custos-control-v1-acme-{_RUNNER_ID}-sandbox",
    )
    client._js.subscribe_bind.assert_awaited_once_with(  # noqa: SLF001
        stream="CRUCIBLE_RUNNER_CONTROL_SIM_V1",
        config=config,
        consumer=f"custos-control-v1-acme-{_RUNNER_ID}-sandbox",
        manual_ack=True,
    )


@pytest.mark.asyncio
async def test_existing_consumer_drift_fails_before_subscription() -> None:
    client = _client()
    config = ConsumerConfig(
        durable_name=f"custos-control-v1-acme-{_RUNNER_ID}-sandbox",
        deliver_subject="custos.runner.control.v1.delivery.other.runner.sandbox",
        filter_subjects=[],
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        replay_policy=ReplayPolicy.INSTANT,
        max_ack_pending=1,
    )
    client._jsm = MagicMock(  # noqa: SLF001
        consumer_info=AsyncMock(return_value=SimpleNamespace(config=config))
    )
    client._js = MagicMock(subscribe_bind=AsyncMock())  # noqa: SLF001

    with pytest.raises(RunnerNatsTransportError, match="does not match"):
        await client.subscribe_control()

    client._js.subscribe_bind.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_subscribe_before_connect_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="before connect"):
        await _client().subscribe_control()


def test_command_subject_and_verified_payload_must_match_exact_mode_session() -> None:
    client = _client()
    subject = str(client.connection_profile.durable_config["filter_subjects"][0])

    client.assert_command_binding(subject, SimpleNamespace(trading_mode="sandbox"))

    with pytest.raises(RunnerNatsTransportError, match="subject is outside"):
        client.assert_command_binding(
            subject.removesuffix("sandbox") + "live",
            SimpleNamespace(trading_mode="sandbox"),
        )
    with pytest.raises(RunnerNatsTransportError, match="payload mode differs"):
        client.assert_command_binding(subject, SimpleNamespace(trading_mode="live"))


def test_policy_subject_and_verified_payload_must_match_exact_mode_session() -> None:
    client = _client()
    subject = str(client.connection_profile.durable_config["filter_subjects"][1])

    client.assert_policy_binding(subject, SimpleNamespace(trading_mode="sandbox"))

    with pytest.raises(RunnerNatsTransportError, match="subject is outside"):
        client.assert_policy_binding(
            subject.removesuffix("sandbox") + "live",
            SimpleNamespace(trading_mode="sandbox"),
        )
    with pytest.raises(RunnerNatsTransportError, match="payload mode differs"):
        client.assert_policy_binding(subject, SimpleNamespace(trading_mode="live"))


def test_opening_release_subject_uses_the_authenticated_uuid():
    runner = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
    subject = f"crucible.runner.opening-release.v1.acme.{runner}.sandbox"
    profile = _TransportProfile(
        runner_id=runner,
        durable_config={
            "filter_subjects": [
                f"crucible.runner.command.v1.acme.{runner}.sandbox",
                f"crucible.runner.policy.v1.acme.{runner}.sandbox",
                subject,
            ]
        },
    )
    client = CrucibleNatsClient(
        connection_profile=profile,
        tenant_id="acme",
        runner_id=str(runner).upper(),
        machine_credential=_MachineCredential(runner_id=runner),
    )
    client.assert_opening_release_binding(subject)

"""Inbound-only Crucible control transport over an existing runner-control durable."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from nats.js.api import AckPolicy, DeliverPolicy, ReplayPolicy

from custos.core.nats_transport import (
    RunnerNatsConnectionProfile,
    RunnerNatsTransportError,
)


@dataclass
class CrucibleNatsClient:
    """Bind, but never create, the exact tenant+runner command+policy consumer."""

    connection_profile: RunnerNatsConnectionProfile
    tenant_id: str
    runner_id: str
    machine_credential: Any
    _nc: Any = field(default=None, init=False, repr=False)
    _js: Any = field(default=None, init=False, repr=False)
    _jsm: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.machine_credential.assert_active()
        runner_id = UUID(self.runner_id)
        if (
            self.machine_credential.tenant_id != self.tenant_id
            or self.machine_credential.runner_id != runner_id
            or self.connection_profile.tenant_id != self.tenant_id
            or self.connection_profile.runner_id != runner_id
        ):
            raise ValueError("runner NATS authority does not match tenant/runner identity")
        self.connection_profile.assert_active()

    async def connect(self) -> None:
        self.machine_credential.assert_active()
        self.connection_profile.assert_active()
        self._nc = await self.connection_profile.connect(
            name=(
                f"custos-control-{self.tenant_id}-{self.runner_id}-"
                f"{self.connection_profile.trading_mode}"
            )
        )
        self._js = self._nc.jetstream()
        self._jsm = self._js

    async def close(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            await self._nc.drain()
        self._nc = None
        self._js = None
        self._jsm = None

    async def subscribe_control(self) -> Any:
        self.machine_credential.assert_active()
        self.connection_profile.assert_active()
        if self._js is None or self._jsm is None:
            raise RuntimeError("NATS subscribe attempted before connect")
        expected = self.connection_profile.durable_config
        stream = str(expected["stream_name"])
        durable = str(expected["durable_name"])
        info = await self._jsm.consumer_info(stream, durable)
        config = info.config
        _assert_existing_consumer(config, expected)
        return await self._js.subscribe_bind(
            stream=stream,
            config=config,
            consumer=durable,
            manual_ack=True,
        )

    def assert_command_binding(self, subject: str, command: Any) -> None:
        """Bind the broker subject and verified payload to this exact-mode session."""

        expected_subjects = list(self.connection_profile.durable_config["filter_subjects"])
        if len(expected_subjects) != 2 or expected_subjects[0] != subject:
            raise RunnerNatsTransportError(
                "runner command subject is outside the exact runner-control mode authority"
            )
        if getattr(command, "trading_mode", None) != self.connection_profile.trading_mode:
            raise RunnerNatsTransportError(
                "runner command payload mode differs from the authenticated session"
            )

    def assert_policy_binding(self, subject: str, policy: Any) -> None:
        """Bind the runner-safety policy subject and verified body to this exact-mode session."""

        expected_subjects = list(self.connection_profile.durable_config["filter_subjects"])
        if len(expected_subjects) != 2 or expected_subjects[1] != subject:
            raise RunnerNatsTransportError(
                "runner policy subject is outside the exact runner-control mode authority"
            )
        if getattr(policy, "trading_mode", None) != self.connection_profile.trading_mode:
            raise RunnerNatsTransportError(
                "runner policy payload mode differs from the authenticated session"
            )


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _assert_existing_consumer(config: Any, expected: Any) -> None:
    filters = list(config.filter_subjects or ())
    if not filters and config.filter_subject:
        filters = [config.filter_subject]
    actual = {
        "durable_name": config.durable_name,
        "delivery_subject": config.deliver_subject,
        "filter_subjects": filters,
        "deliver_policy": _enum_value(config.deliver_policy),
        "ack_policy": _enum_value(config.ack_policy),
        "replay_policy": _enum_value(config.replay_policy),
        "max_ack_pending": config.max_ack_pending,
    }
    required = {
        "durable_name": expected["durable_name"],
        "delivery_subject": expected["delivery_subject"],
        "filter_subjects": list(expected["filter_subjects"]),
        "deliver_policy": DeliverPolicy.ALL.value,
        "ack_policy": AckPolicy.EXPLICIT.value,
        "replay_policy": ReplayPolicy.INSTANT.value,
        "max_ack_pending": 1,
    }
    if actual != required:
        raise RunnerNatsTransportError(
            "existing runner-control durable does not match signed authority"
        )

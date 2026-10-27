"""Accept broker source events into one deployment inbox before acknowledging them."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from custos.core.external_source_signal import (
    ExternalSourceSignalError,
    parse_external_source_signal,
)
from custos.core.external_source_transport import SignalSubscriptionSpec
from custos.engines.nautilus.external_source_delivery import (
    SourceSubscriptionBinding,
    plan_target_delivery,
)
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox, TargetIntakeReceipt
from custos.engines.nautilus.host import ExternalSourceRuntimeScope


class ExternalSourceSubscriberError(ValueError):
    """A broker message did not match the bound source subscription."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalSourceSubscriberError("source broker JSON has duplicate fields")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ExternalSourceSubscriberError("source broker JSON has a nonfinite number")


class ExternalSourceSubscriber:
    """One durable consumer, one exact deployment and one private target inbox."""

    def __init__(
        self,
        spec: SignalSubscriptionSpec,
        binding: SourceSubscriptionBinding,
        inbox: ExternalTargetInbox,
        *,
        scope_provider: Callable[[], ExternalSourceRuntimeScope],
    ) -> None:
        if (
            spec.tenant_id != binding.tenant_id
            or spec.source_id != binding.source_id
            or spec.authority != binding.authority
        ):
            raise ExternalSourceSubscriberError("subscription differs from target binding")
        self.spec = spec
        self.binding = binding
        self.inbox = inbox
        self.scope_provider = scope_provider

    async def handle_message(self, message: Any, *, at: datetime) -> TargetIntakeReceipt | None:
        """Ack only after durable acceptance; term a malformed broker payload."""
        if message.subject != self.spec.filter_subject or not isinstance(message.data, bytes):
            await message.term()
            return None
        if not 0 < len(message.data) <= 4096:
            await message.term()
            return None
        try:
            payload = json.loads(
                message.data.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=_reject_nonfinite,
                object_pairs_hook=_unique_object,
            )
            if (
                not isinstance(payload, dict)
                or payload.get("tenantId") != self.binding.tenant_id
                or "signalToken" in payload
            ):
                raise ExternalSourceSubscriberError("source broker envelope is untrusted")
            event = parse_external_source_signal(
                payload,
                tenant_id=self.binding.tenant_id,
                expected_source_id=self.binding.source_id,
                allowed_symbols=frozenset({self.binding.source_symbol}),
                now=at,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ExternalSourceSignalError,
            ExternalSourceSubscriberError,
        ):
            await message.term()
            return None
        scope = self.scope_provider()
        directive = plan_target_delivery(event, self.binding, scope, at=at)
        receipt = await asyncio.to_thread(self.inbox.accept, directive, received_at=at)
        await message.ack()
        return receipt

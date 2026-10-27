"""Each deployment persists and reconciles its own source delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from custos_toolkit.signals.types import Signal

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.engines.nautilus.external_source_delivery import (
    SourceSubscriptionBinding,
    plan_target_delivery,
)
from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FIRST = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000001"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)
SECOND = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000002"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002"),
    deployment_spec_digest="b" * 64,
    generation=1,
    trading_mode="testnet",
)


def directive(
    authority: EngineLifecycleAuthority,
    *,
    source_id: str = "synthetic-supertrend",
    target: str = "SHORT",
    risk_action: str | None = None,
    bar: datetime = BAR,
    received_at: datetime = NOW,
):
    payload = {
        "schemaVersion": 1,
        "sourceId": source_id,
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": bar.isoformat(),
        "timestamp": bar.isoformat(),
        "maxLag": 60,
        "target": target,
        "eventId": make_source_event_id(
            source_id=source_id,
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=bar,
            target=target,
            risk_action=risk_action,
        ),
    }
    if risk_action is not None:
        payload["riskAction"] = risk_action
    source = parse_external_source_signal(
        payload,
        tenant_id="tenant-a",
        expected_source_id=source_id,
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=received_at,
    )
    binding = SourceSubscriptionBinding(
        tenant_id="tenant-a",
        source_id=source_id,
        source_symbol="ETH-USDT",
        target_pair="ETH-USDT",
        authority=authority,
    )
    scope = ExternalSourceRuntimeScope(
        authority=authority,
        connector="okx_perpetual" if authority == FIRST else "binance_perpetual",
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )
    return plan_target_delivery(source, binding, scope, at=received_at)


def test_one_source_event_has_two_independent_target_receipts(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    first, second = directive(FIRST), directive(SECOND)
    assert first.source_event_id == second.source_event_id
    assert first.delivery_id != second.delivery_id
    assert inbox.accept(first, received_at=NOW).status == "accepted"
    assert inbox.accept(second, received_at=NOW).status == "accepted"
    assert inbox.accept(first, received_at=NOW).status == "duplicate"
    first_pending = inbox.pending(FIRST, at=NOW)
    assert [item.delivery_id for item in first_pending] == [first.delivery_id]
    assert first_pending[0].emitted_at_ns == 1_793_102_400_000_000_000
    assert first_pending[0].max_lag == 60
    recovered_signal = ExternalTargetInbox(inbox.path).pending(FIRST, at=NOW)[0].to_signal()
    assert recovered_signal is not None
    assert recovered_signal.amount is None
    assert recovered_signal.timestamp == first_pending[0].emitted_at_ns
    assert recovered_signal.metadata["target_delivery_id"] == first.delivery_id
    assert [item.delivery_id for item in inbox.pending(SECOND, at=NOW)] == [second.delivery_id]
    assert inbox.begin_delivery(first.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(first.delivery_id, authority=FIRST, at=NOW)
    assert [item.delivery_id for item in inbox.pending(SECOND, at=NOW)] == [second.delivery_id]
    assert inbox.mark_handoff(second.delivery_id, authority=SECOND, at=NOW) is False


def test_one_deployment_cannot_mix_unrelated_signal_sources(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    inbox.accept(directive(FIRST), received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="source"):
        inbox.accept(directive(FIRST, source_id="other-source"), received_at=NOW)


def test_flat_containment_replaces_a_full_pending_entry_queue(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite", max_pending=1)
    entry = directive(FIRST)
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(entry, received_at=NOW)
    assert inbox.accept(risk, received_at=NOW).status == "accepted"
    assert [item.delivery_id for item in inbox.pending(FIRST, at=NOW)] == [risk.delivery_id]


def test_newer_risk_flat_supersedes_unrequested_normal_flat(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite", max_pending=1)
    normal = directive(FIRST, target="FLAT")
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(normal, received_at=NOW)
    inbox.accept(risk, received_at=NOW)
    assert [item.delivery_id for item in inbox.pending(FIRST, at=NOW)] == [risk.delivery_id]
    other = ExternalTargetInbox(tmp_path / "risk-first.sqlite")
    other.accept(risk, received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="risk flatten"):
        other.accept(normal, received_at=NOW)


def test_flat_fence_is_per_deployment_and_requires_confirmation(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(risk, received_at=NOW)
    later_bar = BAR + timedelta(minutes=30)
    later_time = NOW + timedelta(minutes=30)
    first_entry = directive(FIRST, target="LONG", bar=later_bar, received_at=later_time)
    second_entry = directive(SECOND, target="LONG", bar=later_bar, received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="flat confirmation"):
        inbox.accept(first_entry, received_at=later_time)
    assert inbox.accept(second_entry, received_at=later_time).status == "accepted"
    assert inbox.confirm_flat(risk.delivery_id, authority=FIRST, at=NOW) is False
    assert inbox.confirm_flat(risk.delivery_id, authority=SECOND, at=NOW) is False
    assert inbox.begin_delivery(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.confirm_flat(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.accept(first_entry, received_at=later_time).status == "accepted"


def test_restart_unknown_entry_blocks_reentry_until_new_flat(tmp_path: Path) -> None:
    path = tmp_path / "target.sqlite"
    inbox = ExternalTargetInbox(path)
    first = directive(FIRST)
    inbox.accept(first, received_at=NOW)
    assert inbox.begin_delivery(first.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(first.delivery_id, authority=FIRST, at=NOW)
    restarted = ExternalTargetInbox(path)
    assert restarted.recover_uncertain(FIRST.deployment_instance_id, at=NOW) == 1
    later_bar = BAR + timedelta(minutes=30)
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=later_bar, received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="uncertain"):
        restarted.accept(later, received_at=later_time)
    risk = directive(
        FIRST, target="FLAT", risk_action="FLATTEN", bar=later_bar, received_at=later_time
    )
    restarted.accept(risk, received_at=later_time)
    assert restarted.begin_delivery(risk.delivery_id, authority=FIRST, at=later_time)
    assert restarted.mark_handoff(risk.delivery_id, authority=FIRST, at=later_time)
    assert restarted.confirm_flat(risk.delivery_id, authority=FIRST, at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="latest flat"):
        restarted.accept(later, received_at=later_time)
    newest_time = later_time + timedelta(minutes=30)
    newest = directive(
        FIRST, target="LONG", bar=later_bar + timedelta(minutes=30), received_at=newest_time
    )
    assert restarted.accept(newest, received_at=newest_time).status == "accepted"


def test_restart_does_not_dispatch_an_already_accepted_new_entry(tmp_path: Path) -> None:
    path = tmp_path / "target.sqlite"
    inbox = ExternalTargetInbox(path)
    first = directive(FIRST)
    inbox.accept(first, received_at=NOW)
    assert inbox.begin_delivery(first.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(first.delivery_id, authority=FIRST, at=NOW)
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=BAR + timedelta(minutes=30), received_at=later_time)
    inbox.accept(later, received_at=later_time)

    restarted = ExternalTargetInbox(path)
    assert restarted.recover_uncertain(FIRST.deployment_instance_id, at=later_time) == 1
    assert restarted.pending(FIRST, at=later_time) == []
    assert restarted.begin_delivery(later.delivery_id, authority=FIRST, at=later_time) is False


def test_entry_cannot_pass_an_inflight_handoff_even_before_recovery(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    first = directive(FIRST)
    inbox.accept(first, received_at=NOW)
    assert inbox.begin_delivery(first.delivery_id, authority=FIRST, at=NOW)
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=BAR + timedelta(minutes=30), received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="unresolved"):
        inbox.accept(later, received_at=later_time)
    assert inbox.mark_handoff(first.delivery_id, authority=FIRST, at=NOW)
    assert inbox.accept(later, received_at=later_time).status == "accepted"


def test_wrong_generation_and_expired_events_never_dispatch(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    first = directive(FIRST)
    inbox.accept(first, received_at=NOW)
    newer = EngineLifecycleAuthority(
        deployment_instance_id=FIRST.deployment_instance_id,
        deployment_spec_id=FIRST.deployment_spec_id,
        deployment_spec_digest=FIRST.deployment_spec_digest,
        generation=2,
        trading_mode="testnet",
    )
    assert inbox.pending(newer, at=NOW) == []
    assert inbox.begin_delivery(first.delivery_id, authority=newer, at=NOW) is False
    assert inbox.pending(FIRST, at=NOW + timedelta(seconds=61)) == []
    assert (
        inbox.begin_delivery(first.delivery_id, authority=FIRST, at=NOW + timedelta(seconds=61))
        is False
    )


def test_fabricated_target_identity_and_risk_shape_are_rejected(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    first = directive(FIRST)
    with pytest.raises(ExternalTargetInboxError, match="identity"):
        inbox.accept(replace(first, delivery_id="forged"), received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="target"):
        inbox.accept(replace(first, target="UNKNOWN"), received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="risk"):
        inbox.accept(replace(first, risk_action="FLATTEN"), received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="signal"):
        inbox.accept(replace(first, signal=None), received_at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="amount"):
        inbox.accept(
            replace(first, signal=Signal.enter_short(100, pair="ETH-USDT", amount=100)),
            received_at=NOW,
        )
    flat = directive(FIRST, target="FLAT")
    with pytest.raises(ExternalTargetInboxError, match="signal"):
        inbox.accept(replace(flat, signal=first.signal), received_at=NOW)

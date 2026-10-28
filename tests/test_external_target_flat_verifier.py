"""A flat handoff remains fenced until fresh, scoped venue evidence arrives."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from custos.engines.nautilus.external_target_flat_verifier import (
    ExternalTargetFlatVerifier,
    VenueFlatObservation,
)
from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from tests.test_external_target_dispatcher import host
from tests.test_external_target_inbox import BAR, FIRST, NOW, directive


class Reader:
    def __init__(self, observation: VenueFlatObservation) -> None:
        self.observation = observation
        self.calls = []

    async def observe_flat(self, *, connector: str, pair: str) -> VenueFlatObservation:
        self.calls.append((connector, pair))
        return self.observation


def observation(**changes) -> VenueFlatObservation:
    return replace(
        VenueFlatObservation(
            source="venue_api",
            connector="okx_perpetual",
            pair="ETH-USDT",
            observed_at=NOW + timedelta(seconds=1),
            positions_complete=True,
            orders_complete=True,
            open_positions=0,
            open_orders=0,
        ),
        **changes,
    )


def requested_inbox(tmp_path: Path):
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(risk, received_at=NOW)
    assert inbox.begin_delivery(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=FIRST, at=NOW)
    return inbox, risk


@pytest.mark.asyncio
async def test_fresh_venue_flat_observation_releases_only_its_target(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    reader = Reader(observation())
    verifier = ExternalTargetFlatVerifier(
        inbox, host(), reader, clock=lambda: NOW + timedelta(seconds=2)
    )
    assert await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert reader.calls == [("okx_perpetual", "ETH-USDT")]
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    later = directive(
        FIRST,
        target="LONG",
        bar=BAR + timedelta(minutes=30),
        received_at=NOW + timedelta(minutes=30),
    )
    assert inbox.accept(later, received_at=NOW + timedelta(minutes=30)).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"source": "nautilus_cache"},
        {"connector": "binance_perpetual"},
        {"pair": "BTC-USDT"},
        {"positions_complete": False},
        {"orders_complete": False},
        {"open_positions": 1},
        {"open_orders": 1},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(seconds=31)},
    ],
)
async def test_incomplete_or_nonflat_observation_keeps_fence(tmp_path: Path, changes) -> None:
    inbox, risk = requested_inbox(tmp_path)
    verifier = ExternalTargetFlatVerifier(
        inbox, host(), Reader(observation(**changes)), clock=lambda: NOW + timedelta(seconds=2)
    )
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    later = directive(
        FIRST,
        target="LONG",
        bar=BAR + timedelta(minutes=30),
        received_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(ExternalTargetInboxError, match="flat confirmation"):
        inbox.accept(later, received_at=NOW + timedelta(minutes=30))


@pytest.mark.asyncio
async def test_stale_authority_cannot_query_or_release_flat(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    reader = Reader(observation())
    verifier = ExternalTargetFlatVerifier(
        inbox,
        host(replace(FIRST, generation=2)),
        reader,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    with pytest.raises(RuntimeError, match="authority"):
        await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert reader.calls == []


@pytest.mark.asyncio
async def test_clock_is_read_after_venue_observation(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    sequence = []

    class SequencedReader(Reader):
        async def observe_flat(self, *, connector: str, pair: str) -> VenueFlatObservation:
            sequence.append("venue")
            return await super().observe_flat(connector=connector, pair=pair)

    def clock():
        sequence.append("clock")
        return NOW + timedelta(seconds=2)

    verifier = ExternalTargetFlatVerifier(
        inbox, host(), SequencedReader(observation()), clock=clock
    )
    assert await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert sequence == ["venue", "clock"]


@pytest.mark.asyncio
async def test_local_order_or_position_vetoes_venue_flat(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    runtime = host()
    runtime.flat_local_clear = False
    reader = Reader(observation())
    verifier = ExternalTargetFlatVerifier(
        inbox, runtime, reader, clock=lambda: NOW + timedelta(seconds=2)
    )
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert reader.calls == []
    assert runtime.flat_local_checks == [(FIRST, "ETH-USDT")]
    assert inbox.pending_flat_request(risk.delivery_id, authority=FIRST) is not None


@pytest.mark.asyncio
async def test_local_state_is_rechecked_after_venue_read(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    runtime = host()

    class MovingReader(Reader):
        async def observe_flat(self, *, connector: str, pair: str) -> VenueFlatObservation:
            runtime.flat_local_clear = False
            return await super().observe_flat(connector=connector, pair=pair)

    verifier = ExternalTargetFlatVerifier(
        inbox, runtime, MovingReader(observation()), clock=lambda: NOW + timedelta(seconds=2)
    )
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert runtime.flat_local_checks == [(FIRST, "ETH-USDT"), (FIRST, "ETH-USDT")]
    assert inbox.pending_flat_request(risk.delivery_id, authority=FIRST) is not None


@pytest.mark.asyncio
async def test_sandbox_can_release_flat_only_from_guarded_local_engine(tmp_path: Path) -> None:
    sandbox = replace(FIRST, trading_mode="sandbox")
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    risk = directive(sandbox, target="FLAT", risk_action="FLATTEN")
    inbox.accept(risk, received_at=NOW)
    assert inbox.begin_delivery(risk.delivery_id, authority=sandbox, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=sandbox, at=NOW)
    runtime = host(sandbox)
    runtime.scope = replace(runtime.scope, connector="binance_perpetual")
    verifier = ExternalTargetFlatVerifier(
        inbox, runtime, None, clock=lambda: NOW + timedelta(seconds=2)
    )
    runtime.flat_local_clear = False
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=sandbox)
    runtime.flat_local_clear = True
    assert await verifier.confirm_if_flat(risk.delivery_id, authority=sandbox)
    assert runtime.flat_local_checks == [
        (sandbox, "ETH-USDT"),
        (sandbox, "ETH-USDT"),
        (sandbox, "ETH-USDT"),
    ]


@pytest.mark.asyncio
async def test_testnet_without_independent_reader_never_releases_flat(tmp_path: Path) -> None:
    inbox, risk = requested_inbox(tmp_path)
    verifier = ExternalTargetFlatVerifier(
        inbox, host(), None, clock=lambda: NOW + timedelta(seconds=2)
    )
    assert not await verifier.confirm_if_flat(risk.delivery_id, authority=FIRST)
    assert inbox.pending_flat_request(risk.delivery_id, authority=FIRST) is not None

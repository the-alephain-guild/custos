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
    verifier = ExternalTargetFlatVerifier(inbox, host(), reader)
    assert await verifier.confirm_if_flat(
        risk.delivery_id, authority=FIRST, at=NOW + timedelta(seconds=2)
    )
    assert reader.calls == [("okx_perpetual", "ETH-USDT")]
    assert not await verifier.confirm_if_flat(
        risk.delivery_id, authority=FIRST, at=NOW + timedelta(seconds=2)
    )
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
    verifier = ExternalTargetFlatVerifier(inbox, host(), Reader(observation(**changes)))
    assert not await verifier.confirm_if_flat(
        risk.delivery_id, authority=FIRST, at=NOW + timedelta(seconds=2)
    )
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
    verifier = ExternalTargetFlatVerifier(inbox, host(replace(FIRST, generation=2)), reader)
    with pytest.raises(RuntimeError, match="authority"):
        await verifier.confirm_if_flat(
            risk.delivery_id, authority=FIRST, at=NOW + timedelta(seconds=2)
        )
    assert reader.calls == []

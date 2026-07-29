"""Live is refused at the offline lane's boundary, twice and early.

Twice, because a spec claiming sandbox and a transport carrying testnet are two
independent claims and either can be the wrong one. Early, because a refusal that
arrives after the spec is parsed, a connection is opened or a file is written is a
refusal that already let something happen.
"""

from __future__ import annotations

import pytest

from custos.contracts import TradingMode
from custos.offline.mode_guard import (
    PERMITTED_MODES,
    OfflineModeRefused,
    admit_offline_spec,
    refuse_live,
)

SANDBOX_SPEC = b'{"trading_mode": "sandbox", "spec_id": "s", "generation": 1}'
TESTNET_SPEC = b'{"trading_mode": "testnet", "spec_id": "s", "generation": 1}'
LIVE_SPEC = b'{"trading_mode": "live", "spec_id": "s", "generation": 1}'


def test_permitted_modes_are_the_canonical_vocabulary_without_live() -> None:
    assert PERMITTED_MODES == frozenset({TradingMode.SANDBOX, TradingMode.TESTNET})
    assert TradingMode.LIVE not in PERMITTED_MODES


@pytest.mark.parametrize("mode", ["sandbox", "testnet"])
def test_admits_the_non_live_modes(mode: str) -> None:
    assert refuse_live(mode, source="command line") == TradingMode(mode)


def test_refuses_live_declared_in_the_spec() -> None:
    with pytest.raises(OfflineModeRefused, match="live"):
        admit_offline_spec(LIVE_SPEC, command_mode="live")


def test_refuses_live_passed_on_the_command_line_alone() -> None:
    """The spec claiming sandbox does not make a live transport acceptable."""

    with pytest.raises(OfflineModeRefused, match="live"):
        admit_offline_spec(SANDBOX_SPEC, command_mode="live")


def test_refuses_live_declared_in_the_spec_alone() -> None:
    with pytest.raises(OfflineModeRefused, match="live"):
        admit_offline_spec(LIVE_SPEC, command_mode="sandbox")


def test_refuses_a_spec_that_disagrees_with_the_transport() -> None:
    with pytest.raises(OfflineModeRefused, match="disagree"):
        admit_offline_spec(SANDBOX_SPEC, command_mode="testnet")


@pytest.mark.parametrize(
    ("raw", "mode"),
    [(SANDBOX_SPEC, "sandbox"), (TESTNET_SPEC, "testnet")],
)
def test_admits_an_agreeing_non_live_spec(raw: bytes, mode: str) -> None:
    assert admit_offline_spec(raw, command_mode=mode) == TradingMode(mode)


def test_refuses_live_before_the_rest_of_the_spec_is_looked_at() -> None:
    """A live spec is refused on mode, not on whatever else is wrong with it."""

    unusable = b'{"trading_mode": "live", "generation": "not-a-number", "pairs": 7}'

    with pytest.raises(OfflineModeRefused, match="live"):
        admit_offline_spec(unusable, command_mode="sandbox")


def test_refuses_a_spec_carrying_no_mode() -> None:
    with pytest.raises(OfflineModeRefused, match="declare"):
        admit_offline_spec(b'{"spec_id": "s"}', command_mode="sandbox")


def test_refuses_an_unreadable_spec_rather_than_assuming_a_mode() -> None:
    with pytest.raises(OfflineModeRefused):
        admit_offline_spec(b"not json at all", command_mode="sandbox")


@pytest.mark.parametrize("mode", ["", "  ", "LIVE", "prod", "paper", "sim"])
def test_refuses_anything_outside_the_permitted_vocabulary(mode: str) -> None:
    with pytest.raises(OfflineModeRefused):
        refuse_live(mode, source="command line")


def test_refusal_names_where_the_rejected_mode_came_from() -> None:
    with pytest.raises(OfflineModeRefused, match="command line"):
        refuse_live("live", source="command line")
    with pytest.raises(OfflineModeRefused, match="deployment spec"):
        refuse_live("live", source="deployment spec")

"""The level ledger, testable on its own for the first time.

These invariants lived inside a 700-line monitor as four collections indexed by the
same number. Reaching them meant driving a whole position through tick callbacks,
so several were never asserted at all -- the out-of-range guard and the final
level's remainder among them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from custos_toolkit_nautilus.adapter.scaled_exit_ladder import (
    ScaledExitLadder,
    TakeProfitLevelState,
)


def _ladder(*shares: str, base: str | None = None) -> ScaledExitLadder:
    ladder = ScaledExitLadder.from_specs(
        [
            {"target_pct": Decimal(str(index + 1)) / 100, "exit_pct": Decimal(share)}
            for index, share in enumerate(shares)
        ]
    )
    if base is not None:
        ladder.base = Decimal(base)
    return ladder


@pytest.mark.parametrize("level", [0, -1, -2, 3, 99])
def test_a_number_that_is_not_a_level_is_not_a_level(level: int) -> None:
    """Levels are 1-based. Python indexing would answer 0 and -1 with a real level.

    Letting -1 through is the silent kind of wrong: it returns the *last* level, so
    a caller asking about a level that does not exist quietly operates on another one.
    """
    ladder = _ladder("0.5", "0.5")

    assert ladder.at(level) is None


def test_the_levels_that_do_exist_are_the_ones_you_asked_for() -> None:
    """The negative above only means something if the positives resolve."""
    ladder = _ladder("0.3", "0.7")

    assert ladder.at(1) is not None
    assert ladder.at(1).exit_pct == Decimal("0.3")
    assert ladder.at(2).exit_pct == Decimal("0.7")


def test_the_final_level_is_owed_whatever_the_plan_still_lacks() -> None:
    """Rounding the earlier levels lost is recovered at the end, not stranded.

    Three shares of a position of 1 plan to sell all of it. If the venue rounds the
    first two fills down, the last level owes the difference -- reading it as its own
    share alone would leave 0.06 of the position permanently unsold.
    """
    ladder = _ladder("0.33", "0.33", "0.34", base="1")
    ladder.levels[0].filled = Decimal("0.3")
    ladder.levels[1].filled = Decimal("0.3")

    assert ladder.outstanding(3) == Decimal("0.4")
    # Its own share would be only this much.
    assert Decimal("1") * ladder.levels[2].exit_pct == Decimal("0.34")


def test_a_non_final_level_is_owed_only_its_own_share() -> None:
    ladder = _ladder("0.25", "0.75", base="4")
    ladder.levels[0].filled = Decimal("0.4")

    assert ladder.outstanding(1) == Decimal("0.6")


def test_without_a_base_a_level_owes_only_what_was_actually_sent() -> None:
    """No base means no plan; the only target on record is the dispatch."""
    ladder = _ladder("0.5", "0.5")
    ladder.levels[0].dispatched = Decimal("2")
    ladder.levels[0].filled = Decimal("0.5")

    assert ladder.outstanding(1) == Decimal("1.5")


def test_a_part_filled_level_stays_pending_until_nothing_is_outstanding() -> None:
    ladder = _ladder("1.0", base="2")
    ladder.claim_next_reached(Decimal("0.02"))
    ladder.bind_order(1, "O-1")

    assert ladder.confirm_order("O-1", Decimal("1.2")) is True
    assert ladder.at(1).state is TakeProfitLevelState.PENDING
    assert ladder.carries_order("O-1")

    assert ladder.confirm_order("O-1", Decimal("0.8")) is True
    assert ladder.at(1).state is TakeProfitLevelState.COMPLETED
    assert not ladder.carries_order("O-1")


def test_an_order_that_ends_owing_something_returns_its_level_to_the_market() -> None:
    ladder = _ladder("1.0", base="2")
    ladder.claim_next_reached(Decimal("0.02"))
    ladder.bind_order(1, "O-1")
    ladder.record_dispatch(1, Decimal("2"))

    assert ladder.release_order("O-1") is True
    assert ladder.at(1).state is TakeProfitLevelState.ARMED
    assert ladder.at(1).dispatched == Decimal("0")


def test_reset_clears_the_whole_account_not_just_the_states() -> None:
    """Four collections used to be cleared separately; forgetting one was silent."""
    ladder = _ladder("0.5", "0.5", base="2")
    ladder.claim_next_reached(Decimal("0.02"))
    ladder.bind_order(1, "O-1")
    ladder.record_dispatch(1, Decimal("1"))
    ladder.levels[0].filled = Decimal("0.4")

    ladder.reset()

    assert [one.state for one in ladder.levels] == [TakeProfitLevelState.ARMED] * 2
    assert [one.dispatched for one in ladder.levels] == [Decimal("0")] * 2
    assert [one.filled for one in ladder.levels] == [Decimal("0")] * 2
    assert not ladder.carries_order("O-1")
    assert ladder.base is None


def test_claiming_takes_the_lowest_armed_level_that_price_has_reached() -> None:
    ladder = _ladder("0.5", "0.5")

    first = ladder.claim_next_reached(Decimal("0.05"))
    second = ladder.claim_next_reached(Decimal("0.05"))
    third = ladder.claim_next_reached(Decimal("0.05"))

    assert first is not None and first[0] == 1
    assert second is not None and second[0] == 2
    assert third is None, "no level may be offered twice while it is pending"


def test_extending_the_base_adds_rather_than_reseeds() -> None:
    """An entry filling in lots must not discard levels already taken."""
    ladder = _ladder("0.5", "0.5")
    ladder.extend_base(Decimal("1"))
    ladder.extend_base(Decimal("0.5"))
    ladder.extend_base(Decimal("0"))
    ladder.extend_base(Decimal("-3"))

    assert ladder.base == Decimal("1.5")

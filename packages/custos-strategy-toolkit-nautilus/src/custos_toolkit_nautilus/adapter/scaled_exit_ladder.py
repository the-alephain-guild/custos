"""The ledger behind scaled take-profit levels.

A level is one row of bookkeeping: what it targets, how much of the position it is
a share of, whether it is spent, how much was sent for it and how much came back
filled. That used to be four collections indexed by the same number plus a fifth
mapping order ids onto it, so every caller repeated ``index = level - 1`` and its
own bounds check -- and two of those checks measured a different list than the other
two. They happen to be the same length; nothing made them stay that way.

Levels are 1-based to the outside world, matching how they are configured and
logged. The conversion happens here, once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TypedDict

_ZERO = Decimal("0")


class TakeProfitLevelSpec(TypedDict):
    """One configured level: where it triggers, and what share it takes."""

    target_pct: Decimal
    exit_pct: Decimal


class TakeProfitLevelState(str, Enum):  # noqa: UP042 - match the str(Enum) style of SLTPMode
    """Where one scaled take-profit level stands.

    Reaching the target price is an intent, not an outcome. A level is only spent
    once the venue reports the fill that took the quantity, so a dispatch that was
    refused, rejected or never sent returns the level to the market.
    """

    ARMED = "armed"
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass
class ScaledExitLevel:
    """One level's target and its running account of quantity."""

    target_pct: Decimal
    exit_pct: Decimal
    state: TakeProfitLevelState = TakeProfitLevelState.ARMED
    dispatched: Decimal = _ZERO
    filled: Decimal = _ZERO

    def rearm(self) -> None:
        self.state = TakeProfitLevelState.ARMED
        self.dispatched = _ZERO


@dataclass
class ScaledExitLadder:
    """Every configured level plus which order is carrying which.

    ``base`` is the position size the exit percentages are shares of. Exchange mode
    prices every level off the size at submission; tick mode has to use one base too,
    or the same configuration exits a different total depending on fill timing.
    """

    levels: list[ScaledExitLevel] = field(default_factory=list)
    base: Decimal | None = None
    _orders: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_specs(cls, specs: list[TakeProfitLevelSpec] | None) -> ScaledExitLadder:
        return cls(
            levels=[
                ScaledExitLevel(
                    target_pct=spec.get("target_pct", _ZERO),
                    exit_pct=spec.get("exit_pct", _ZERO),
                )
                for spec in (specs or [])
            ]
        )

    def __len__(self) -> int:
        return len(self.levels)

    def at(self, level: int) -> ScaledExitLevel | None:
        """The 1-based level, or ``None`` when there is no such level.

        One lookup, one bounds check. Callers used to write their own, against two
        different lists.
        """
        index = level - 1
        if 0 <= index < len(self.levels):
            return self.levels[index]
        return None

    def is_final(self, level: int) -> bool:
        return level == len(self.levels)

    def reset(self) -> None:
        """Forget every level's account, keeping the configuration.

        Resetting four collections separately made forgetting one a silent error.
        """
        for entry in self.levels:
            entry.state = TakeProfitLevelState.ARMED
            entry.dispatched = _ZERO
            entry.filled = _ZERO
        self._orders.clear()
        self.base = None

    def extend_base(self, additional: Decimal) -> None:
        """Add newly opened exposure to the base the shares are taken from.

        An entry can fill in several lots. The ladder is seeded on the first lot that
        opens exposure, so without this the levels would size against that lot alone
        and leave the rest of the position unsold. Re-seeding would be wrong: it
        discards levels already taken while the entry was still filling.
        """
        if additional <= 0:
            return
        self.base = additional if self.base is None else self.base + additional

    def outstanding(self, level: int) -> Decimal:
        """How much of this 1-based level's target has still not been sold.

        The final level is owed whatever is left of the planned total, so the
        rounding the earlier levels lost is recovered there rather than stranded.
        """
        entry = self.at(level)
        if entry is None:
            return _ZERO
        base = self.base
        if base is None or base <= 0:
            # No base: the only target on record is what was actually dispatched.
            return max(entry.dispatched - entry.filled, _ZERO)
        if self.is_final(level):
            planned_total = base * sum((one.exit_pct for one in self.levels), _ZERO)
            filled_total = sum((one.filled for one in self.levels), _ZERO)
            return max(planned_total - filled_total, _ZERO)
        return max(base * entry.exit_pct - entry.filled, _ZERO)

    def planned_exit_quantity(self, level: int, remaining: Decimal) -> Decimal:
        """The quantity this 1-based level should take now.

        Levels are shares of the base recorded when the position opened, so the total
        taken does not depend on how much earlier levels already sold, and a retry
        after a part fill asks only for the part still owed.
        """
        entry = self.at(level)
        if entry is None:
            return _ZERO
        if self.base is None or self.base <= 0:
            outstanding = self.outstanding(level)
            return outstanding if outstanding > 0 else remaining * entry.exit_pct
        return self.outstanding(level)

    def record_dispatch(self, level: int, quantity: Decimal) -> None:
        entry = self.at(level)
        if entry is not None:
            entry.dispatched = quantity

    def bind_order(self, level: int, order_id: object) -> None:
        """Record which order carries a 1-based level's quantity."""
        if self.at(level) is not None:
            self._orders[str(order_id)] = level - 1

    def carries_order(self, order_id: object) -> bool:
        """Whether a level is still waiting on this order's execution report."""
        return str(order_id) in self._orders

    def confirm_order(self, order_id: object, filled_quantity: Decimal) -> bool:
        """Credit a fill to the level its order carries. True when one matched.

        An IOC lot can come back part filled, and the part that did not fill was not
        sold: the level closes only once nothing is outstanding on it.
        """
        key = str(order_id)
        index = self._orders.get(key)
        if index is None:
            return False
        entry = self.levels[index]
        entry.filled += filled_quantity
        if self.outstanding(index + 1) <= 0:
            entry.state = TakeProfitLevelState.COMPLETED
            self._orders.pop(key, None)
        return True

    def release_order(self, order_id: object) -> bool:
        """The order is done at the venue: settle its level by what it still owes."""
        index = self._orders.pop(str(order_id), None)
        if index is None:
            return False
        entry = self.levels[index]
        if entry.state is TakeProfitLevelState.PENDING:
            if self.outstanding(index + 1) > 0:
                entry.rearm()
            else:
                entry.state = TakeProfitLevelState.COMPLETED
                entry.dispatched = _ZERO
        return True

    def release_level(self, level: int) -> None:
        """Return a 1-based level that was triggered but never dispatched."""
        entry = self.at(level)
        if entry is not None and entry.state is TakeProfitLevelState.PENDING:
            entry.rearm()

    def claim_next_reached(self, pnl_pct: Decimal) -> tuple[int, ScaledExitLevel] | None:
        """The lowest armed level this profit has reached, marked pending.

        Marking is the point: the caller is about to try to sell it, and a second
        tick must not offer the same level again. Anything that does not end in a
        fill hands it back through :meth:`release_level` or :meth:`release_order`.
        """
        for index, entry in enumerate(self.levels):
            if entry.state is not TakeProfitLevelState.ARMED:
                continue
            if pnl_pct >= entry.target_pct:
                entry.state = TakeProfitLevelState.PENDING
                return index + 1, entry
        return None


__all__ = [
    "ScaledExitLadder",
    "ScaledExitLevel",
    "TakeProfitLevelSpec",
    "TakeProfitLevelState",
]

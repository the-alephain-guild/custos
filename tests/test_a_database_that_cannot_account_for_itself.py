"""A database that predates position lots must say so before it trades, not after.

The lot table is created with ``CREATE TABLE IF NOT EXISTS``, and that runs
*before* the block of legacy-shape checks. A database that never had the table
gets a fresh empty one, and the `side`-column check then passes on the table it
just created. The one piece of evidence -- that the table was absent -- is
erased before anybody looks for it.

The neighbouring checks work only because their tables existed in the
predecessor, so `IF NOT EXISTS` was a no-op and the old columns survived.

The cost of the gap is paid later: the database opens, runs, and only fails when
a real reduce-only fill arrives carrying a position id. By then the fill has
already happened at the venue, and an accounting failure freezes the boundary
rather than rejecting an order.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from custos.core.runner_fact import (
    RunnerFactOutbox,
    RunnerStateMigrationError,
)
from tests.test_order_reservation import INSTANCE_A, POLICY_ID, _store


def _as_if_the_lot_table_had_never_existed(path: Path) -> None:
    """Reproduce the pre-lot shape: the table itself is simply not there.

    Dropping it is the only way to get back to that shape, because the current
    schema creates it unconditionally. What the old source left behind is a
    database with reservations, filled quantities, and no attribution at all.
    """
    outbox = RunnerFactOutbox.__new__(RunnerFactOutbox)
    object.__setattr__(outbox, "path", path)
    with outbox._connect() as connection:
        connection.execute("DROP TABLE runner_position_exposure_lot")


def test_an_old_database_holding_an_open_position_refuses_to_open(tmp_path: Path) -> None:
    """Refuse at startup, with the recovery path named.

    The alternative the report offers -- recovering attribution from trusted
    execution evidence -- is not available: attributing a filled quantity needs
    the position id, and that is exactly what the old database never stored.
    Inventing one would be worse than refusing.
    """
    path = tmp_path / "legacy-with-exposure.sqlite3"
    store = _store(path)
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("50"),
    )
    store.record_order_fill_sync(
        event_id="fill",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("50"),
        fill_quantity=Decimal("1"),
    )
    _as_if_the_lot_table_had_never_existed(path)

    with pytest.raises(RunnerStateMigrationError) as raised:
        _store(path)

    message = str(raised.value)
    assert "position lot" in message
    assert "recreate" in message, "the operator must be told what to do, not just what is wrong"


def test_an_old_database_with_nothing_open_still_opens(tmp_path: Path) -> None:
    """Nothing to attribute means nothing to refuse.

    Rejecting a quiet legacy database would cost an operator a rebuild for no
    reason -- the guard exists to protect open exposure, and there is none.
    """
    path = tmp_path / "legacy-quiet.sqlite3"
    store = _store(path)
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("50"),
    )
    store.release_order_reservation_sync(
        event_id="release",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        reason="canceled",
    )
    _as_if_the_lot_table_had_never_existed(path)

    reopened = _store(path)
    assert reopened.load_order_reservation_sync(INSTANCE_A, "entry").filled_quantity == Decimal(
        "0"
    ), "the released reservation holds nothing that would need attributing"


def test_a_current_database_whose_fill_carried_no_position_still_opens(tmp_path: Path) -> None:
    """The obvious predicate would have been wrong, and this is what guards that.

    "A filled reservation with no lot row" is a perfectly legal state today: a
    lot is written only when the fill carries a position id, and reductions for
    the rest are settled through the opening order instead. Using that as the
    guard would refuse healthy databases, so the guard keys on the table's prior
    absence rather than on the data alone.
    """
    path = tmp_path / "current-no-position-id.sqlite3"
    store = _store(path)
    store.reserve_order_notional_sync(
        event_id="reserve",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        policy_id=POLICY_ID,
        requested_notional=Decimal("50"),
    )
    store.record_order_fill_sync(
        event_id="fill",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("50"),
        fill_quantity=Decimal("1"),
    )

    reopened = _store(path)
    assert reopened.load_order_reservation_sync(INSTANCE_A, "entry").filled_exposure == Decimal(
        "50"
    )

"""OrderTracker's in-flight and cooldown guard around closing orders.

Integer and boolean timing logic, guarding against a reduce-only close order flood.
orders.py imports the engine at module level, so the module skips when it is absent.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.orders import OrderTracker, is_stale_order  # noqa: E402
from nautilus_trader.model import OrderSide, OrderType  # noqa: E402

S = 1_000_000_000  # one second in nanoseconds


class TestCloseGuard:
    def test_default_can_submit_close(self):
        t = OrderTracker()
        assert t.can_submit_close(0) is True
        assert t.can_submit_close(123 * S) is True

    def test_mark_closing_blocks_until_timeout(self):
        t = OrderTracker()
        now = 100 * S
        t.mark_closing(now, timeout_ns=5 * S)
        # Nothing more may be sent while one is in flight
        assert t.can_submit_close(now) is False
        assert t.can_submit_close(now + 4 * S) is False
        # After the timeout a retry is allowed, so a lost order cannot wedge this forever
        assert t.can_submit_close(now + 5 * S) is True
        assert t.can_submit_close(now + 6 * S) is True

    def test_reject_cooldown_blocks_then_allows(self):
        t = OrderTracker()
        now = 200 * S
        t.set_close_cooldown(now, cooldown_ns=2 * S)
        assert t.can_submit_close(now) is False
        assert t.can_submit_close(now + 1 * S) is False
        assert t.can_submit_close(now + 2 * S) is True

    def test_clear_closing_resets_block(self):
        t = OrderTracker()
        now = 300 * S
        t.mark_closing(now, timeout_ns=5 * S)
        assert t.can_submit_close(now) is False
        t.clear_closing()
        assert t.can_submit_close(now) is True

    def test_clear_also_resets_closing_deadline(self):
        """clear() must reset the close guard too, so nothing lingers once the position is gone."""
        t = OrderTracker()
        now = 400 * S
        t.mark_closing(now, timeout_ns=5 * S)
        t.clear()
        assert t.can_submit_close(now) is True
        assert t.has_pending_orders is False

    def test_cooldown_does_not_duplicate_with_inflight(self):
        """A rejection sets a cooldown, which overrides the in-flight deadline."""
        t = OrderTracker()
        now = 500 * S
        t.mark_closing(now, timeout_ns=5 * S)  # deadline = now+5s
        # Rejected after 1s, so a 2s cooldown runs from the moment of rejection
        t.set_close_cooldown(now + 1 * S, cooldown_ns=2 * S)  # deadline = now+3s
        assert t.can_submit_close(now + 2 * S) is False
        assert t.can_submit_close(now + 3 * S) is True


class TestCloseRejectCount:
    """The consecutive close-rejection counter that feeds the halt threshold.

    It must survive clear() and clear_closing(), because those mean \"sweep orphans after a
    rejection or reversal\" rather than \"the position is flat\". Resetting there would zero
    the count on the very rejection path that calls clear(), so halt would never fire.
    """

    def test_record_close_reject_increments(self):
        t = OrderTracker()
        assert t.close_reject_count == 0
        t.record_close_reject()
        t.record_close_reject()
        t.record_close_reject()
        assert t.close_reject_count == 3

    def test_reset_close_rejects_zeroes_count(self):
        t = OrderTracker()
        t.record_close_reject()
        t.record_close_reject()
        t.reset_close_rejects()
        assert t.close_reject_count == 0

    def test_clear_does_not_reset_close_reject_count(self):
        """clear() drops the tracked order ids as an orphan sweep, and leaves the count alone:
        resetting it there would zero the halt count on the rejection path that calls it."""
        t = OrderTracker()
        t.record_close_reject()
        t.record_close_reject()
        t.clear()
        assert t.close_reject_count == 2

    def test_clear_closing_does_not_reset_close_reject_count(self):
        """clear_closing() clears only the in-flight and cooldown gates, never the count."""
        t = OrderTracker()
        t.record_close_reject()
        t.clear_closing()
        assert t.close_reject_count == 1


class TestPartialFillProtectionAccounting:
    def test_each_normal_entry_fill_becomes_a_new_protection_lot(self):
        tracker = OrderTracker()
        tracker.set_entry_order("entry-1", side=1)

        first_delta, first_exposure = tracker.record_entry_fill(Decimal("0.0031"))
        second_delta, second_exposure = tracker.record_entry_fill(Decimal("0.0039"))

        assert (first_delta, first_exposure) == (Decimal("0.0031"), True)
        assert (second_delta, second_exposure) == (Decimal("0.0039"), False)
        assert tracker.entry_filled_quantity == Decimal("0.0070")
        assert tracker.entry_protected_quantity == Decimal("0.0070")

    def test_reversal_close_quantity_is_not_mislabeled_as_new_exposure(self):
        tracker = OrderTracker()
        tracker.set_entry_order(
            "reverse-entry",
            side=-1,
            exposure_offset_quantity=Decimal("0.0070"),
        )

        closing_delta, closing_first = tracker.record_entry_fill(Decimal("0.0030"))
        crossing_delta, crossing_first = tracker.record_entry_fill(Decimal("0.0050"))
        open_delta, open_first = tracker.record_entry_fill(Decimal("0.0060"))

        assert (closing_delta, closing_first) == (Decimal("0"), False)
        assert (crossing_delta, crossing_first) == (Decimal("0.0010"), True)
        assert (open_delta, open_first) == (Decimal("0.0060"), False)
        assert tracker.entry_filled_quantity == Decimal("0.0140")
        assert tracker.entry_protected_quantity == Decimal("0.0070")

    def test_all_partial_fill_protection_orders_remain_owned_until_removed(self):
        tracker = OrderTracker()
        tracker.add_exchange_sl_order("stop-lot-1", Decimal("0.0031"))
        tracker.add_exchange_sl_order("stop-lot-2", Decimal("0.0039"))

        assert tracker.exchange_sl_order_ids == ["stop-lot-1", "stop-lot-2"]
        assert tracker.exchange_sl_order_id == "stop-lot-2"
        assert tracker.protected_quantity(exchange_managed=True) == Decimal("0.0070")
        assert tracker.has_pending_orders is True

        tracker.remove_order("stop-lot-1")

        assert tracker.exchange_sl_order_ids == ["stop-lot-2"]
        assert tracker.protected_quantity(exchange_managed=True) == Decimal("0.0039")


class TestStaleProtectiveOrderOwnership:
    def test_untracked_protective_stop_is_preserved_without_owner_evidence(self):
        order = SimpleNamespace(
            client_order_id="other-instance-stop",
            side=OrderSide.SELL,
            order_type=OrderType.STOP_MARKET,
            is_reduce_only=True,
            ts_init=0,
        )

        assert (
            is_stale_order(
                order,
                position_is_long=True,
                tracked_ids={"owned-stop"},
                sl_is_tracked=True,
                now_ns=60 * S,
            )
            is False
        )

    def test_untracked_wrong_side_stop_is_still_removed(self):
        order = SimpleNamespace(
            client_order_id="old-long-stop",
            side=OrderSide.SELL,
            order_type=OrderType.STOP_MARKET,
            is_reduce_only=True,
            ts_init=0,
        )

        assert (
            is_stale_order(
                order,
                position_is_long=False,
                tracked_ids=set(),
                sl_is_tracked=False,
                now_ns=60 * S,
            )
            is True
        )

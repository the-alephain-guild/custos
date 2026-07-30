"""OrderTracker's in-flight and cooldown guard around closing orders.

Integer and boolean timing logic, guarding against a reduce-only close order flood.
orders.py imports the engine at module level, so the module skips when it is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.orders import OrderTracker  # noqa: E402

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

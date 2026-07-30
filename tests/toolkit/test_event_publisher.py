"""Tests for EventPublisher + ExecutionManager tags passthrough.

Covers:
- EventPublisher config switch (enabled/disabled)
- Signal, order, position event publishing via msgbus
- JSON serialization: the payload is bytes, encoded once rather than twice
- Exception safety (publish failure -> log warning, no raise)
- H3 no-publish failure semantics (msgbus None → observable dropped_events)
- signal_id helpers (generate, make_tag, extract)
- ExecutionManager.create_entry_order tags passthrough (market + limit)
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit_nautilus.adapter.event_publisher import (  # noqa: E402
    EVENT_TOPIC,
    EventPublisher,
    extract_signal_id_from_tags,
    generate_signal_id,
    make_signal_tag,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_msgbus():
    bus = MagicMock()
    bus.publish = MagicMock()
    return bus


@pytest.fixture()
def mock_clock():
    clock = MagicMock()
    clock.timestamp_ns.return_value = 1700000000_000000000
    return clock


# ---------------------------------------------------------------------------
# Test 1: disabled publisher never calls msgbus
# ---------------------------------------------------------------------------


def test_publish_signal_disabled_does_not_call_msgbus(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="test-strat", clock=mock_clock, enabled=False
    )
    publisher.publish_signal(
        signal_id="abc",
        direction="ENTER_LONG",
        pair="BTC-USDT",
        price="50000",
        strength=0.9,
    )
    mock_msgbus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: enabled publisher calls msgbus.publish with correct topic + json string
# ---------------------------------------------------------------------------


def test_publish_signal_enabled_calls_msgbus(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="test-strat", clock=mock_clock, enabled=True
    )
    publisher.publish_signal(
        signal_id="abc-123",
        direction="ENTER_LONG",
        pair="BTC-USDT",
        price="50000",
        strength=0.9,
    )
    mock_msgbus.publish.assert_called_once()
    topic, payload = mock_msgbus.publish.call_args[0]
    assert topic == EVENT_TOPIC
    # The payload is bytes of raw UTF-8 JSON, not a str, so it is encoded once.
    assert isinstance(payload, bytes)
    assert json.loads(payload)["type"] == "signal"  # one-layer decode → dict


# ---------------------------------------------------------------------------
# Test 3: signal event JSON contains required fields
# ---------------------------------------------------------------------------


def test_publish_signal_json_contains_required_fields(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="test-strat", clock=mock_clock, enabled=True
    )
    publisher.publish_signal(
        signal_id="sig-001",
        direction="ENTER_SHORT",
        pair="ETH-USDT",
        price="3000.50",
        strength=0.75,
        metadata={"atr": 120},
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["type"] == "signal"
    assert payload["signal_id"] == "sig-001"
    assert payload["direction"] == "ENTER_SHORT"
    assert payload["pair"] == "ETH-USDT"
    assert payload["price"] == "3000.50"
    assert payload["strength"] == 0.75
    assert payload["metadata"] == {"atr": 120}
    assert payload["strategy_id"] == "test-strat"
    assert "event_id" in payload
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# Test 4: order event JSON contains required fields
# ---------------------------------------------------------------------------


def test_publish_order_json_contains_required_fields(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-x", clock=mock_clock, enabled=True
    )
    publisher.publish_order(
        order_id="O-001",
        signal_id="sig-001",
        venue_order_id="VO-001",
        instrument="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity="0.1",
        fill_price="50100",
        status="filled",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["type"] == "order"
    assert payload["order_id"] == "O-001"
    assert payload["signal_id"] == "sig-001"
    assert payload["status"] == "filled"
    assert payload["strategy_id"] == "strat-x"


# ---------------------------------------------------------------------------
# publish_order carries trigger_price and reduce_only; publish_signal carries SL/TP
# ---------------------------------------------------------------------------


def test_publish_order_includes_trigger_price_reduce_only(mock_msgbus, mock_clock):
    """A stop order's trigger_price and reduce_only reach the payload."""
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-x", clock=mock_clock, enabled=True
    )
    publisher.publish_order(
        order_id="O-STOP-1",
        signal_id="sig-001",
        venue_order_id=None,
        instrument="BTCUSDT.BINANCE",
        side="SELL",
        order_type="STOP_MARKET",
        quantity="0.1",
        fill_price=None,
        status="canceled",
        trigger_price="64704.90",
        reduce_only=True,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["trigger_price"] == "64704.90"
    assert payload["reduce_only"] is True


def test_publish_order_trigger_fields_default(mock_msgbus, mock_clock):
    """Omitting trigger_price and reduce_only defaults them to None and False."""
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-x", clock=mock_clock, enabled=True
    )
    publisher.publish_order(
        order_id="O-001",
        signal_id="sig-001",
        venue_order_id=None,
        instrument="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity="0.1",
        fill_price="50100",
        status="filled",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["trigger_price"] is None
    assert payload["reduce_only"] is False


def test_publish_signal_includes_stop_loss_take_profit(mock_msgbus, mock_clock):
    """A signal event carries stop_loss and take_profit."""
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="test-strat", clock=mock_clock, enabled=True
    )
    publisher.publish_signal(
        signal_id="sig-sltp",
        direction="ENTER_LONG",
        pair="BTC-USDT",
        price="65000",
        strength=0.9,
        stop_loss="63700.0",
        take_profit="67000.0",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["stop_loss"] == "63700.0"
    assert payload["take_profit"] == "67000.0"


def test_publish_signal_sl_tp_default_none(mock_msgbus, mock_clock):
    """Omitting the stop and take-profit defaults them to None."""
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="test-strat", clock=mock_clock, enabled=True
    )
    publisher.publish_signal(
        signal_id="sig-no-sltp",
        direction="ENTER_LONG",
        pair="BTC-USDT",
        price="65000",
        strength=0.9,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["stop_loss"] is None
    assert payload["take_profit"] is None


# ---------------------------------------------------------------------------
# The publisher's strategy_id must be the slug from the STRATEGY_ID environment variable,
# not the engine's internal id such as "SuperTrendStrategy-000". The wrong id makes the
# consumer's upsert hit a foreign key and kills the whole persistence path silently.
# ---------------------------------------------------------------------------


def test_resolve_event_strategy_id_prefers_env(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import resolve_event_strategy_id

    monkeypatch.setenv("STRATEGY_ID", "supertrend-mq65551c")
    # The fallback is the engine's internal id; when the variable is set the slug wins
    assert resolve_event_strategy_id("SuperTrendStrategy-000") == "supertrend-mq65551c"


def test_resolve_event_strategy_id_falls_back_when_env_absent(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import resolve_event_strategy_id

    monkeypatch.delenv("STRATEGY_ID", raising=False)
    assert resolve_event_strategy_id("SuperTrendStrategy-000") == "SuperTrendStrategy-000"


def test_resolve_event_strategy_id_blank_env_falls_back(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import resolve_event_strategy_id

    monkeypatch.setenv("STRATEGY_ID", "  ")  # blank counts as unset
    assert resolve_event_strategy_id("SuperTrendStrategy-000") == "SuperTrendStrategy-000"


# ---------------------------------------------------------------------------
# publish_order carries a LIMIT order's price, so the consumer can derive a take-profit
# from a reduce_only LIMIT order's price.
# ---------------------------------------------------------------------------


def test_publish_order_includes_limit_price(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-x", clock=mock_clock, enabled=True
    )
    publisher.publish_order(
        order_id="O-TP",
        signal_id="sig-1",
        venue_order_id=None,
        instrument="BTCUSDT.BINANCE",
        side="SELL",
        order_type="LIMIT",
        quantity="0.1",
        fill_price=None,
        status="accepted",
        reduce_only=True,
        price="67000.0",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["price"] == "67000.0"


def test_publish_order_price_default_none(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-x", clock=mock_clock, enabled=True
    )
    publisher.publish_order(
        order_id="O-1",
        signal_id=None,
        venue_order_id=None,
        instrument="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity="0.1",
        fill_price="50100",
        status="filled",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["price"] is None


# ---------------------------------------------------------------------------
# Publishing enabled with STRATEGY_ID missing must be detectable and warned about, so the
# fallback cannot silently reintroduce the foreign-key collision that loses every event.
# ---------------------------------------------------------------------------


def test_strategy_id_env_missing_true_when_unset(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import strategy_id_env_missing

    monkeypatch.delenv("STRATEGY_ID", raising=False)
    assert strategy_id_env_missing() is True


def test_strategy_id_env_missing_false_when_set(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import strategy_id_env_missing

    monkeypatch.setenv("STRATEGY_ID", "supertrend-x")
    assert strategy_id_env_missing() is False


def test_strategy_id_env_missing_true_when_blank(monkeypatch):
    from custos_toolkit_nautilus.adapter.event_publisher import strategy_id_env_missing

    monkeypatch.setenv("STRATEGY_ID", "  ")
    assert strategy_id_env_missing() is True


# ---------------------------------------------------------------------------
# Test 5: position event JSON contains required fields
# ---------------------------------------------------------------------------


def test_publish_position_json_contains_required_fields(mock_msgbus, mock_clock):
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="strat-y", clock=mock_clock, enabled=True
    )
    publisher.publish_position(
        position_id="P-001",
        signal_id="sig-002",
        instrument="ETHUSDT.BINANCE",
        side="LONG",
        quantity="5.0",
        realized_pnl="120.50",
        status="closed",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["type"] == "position"
    assert payload["position_id"] == "P-001"
    assert payload["signal_id"] == "sig-002"
    assert payload["status"] == "closed"
    assert payload["realized_pnl"] == "120.50"


# ---------------------------------------------------------------------------
# Test 6: publish failure only logs warning, never raises
# ---------------------------------------------------------------------------


def test_publish_failure_logs_warning_no_exception(mock_clock):
    broken_bus = MagicMock()
    broken_bus.publish.side_effect = RuntimeError("bus on fire")
    publisher = EventPublisher(
        msgbus=broken_bus, strategy_id="boom", clock=mock_clock, enabled=True
    )
    # Should NOT raise
    publisher.publish_signal(
        signal_id="x",
        direction="NEUTRAL",
        pair="X",
        price="0",
        strength=0.0,
    )
    # H3: publish exception is non-silent — counted as a dropped event.
    assert publisher.dropped_events == 1


# ---------------------------------------------------------------------------
# Enabled but with no message bus available: the drop must be observable, not silent
# no-publish). After removing direct XADD, external pub depends entirely on a
# db-backed msgbus; a None msgbus must be observable, not invisible.
# ---------------------------------------------------------------------------


def test_publish_enabled_but_msgbus_none_drops_observably(mock_clock, caplog):
    import logging

    publisher = EventPublisher(msgbus=None, strategy_id="no-bus", clock=mock_clock, enabled=True)
    with caplog.at_level(logging.ERROR):
        publisher.publish_signal(
            signal_id="x",
            direction="ENTER_LONG",
            pair="BTC-USDT",
            price="50000",
            strength=0.9,
        )
    # Non-silent: dropped counter increments and an ERROR was logged.
    assert publisher.dropped_events == 1
    assert any("msgbus" in r.message.lower() for r in caplog.records)


def test_publish_disabled_msgbus_none_no_drop(mock_clock):
    """disabled publisher never counts drops (enabled gate precedes msgbus check)."""
    publisher = EventPublisher(msgbus=None, strategy_id="off", clock=mock_clock, enabled=False)
    publisher.publish_signal(
        signal_id="x", direction="ENTER_LONG", pair="BTC-USDT", price="50000", strength=0.9
    )
    assert publisher.dropped_events == 0


def test_publish_bytes_payload_single_encoded(mock_msgbus, mock_clock):
    """One json.loads on the payload bytes yields the dict — it is not encoded twice."""
    publisher = EventPublisher(
        msgbus=mock_msgbus, strategy_id="enc", clock=mock_clock, enabled=True
    )
    publisher.publish_signal(
        signal_id="s", direction="ENTER_LONG", pair="BTC-USDT", price="1", strength=0.1
    )
    payload = mock_msgbus.publish.call_args[0][1]
    assert isinstance(payload, bytes)
    decoded = json.loads(payload)  # one layer → dict, not str
    assert isinstance(decoded, dict)


# ---------------------------------------------------------------------------
# Test 7: create_entry_order passes tags to order_factory.market
# ---------------------------------------------------------------------------


def test_create_entry_order_market_passes_tags():
    pytest.importorskip("nautilus_trader")
    from custos_toolkit.signals.types import Signal, SignalDirection
    from custos_toolkit_nautilus.adapter.execution import ExecutionManager
    from nautilus_trader.model.identifiers import InstrumentId

    mock_factory = MagicMock()
    mock_cache = MagicMock()
    mock_log = MagicMock()

    # Set up instrument mock
    instrument = MagicMock()
    instrument.size_precision = 3
    instrument.make_qty.return_value = MagicMock()
    mock_cache.instrument.return_value = instrument

    mgr = ExecutionManager(mock_factory, mock_cache, mock_log)
    signal = Signal(direction=SignalDirection.ENTER_LONG, price=Decimal("50000"))
    iid = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")

    bar = MagicMock()
    bar.close = Decimal("50000")

    tags = ["signal_id:abc-123"]
    mgr.create_entry_order(
        instrument_id=iid,
        signal=signal,
        size=Decimal("1000"),
        bar=bar,
        order_type="market",
        tags=tags,
    )
    mock_factory.market.assert_called_once()
    call_kwargs = mock_factory.market.call_args
    assert call_kwargs.kwargs.get("tags") == tags or call_kwargs[1].get("tags") == tags


# ---------------------------------------------------------------------------
# Test 8: create_entry_order passes tags to order_factory.limit
# ---------------------------------------------------------------------------


def test_create_entry_order_limit_passes_tags():
    pytest.importorskip("nautilus_trader")
    from custos_toolkit.signals.types import Signal, SignalDirection
    from custos_toolkit_nautilus.adapter.execution import ExecutionManager
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price

    mock_factory = MagicMock()
    mock_cache = MagicMock()
    mock_log = MagicMock()

    instrument = MagicMock()
    instrument.size_precision = 3
    instrument.price_precision = 2
    instrument.price_increment = Decimal("0.01")
    instrument.make_qty.return_value = MagicMock()
    instrument.make_price = MagicMock(return_value=Price(Decimal("49950.00"), 2))
    mock_cache.instrument.return_value = instrument

    mgr = ExecutionManager(mock_factory, mock_cache, mock_log)
    signal = Signal(direction=SignalDirection.ENTER_LONG, price=Decimal("50000"))
    iid = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")

    bar = MagicMock()
    bar.close = Decimal("50000")

    tags = ["signal_id:def-456"]
    mgr.create_entry_order(
        instrument_id=iid,
        signal=signal,
        size=Decimal("1000"),
        bar=bar,
        order_type="limit",
        tags=tags,
    )
    mock_factory.limit.assert_called_once()
    call_kwargs = mock_factory.limit.call_args
    assert call_kwargs.kwargs.get("tags") == tags or call_kwargs[1].get("tags") == tags


# ---------------------------------------------------------------------------
# Test: signal_id helpers
# ---------------------------------------------------------------------------


def test_generate_signal_id_is_valid_uuid():
    import uuid

    sid = generate_signal_id()
    # Should not raise
    uuid.UUID(sid, version=4)


def test_make_signal_tag_format():
    tag = make_signal_tag("abc-123")
    assert tag == "signal_id:abc-123"


def test_extract_signal_id_from_tags_success():
    tags = ["other_tag", "signal_id:abc-123", "extra"]
    assert extract_signal_id_from_tags(tags) == "abc-123"


def test_extract_signal_id_from_tags_none():
    assert extract_signal_id_from_tags(None) is None


def test_extract_signal_id_from_tags_empty():
    assert extract_signal_id_from_tags([]) is None


def test_extract_signal_id_from_tags_no_match():
    assert extract_signal_id_from_tags(["unrelated", "tags"]) is None


# ---------------------------------------------------------------------------
# The cohesive methods: publish_order_event and publish_position_event
# They pull the common fields off an engine event and order — order id, venue, instrument,
# trigger, reduce_only, price — and never raise, so their callers stay thin delegates.
# ---------------------------------------------------------------------------


def _ns(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_publish_order_event_extracts_common_fields(mock_msgbus, mock_clock):
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(client_order_id="O-1", venue_order_id="VO-1", instrument_id="BTCUSDT.BINANCE")
    order = _ns(trigger_price="64704.90", is_reduce_only=True, price="65000")
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id="sig-1",
        side="SELL",
        order_type="STOP_MARKET",
        quantity="0.1",
        status="accepted",
        fill_price=None,
        include_price=True,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["order_id"] == "O-1"
    assert payload["venue_order_id"] == "VO-1"
    assert payload["instrument"] == "BTCUSDT.BINANCE"
    assert payload["side"] == "SELL"
    assert payload["order_type"] == "STOP_MARKET"
    assert payload["quantity"] == "0.1"
    assert payload["status"] == "accepted"
    assert payload["trigger_price"] == "64704.90"
    assert payload["reduce_only"] is True
    assert payload["price"] == "65000"


def test_publish_order_event_omits_price_unless_requested(mock_msgbus, mock_clock):
    """include_price defaults to False, so cancelled and rejected orders carry no price."""
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(client_order_id="O-2", venue_order_id=None, instrument_id="BTCUSDT.BINANCE")
    order = _ns(trigger_price=None, is_reduce_only=False, price="65000")
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id=None,
        side="BUY",
        order_type="LIMIT",
        quantity="0.1",
        status="canceled",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["price"] is None
    assert payload["venue_order_id"] is None
    assert payload["reduce_only"] is False


def test_publish_order_event_venue_from_event_false_forces_none(mock_msgbus, mock_clock):
    """For rejected and cancel-rejected, venue_from_event=False forces venue_order_id to None,
    even when the event carries one — a rejection has no valid venue id."""
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(client_order_id="O-R", venue_order_id="VO-SHOULD-IGNORE", instrument_id="X.BINANCE")
    order = _ns(trigger_price=None, is_reduce_only=True, price=None)
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id=None,
        side="SELL",
        order_type="MARKET",
        quantity="1",
        status="rejected",
        venue_from_event=False,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["venue_order_id"] is None
    assert payload["reduce_only"] is True


def test_publish_order_event_never_raises_on_extraction_error(mock_msgbus, mock_clock):
    """A failed extraction does not escape, and increments dropped_events instead."""

    class _BoomOrder:
        is_reduce_only = False

        @property
        def trigger_price(self):
            raise RuntimeError("boom")

    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(client_order_id="O-3", venue_order_id=None, instrument_id="X.BINANCE")
    before = publisher.dropped_events
    publisher.publish_order_event(
        event=event,
        order=_BoomOrder(),
        signal_id=None,
        side="BUY",
        order_type="MARKET",
        quantity="1",
        status="filled",
    )
    assert publisher.dropped_events == before + 1


def test_publish_order_event_filled_includes_fill_latency(mock_msgbus, mock_clock):
    """A fill computes submit-to-fill latency in milliseconds from the nanosecond stamps."""
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    # ts_submitted=1000ms and ts_event=1042ms in nanoseconds, so the latency is 42ms
    event = _ns(
        client_order_id="O-LAT",
        venue_order_id="VO-1",
        instrument_id="BTCUSDT.BINANCE",
        ts_event=1_042_000_000,
    )
    order = _ns(
        trigger_price=None,
        is_reduce_only=False,
        price=None,
        ts_submitted=1_000_000_000,
        ts_init=999_000_000,
    )
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id="sig-1",
        side="BUY",
        order_type="MARKET",
        quantity="0.5",
        status="filled",
        fill_price="65000",
        include_price=True,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["fill_latency_ms"] == 42


def test_publish_order_event_non_filled_no_fill_latency(mock_msgbus, mock_clock):
    """A non-fill event such as accepted or cancelled has no latency at all."""
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(
        client_order_id="O-ACC",
        venue_order_id="VO-1",
        instrument_id="BTCUSDT.BINANCE",
        ts_event=1_042_000_000,
    )
    order = _ns(
        trigger_price=None,
        is_reduce_only=False,
        price=None,
        ts_submitted=1_000_000_000,
        ts_init=999_000_000,
    )
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id="sig-1",
        side="BUY",
        order_type="STOP_MARKET",
        quantity="0.5",
        status="accepted",
        fill_price=None,
        include_price=True,
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["fill_latency_ms"] is None


def test_publish_order_event_fill_latency_clock_anomaly_none(mock_msgbus, mock_clock):
    """A fill stamped before its submit — a clock anomaly — yields no latency, not a negative."""
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(
        client_order_id="O-NEG",
        venue_order_id="VO-1",
        instrument_id="X.BINANCE",
        ts_event=1_000_000_000,
    )
    order = _ns(
        trigger_price=None,
        is_reduce_only=False,
        price=None,
        ts_submitted=1_042_000_000,  # submitted after the fill
        ts_init=1_040_000_000,
    )
    publisher.publish_order_event(
        event=event,
        order=order,
        signal_id=None,
        side="BUY",
        order_type="MARKET",
        quantity="0.5",
        status="filled",
        fill_price="65000",
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["fill_latency_ms"] is None


def test_compute_fill_latency_non_numeric_timestamps_returns_none():
    """Non-numeric timestamps (unset / mocked) yield None instead of raising.

    A raised TypeError would be swallowed by publish_order_event's broad guard and
    drop the whole order event; a best-effort latency metric must never do that.
    """
    from custos_toolkit_nautilus.adapter.event_publisher import _compute_fill_latency_ms

    # MagicMock attrs are truthy non-numbers, so the `<` comparison must not raise.
    assert _compute_fill_latency_ms(MagicMock(), MagicMock()) is None


def test_publish_position_event_extracts_fields(mock_msgbus, mock_clock):
    publisher = EventPublisher(msgbus=mock_msgbus, strategy_id="s", clock=mock_clock, enabled=True)
    event = _ns(
        position_id="P-1", instrument_id="BTCUSDT.BINANCE", entry=_ns(name="LONG"), quantity="0.2"
    )
    publisher.publish_position_event(
        event=event, signal_id="sig-1", status="closed", realized_pnl="5.0"
    )
    payload = json.loads(mock_msgbus.publish.call_args[0][1])
    assert payload["type"] == "position"
    assert payload["position_id"] == "P-1"
    assert payload["instrument"] == "BTCUSDT.BINANCE"
    assert payload["side"] == "LONG"
    assert payload["quantity"] == "0.2"
    assert payload["status"] == "closed"
    assert payload["realized_pnl"] == "5.0"

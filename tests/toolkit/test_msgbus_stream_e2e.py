"""What the engine's message bus actually writes into a Redis stream.

Exercises the real on-disk shape the engine's ``MessageBus`` produces through
``RedisMessageBusDatabase``, confirming:

1. the stream key naming — it is ``streams_prefix`` itself, with no topic or trader suffix;
2. the entry fields ``{topic, payload}``, matching what the consumer's
   ``_parse_stream_entry`` expects;
3. that a ``bytes`` payload is encoded once, so a single ``json.loads`` yields the dict,
   while a ``str`` payload is encoded twice — which is why the publisher must send bytes;
4. that a bus reached through the real ``EventPublisher`` injection genuinely publishes

If the on-disk shape stops matching what the consumer expects, stop and settle it rather

Harness note: this must run against a real ``MessageBus`` carrying a ``database``. A bare
``MessageBus()`` has ``_database`` as ``None`` — the kernel injects it at construction —
so the whole outbound branch of ``publish_c`` is skipped and the test passes vacuously.

Needs the message-bus Redis on 6380; skips when it is unreachable. The stream prefix is
a fresh uuid so historical Redis data cannot satisfy these assertions.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

redis = pytest.importorskip("redis")
nautilus_trader = pytest.importorskip("nautilus_trader")

import msgspec  # noqa: E402
from custos_toolkit_nautilus.adapter.event_publisher import (  # noqa: E402
    EVENT_TOPIC,
    EventPublisher,
)
from nautilus_trader.common.component import LiveClock, MessageBus  # noqa: E402
from nautilus_trader.common.config import (  # noqa: E402
    DatabaseConfig,
    MessageBusConfig,
    pyo3_config_json,
)
from nautilus_trader.core import nautilus_pyo3  # noqa: E402
from nautilus_trader.core.uuid import UUID4  # noqa: E402
from nautilus_trader.model.identifiers import TraderId  # noqa: E402
from nautilus_trader.serialization.serializer import MsgSpecSerializer  # noqa: E402

REDIS_HOST = "localhost"
REDIS_PORT = 6380


def _redis_available() -> bool:
    try:
        # socket_timeout (read) is as important as socket_connect_timeout here: a
        # dead docker-proxy can keep :6380 LISTENing after the redis container is
        # gone, so connect() succeeds but ping()'s read would block forever without
        # a read timeout -- hanging pytest collection instead of skipping.
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        return bool(client.ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason=(
        "message-bus Redis@6380 unreachable; start nautilus-redis with the host 6380 "
        "loopback mapping (deploy/nautilus/docker-compose.yaml)"
    ),
)


def _build_db_backed_msgbus(stream_prefix: str) -> MessageBus:
    """A real MessageBus with ``RedisMessageBusDatabase`` injected, as the runner builds it.

    A bare ``MessageBus()`` has ``_database=None``, skips the outbound branch, and passes
    """
    trader_id = TraderId("D2GATE-001")
    instance_id = UUID4()
    mb_config = MessageBusConfig(
        database=DatabaseConfig(type="redis", host=REDIS_HOST, port=REDIS_PORT),
        encoding="json",
        timestamps_as_iso8601=True,
        buffer_interval_ms=10,
        types_filter=None,
        use_trader_prefix=False,
        use_trader_id=False,
        use_instance_id=False,
        streams_prefix=stream_prefix,
        stream_per_topic=False,
    )
    msgbus_db = nautilus_pyo3.RedisMessageBusDatabase(
        trader_id=nautilus_pyo3.TraderId(trader_id.value),
        instance_id=nautilus_pyo3.UUID4.from_str(instance_id.value),
        config_json=pyo3_config_json(mb_config),
    )
    serializer = MsgSpecSerializer(
        encoding=msgspec.json,
        timestamps_as_str=True,
        timestamps_as_iso8601=True,
    )
    return MessageBus(
        trader_id=trader_id,
        instance_id=instance_id,
        clock=LiveClock(),
        serializer=serializer,
        database=msgbus_db,
        config=mb_config,
    )


def _poll_stream(client, stream_key: str, min_entries: int = 1, timeout: float = 5.0):
    """Poll until the outbound thread flushes its buffer. Returns the entries read."""
    deadline = time.time() + timeout
    entries: list = []
    while time.time() < deadline:
        entries = client.xrange(stream_key)
        if len(entries) >= min_entries:
            return entries
        time.sleep(0.1)
    return entries


def _decode_payload(raw: str):
    """Decode either shape: the bytes path yields a dict at once, the str path needs twice."""
    value = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    return value


@pytest.fixture
def redis_client():
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def stream_prefix(redis_client):
    """A fresh stream prefix, cleaned either side, so history cannot satisfy an assertion."""
    prefix = f"d2gate_{uuid.uuid4().hex[:12]}"
    for key in redis_client.scan_iter(match=f"{prefix}*"):
        redis_client.delete(key)
    yield prefix
    for key in redis_client.scan_iter(match=f"{prefix}*"):
        redis_client.delete(key)


def test_direct_bytes_publish_writes_clean_single_encoded_json(redis_client, stream_prefix):
    """A bytes publish: stream key is the prefix, fields are topic and payload, JSON once."""
    msgbus = _build_db_backed_msgbus(stream_prefix)
    original = {"type": "signal", "src": "bytes", "n": 1}

    msgbus.publish(EVENT_TOPIC, json.dumps(original).encode("utf-8"))

    entries = _poll_stream(redis_client, stream_prefix, min_entries=1)
    assert len(entries) >= 1, "nothing reached the stream — was the outbound branch skipped?"

    # The stream key equals streams_prefix itself, with no topic or trader suffix
    keys = list(redis_client.scan_iter(match=f"{stream_prefix}*"))
    assert keys == [stream_prefix], f"the stream key should be the prefix itself, got {keys}"

    _entry_id, fields = entries[0]
    # The entry fields are topic and payload, matching the consumer's parser
    assert set(fields.keys()) == {"topic", "payload"}, (
        f"fields should be topic and payload, got {fields.keys()}"
    )
    assert fields["topic"] == EVENT_TOPIC

    # One json.loads yields the original dict, because bytes are not encoded twice
    parsed = json.loads(fields["payload"])
    assert isinstance(parsed, dict), "one json.loads on a bytes payload should give a dict"
    assert parsed == original


def test_str_publish_double_encodes(redis_client, stream_prefix):
    """A str publish goes through the serializer twice, so one loads gives a str, not a dict."""
    msgbus = _build_db_backed_msgbus(stream_prefix)
    original = {"type": "signal", "src": "str", "n": 2}

    # Deliberately a str rather than bytes, reproducing the publisher's old fallback path
    msgbus.publish(EVENT_TOPIC, json.dumps(original))

    entries = _poll_stream(redis_client, stream_prefix, min_entries=1)
    assert len(entries) >= 1, "the str publish did not reach the Redis stream"

    _entry_id, fields = entries[0]
    once = json.loads(fields["payload"])
    assert isinstance(once, str), "one loads on a str payload gives a str — the double encoding"
    twice = json.loads(once)
    assert twice == original, "only the second decode restores the dict"


def test_event_publisher_injected_msgbus_externally_publishes(redis_client, stream_prefix):
    """The bus the EventPublisher was given really publishes outward, not just locally.

    Forcing ``_redis_client=None`` takes the message-bus path, so this checks the very bus
    the strategy injected reaches Redis. It holds for either encoding, because
    ``_decode_payload`` handles both before the signal fields are asserted.
    """
    msgbus = _build_db_backed_msgbus(stream_prefix)
    # The publisher always sends bytes through the injected bus — there is no direct XADD.
    publisher = EventPublisher(msgbus=msgbus, strategy_id="d2gate-strat", enabled=True)

    signal_id = uuid.uuid4().hex
    publisher.publish_signal(
        signal_id=signal_id,
        direction="ENTER_LONG",
        pair="BTCUSDT",
        price="50000",
        strength=0.9,
    )

    entries = _poll_stream(redis_client, stream_prefix, min_entries=1)
    assert len(entries) >= 1, "the injected bus did not reach Redis — subscribed only?"

    _entry_id, fields = entries[0]
    assert fields["topic"] == EVENT_TOPIC
    parsed = _decode_payload(fields["payload"])
    assert parsed["type"] == "signal"
    assert parsed["signal_id"] == signal_id
    assert parsed["strategy_id"] == "d2gate-strat"

#!/usr/bin/env python3
"""Generate the conformance vectors consumers of Custos contracts execute.

Each vector carries the exact bytes of one message as a JSON string, so a
consumer can test its own parser and digests against them. Vectors marked
``producer-canonical`` are rendered here by the runner's production code from the
semantic ``producer_input`` they record; ``tolerance`` vectors carry spellings the
runner does not write but a consumer must handle as the expectation says.

``--check`` regenerates every vector file and compares bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import UUID

from custos_toolkit.contracts.strategy_execution import canonical_json_bytes

from custos.core import runner_deployment_lifecycle_fact as lifecycle_module
from custos.core import runtime_log_fact as runtime_log_module
from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    ORDER_EVENT_FIELDS,
    ORDER_EVENTS,
    ORDER_PRICE_RULES,
    RUNNER_FACT_CAPABILITY_SHAPES,
    RUNNER_FACT_SCHEMA_VERSION,
    RunnerFactAuthority,
    RunnerFactIdentity,
    equity_snapshot,
    execution_fill,
    fill_correction_fact,
    order_event_fact,
    position_closed,
    position_snapshot,
    runner_fact_capability_shape,
    runner_fact_signing_header,
    runner_fact_signing_preimage,
)
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactEventBridge
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
from custos.core.utc_time import render_utc_nanos

ROOT = Path(__file__).resolve().parents[1]
RUNNER_FACT_VECTORS_PATH = Path("docs/authority/conformance/runner-fact-batch-v1.vectors.json")
RUNNER_FACT_SEQUENCES_PATH = Path("docs/authority/conformance/runner-fact-batch-v1.sequences.json")
LINEAGE_GOLDEN_PATH = Path("docs/authority/runner-fact-lineage-golden-v1.json")
STRATEGY_VECTORS_PATH = Path("docs/authority/conformance/strategy-canonical-json-v1.vectors.json")
RUNNER_FACT_CONTRACT = ("alephain.custos.runner_fact_batch.v1", 4)
STRATEGY_CANONICAL_CONTRACT = ("alephain.custos.strategy_canonical_json.v1", 1)
CAPABILITY_MANIFEST_PATH = Path("docs/authority/runner-fact-capability-manifest-v1.json")

ERROR_CLASSES = (
    "unknown_field",
    "non_canonical_decimal",
    "binary_float",
    "missing_timezone",
    "digest_mismatch",
    "identity_conflict",
    "schema_violation",
    "decimal_out_of_range",
    "invariant_violation",
    "unknown_reference",
    "capability_mismatch",
)
# A test-only signing seed; it signs nothing outside these vectors.
TEST_SIGNING_SEED = bytes(range(1, 33))
TENANT_ID = "acme"
TRADING_MODE = "sandbox"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
SPEC_DIGEST = "a" * 64
GENERATION = 7
COMMAND_FINGERPRINT = "e" * 64
CORRELATION_ID = UUID("70000000-0000-4000-8000-000000000007")
BATCH_NAMESPACE = "custos.conformance.batch"

WHOLE_SECOND_NS = 1_791_075_723_000_000_000
MILLISECOND_NS = 1_791_075_723_632_000_000
MICROSECOND_NS = 1_791_075_723_632_001_000
NANOSECOND_NS = 1_791_075_723_632_001_001


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pretty(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sidecar(path: Path, payload: bytes) -> bytes:
    return f"{_sha256(payload)}  {path.name}\n".encode("ascii")


def identity() -> RunnerFactIdentity:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = (
        Ed25519PrivateKey.from_private_bytes(TEST_SIGNING_SEED)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    )
    return RunnerFactIdentity.from_private_bytes(
        TEST_SIGNING_SEED, f"ed25519-{_sha256(public)[:32]}"
    )


def authority() -> RunnerFactAuthority:
    manifest = json.loads((ROOT / CAPABILITY_MANIFEST_PATH).read_text(encoding="utf-8"))
    return RunnerFactAuthority(
        tenant_id=TENANT_ID,
        trading_mode=TRADING_MODE,
        runner_id=RUNNER_ID,
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=SPEC_ID,
        deployment_spec_digest=SPEC_DIGEST,
        generation=GENERATION,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest=_sha256(_canonical(manifest)),
    )


def lineage_authority() -> RunnerFactAuthority:
    """The stream of the lineage golden: a capability that declares order lineage
    and fill correction, so its batches may carry the two kinds."""

    golden = json.loads((ROOT / LINEAGE_GOLDEN_PATH).read_text(encoding="utf-8"))
    batch = golden["batches"][0]
    return RunnerFactAuthority(
        tenant_id=batch["tenant_id"],
        trading_mode=batch["trading_mode"],
        runner_id=UUID(batch["runner_id"]),
        deployment_instance_id=UUID(batch["deployment_instance_id"]),
        deployment_spec_id=UUID(batch["deployment_spec_id"]),
        deployment_spec_digest=batch["deployment_spec_digest"],
        generation=batch["generation"],
        strategy_id=UUID(batch["strategy_id"]),
        capability_version_id=UUID(batch["capability_version_id"]),
        capability_version=batch["capability_version"],
        capability_manifest_digest=batch["capability_manifest_digest"],
    )


@contextmanager
def clock_ns(epoch_ns: int) -> Iterator[None]:
    """Hold the runner's nanosecond clock still while a fact reads it."""

    with (
        mock.patch.object(lifecycle_module.time, "time_ns", return_value=epoch_ns),
        mock.patch.object(runtime_log_module.time, "time_ns", return_value=epoch_ns),
    ):
        yield


def lifecycle_fact(epoch_ns: int) -> dict[str, Any]:
    with clock_ns(epoch_ns):
        fact = RunnerDeploymentLifecycleFact.observed(
            authority(),
            generation=GENERATION,
            lifecycle_state="running",
            command_fingerprint=COMMAND_FINGERPRINT,
            outcome="applied",
        )
    return fact.to_wire()


def runtime_log_fact(epoch_ns: int, message: str = "strategy started") -> dict[str, Any]:
    emitter = object.__new__(RunnerRuntimeLogEmitter)
    emitter._redactor = RuntimeLogRedactor()
    with clock_ns(epoch_ns):
        return emitter._fact(
            authority(),
            level="INFO",
            component="engine",
            message=message,
            structured_fields={"phase": "start"},
            correlation_id=CORRELATION_ID,
            causation_id=None,
        )


class _CapturingEmitter:
    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []

    def emit_sync(self, _authority: RunnerFactAuthority, facts: Sequence[Mapping[str, Any]]):
        self.facts.extend(dict(fact) for fact in facts)
        return tuple(facts)


class PositionClosed:
    """The PositionClosed fields the runner's event bridge reads."""

    def __init__(self, fields: Mapping[str, Any]) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def bridged_position_closes(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn engine PositionClosed events into facts through the production bridge."""

    capture = _CapturingEmitter()
    deployment = RunnerFactDeployment(
        authority=authority(),
        deployment_instance_id=str(INSTANCE_ID),
        deployment_spec_id=str(SPEC_ID),
        deployment_spec_digest=SPEC_DIGEST,
        venue="BINANCE",
        currency="USDT",
        reconciliation_available=True,
        strategy_version="v1",
        timeframe="1-MINUTE",
    )
    bridge = RunnerFactEventBridge(emitter=capture, deployment=deployment)
    for event in events:
        bridge._on_position_event(PositionClosed(event))
    return capture.facts


def _batch_id(vector_id: str, source_seq_start: int) -> str:
    return str(
        UUID(
            bytes=hashlib.sha256(
                f"{BATCH_NAMESPACE}\0{vector_id}\0{source_seq_start}".encode()
            ).digest()[:16],
            version=4,
        )
    )


def signed_batch(
    vector_id: str,
    facts: Sequence[Mapping[str, Any]],
    *,
    emitted_at: str,
    source_seq_start: int = 1,
    auth: RunnerFactAuthority | None = None,
) -> dict[str, Any]:
    """Sequence, digest and sign facts the way the runner's outbox does."""

    sequenced = [{**fact, "seq": source_seq_start + offset} for offset, fact in enumerate(facts)]
    auth = auth or authority()
    signer = identity()
    header = {
        "schema_version": RUNNER_FACT_SCHEMA_VERSION,
        "batch_id": _batch_id(vector_id, source_seq_start),
        "tenant_id": auth.tenant_id,
        "trading_mode": auth.trading_mode,
        "runner_id": str(auth.runner_id),
        "deployment_instance_id": str(auth.deployment_instance_id),
        "deployment_spec_id": str(auth.deployment_spec_id),
        "deployment_spec_digest": auth.deployment_spec_digest,
        "generation": auth.generation,
        "strategy_id": str(auth.strategy_id),
        "capability_version_id": str(auth.capability_version_id),
        "capability_version": auth.capability_version,
        "capability_manifest_digest": auth.capability_manifest_digest,
        "key_id": signer.key_id,
        "emitted_at": emitted_at,
        "source_seq_start": source_seq_start,
        "source_seq_end": source_seq_start + len(sequenced) - 1,
        "payload_digest": _sha256(_canonical(sequenced)),
    }
    header = runner_fact_signing_header(header)
    return {
        **header,
        "facts": sequenced,
        "signature": signer.sign_batch_payload(_canonical(header)),
    }


def raw(batch: Mapping[str, Any]) -> str:
    """The batch exactly as the outbox publishes it."""

    return _canonical(batch).decode("utf-8")


def accept(batch: Mapping[str, Any], *, typed_roundtrip_equal: bool) -> dict[str, Any]:
    preimage = runner_fact_signing_preimage(batch)
    return {
        "outcome": "accept",
        "payload_digest": _sha256(_canonical(batch["facts"])),
        "envelope_digest": _sha256(_canonical(batch)),
        "signing_preimage_hex": preimage.hex(),
        "typed_roundtrip_equal": typed_roundtrip_equal,
    }


def reject(error_class: str, stage: str) -> dict[str, Any]:
    assert error_class in ERROR_CLASSES
    return {"outcome": "reject", "error_class": error_class, "stage": stage}


# The schema states some bounds only approximately: maxLength counts characters
# where the consumer counts UTF-8 bytes, and a pattern cannot compare a mantissa
# with 2**96 - 1. A vector the consumer refuses but the schema admits says so.
SCHEMA_COARSE = {"schema_outcome": "accept"}


def _closed(position_id: str, realized_pnl: Any, event_id: str) -> dict[str, Any]:
    fact = position_closed(
        event_id=UUID(event_id),
        position_id=UUID(position_id),
        realized_pnl="1",
        currency="USDT",
        opened_at="2026-08-06T07:00:00Z",
        closed_at="2026-08-06T07:01:00Z",
    )
    fact["realized_pnl"] = realized_pnl
    return fact


UTC_INSTANTS = (
    ("utc-whole-second", WHOLE_SECOND_NS, "2026-10-04T01:02:03Z"),
    ("utc-millisecond-instant", MILLISECOND_NS, "2026-10-04T01:02:03.632Z"),
    ("utc-microsecond-instant", MICROSECOND_NS, "2026-10-04T01:02:03.632001Z"),
    ("utc-nanosecond-instant", NANOSECOND_NS, "2026-10-04T01:02:03.632001001Z"),
)
NETTING_EVENTS = (
    {
        "event_id": "E-1",
        "position_id": "BTCUSDT-PERP.BINANCE-NETTING",
        "opening_order_id": "O-1",
        "realized_pnl": "10 USDT",
        "currency": "USDT",
        "ts_opened": 1_791_075_600_000_000_000,
        "ts_closed": 1_791_075_660_000_000_000,
    },
    {
        "event_id": "E-2",
        "position_id": "BTCUSDT-PERP.BINANCE-NETTING",
        "opening_order_id": "O-2",
        "realized_pnl": "-4.25 USDT",
        "currency": "USDT",
        "ts_opened": 1_791_075_720_000_000_000,
        "ts_closed": 1_791_075_780_000_000_000,
    },
)
STRING_ESCAPES = 'ord-\u0001\t"\\é😀'
FILL_EVENT = "92000000-0000-4000-8000-000000000005"
# venue_trade_id, bounded at 256 UTF-8 bytes; U+00E9 is two bytes, so 128 of them
# sit exactly at the bound with half as many characters. Control characters sit
# inside the value: the runner trims surrounding whitespace, and U+0085 is both a
# control character and whitespace.
IDENTIFIER_VECTORS: tuple[tuple[str, str, str, dict[str, Any] | None], ...] = (
    ("identifier-at-byte-limit", "é" * 128, "accept", None),
    ("identifier-over-byte-limit", "é" * 128 + "a", "reject", SCHEMA_COARSE),
    ("identifier-over-character-limit", "a" * 257, "reject", None),
    ("identifier-c0-control", "T-\u0001-1", "reject", None),
    ("identifier-delete", "T-\u007f-1", "reject", None),
    ("identifier-c1-control", "T-\u0085-1", "reject", None),
)
# Spellings the runner never writes, each tried in a decimal field of every
# consumer decoding style: a plain field of an execution fill, a top-level
# equity amount and a row inside a position snapshot.
DECIMAL_SPELLINGS: tuple[tuple[str, Any, str], ...] = (
    ("trailing-zero", "1.50", "non_canonical_decimal"),
    ("exponent", "1e-8", "non_canonical_decimal"),
    ("negative-zero", "-0", "non_canonical_decimal"),
    ("leading-plus", "+1", "non_canonical_decimal"),
    ("scale-29", "0." + "0" * 28 + "1", "decimal_out_of_range"),
    # 1.5 is exact in binary, so the JSON number reads the same in every parser.
    ("json-number", 1.5, "binary_float"),
)
LARGEST_DECIMAL = "79228162514264337593543950335"  # 2**96 - 1


def _utc_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, epoch_ns, rendered in UTC_INSTANTS:
        if render_utc_nanos(epoch_ns) != rendered:
            raise RuntimeError(f"{vector_id}: the runner renders {render_utc_nanos(epoch_ns)}")
        batch = signed_batch(
            vector_id,
            [lifecycle_fact(epoch_ns), runtime_log_fact(epoch_ns)],
            emitted_at=rendered,
        )
        vectors.append(
            {
                "id": vector_id,
                "origin": "producer-canonical",
                "covers": ["UTC rendering of signed instants"],
                "producer_input": {"occurred_at_epoch_ns": epoch_ns},
                "raw": raw(batch),
                "expected": accept(batch, typed_roundtrip_equal=True),
            }
        )
    for vector_id, lexeme, covers in (
        ("utc-six-digit-fraction", "2026-10-04T01:02:03.632000Z", "isoformat microseconds"),
        ("utc-nine-digit-fraction", "2026-10-04T01:02:03.632000000Z", "fixed nine digits"),
    ):
        facts = [lifecycle_fact(MILLISECOND_NS), runtime_log_fact(MILLISECOND_NS)]
        facts[0]["occurred_at"] = facts[0]["observed_at"] = lexeme
        facts[1]["occurred_at"] = lexeme
        batch = signed_batch(vector_id, facts, emitted_at=lexeme)
        vectors.append(
            {
                "id": vector_id,
                "origin": "tolerance",
                "covers": [f"a former runner spelling of an instant ({covers})"],
                "raw": raw(batch),
                "expected": accept(batch, typed_roundtrip_equal=False),
            }
        )
    return vectors


def runner_fact_vectors() -> list[dict[str, Any]]:
    vectors = _utc_vectors()

    reopened = bridged_position_closes(NETTING_EVENTS)
    batch = signed_batch(
        "netting-reopen-two-lifecycles", reopened, emitted_at="2026-10-04T13:43:00Z"
    )
    vectors.append(
        {
            "id": "netting-reopen-two-lifecycles",
            "origin": "producer-canonical",
            "covers": ["NETTING reopen: one engine position slot, two lifecycles"],
            "producer_input": {"position_closed_events": list(NETTING_EVENTS)},
            "raw": raw(batch),
            "expected": accept(batch, typed_roundtrip_equal=True),
        }
    )

    reused = "91000000-0000-4000-8000-000000000001"
    prior = signed_batch(
        "netting-reused-position-id",
        [_closed(reused, "9", "92000000-0000-4000-8000-000000000001")],
        emitted_at="2026-10-04T13:44:00Z",
    )
    second = signed_batch(
        "netting-reused-position-id",
        [_closed(reused, "-6", "92000000-0000-4000-8000-000000000002")],
        emitted_at="2026-10-04T13:45:00Z",
        source_seq_start=2,
    )
    vectors.append(
        {
            "id": "netting-reused-position-id",
            "origin": "tolerance",
            "covers": ["two different closes under one position_id"],
            "raw_prior": [raw(prior)],
            "raw": raw(second),
            "expected": reject("identity_conflict", "projection"),
        }
    )

    for vector_id, value, error_class, covers in (
        ("realized-pnl-scientific-string", "1e-8", "non_canonical_decimal", "exponent spelling"),
        ("decimal-trailing-zero", "1.50", "non_canonical_decimal", "trailing fractional zero"),
        ("decimal-negative-zero", "-0", "non_canonical_decimal", "signed zero"),
        # 1.5 is exact in binary, so the JSON number reads the same in every parser.
        ("realized-pnl-binary-float", 1.5, "binary_float", "JSON number"),
    ):
        fact = _closed(
            "91000000-0000-4000-8000-000000000002", value, "92000000-0000-4000-8000-000000000003"
        )
        batch = signed_batch(vector_id, [fact], emitted_at="2026-10-04T13:46:00Z")
        vectors.append(
            {
                "id": vector_id,
                "origin": "tolerance",
                "covers": [f"position_closed.realized_pnl {covers}"],
                "raw": raw(batch),
                "expected": reject(error_class, "decode"),
            }
        )

    scientific = position_closed(
        event_id=UUID("92000000-0000-4000-8000-000000000004"),
        position_id=UUID("91000000-0000-4000-8000-000000000003"),
        realized_pnl=Decimal("1E-8"),
        currency="USDT",
        opened_at="2026-08-06T07:00:00Z",
        closed_at="2026-08-06T07:01:00Z",
    )
    batch = signed_batch(
        "decimal-from-scientific-input", [scientific], emitted_at="2026-10-04T13:47:00Z"
    )
    vectors.append(
        {
            "id": "decimal-from-scientific-input",
            "origin": "producer-canonical",
            "covers": ["a decimal held in exponent form is written in plain notation"],
            "producer_input": {"realized_pnl": "1E-8"},
            "raw": raw(batch),
            "expected": accept(batch, typed_roundtrip_equal=True),
        }
    )

    fill = execution_fill(
        venue="BINANCE",
        venue_trade_id="T-1",
        venue_order_id="V-1",
        instrument="BTCUSDT-PERP",
        side="BUY",
        quantity="0.5",
        price="60000",
        fee="0.01",
        currency="USDT",
        occurred_at="2026-10-04T13:48:00Z",
        client_order_id=None,
        event_id=UUID("92000000-0000-4000-8000-000000000005"),
    )
    batch = signed_batch("optional-null", [fill], emitted_at="2026-10-04T13:48:01Z")
    vectors.append(
        {
            "id": "optional-null",
            "origin": "producer-canonical",
            "covers": ["a required nullable field written as null enters the digest"],
            "producer_input": {"client_order_id": None},
            "raw": raw(batch),
            "expected": accept(batch, typed_roundtrip_equal=True),
        }
    )

    batch = signed_batch(
        "unknown-field", [lifecycle_fact(WHOLE_SECOND_NS)], emitted_at="2026-10-04T13:49:00Z"
    )
    vectors.append(
        {
            "id": "unknown-field",
            "origin": "tolerance",
            "covers": ["a top-level member the contract does not define"],
            "raw": raw({**batch, "x_unknown": True}),
            "expected": reject("unknown_field", "decode"),
        }
    )

    # A free-text field: both sides keep control characters there, so the vector
    # tests escaping alone. Identifier fields refuse control characters on the
    # consumer side, which the vectors do not paper over.
    escaped = runtime_log_fact(WHOLE_SECOND_NS, message=STRING_ESCAPES)
    batch = signed_batch("string-escapes", [escaped], emitted_at="2026-10-04T13:50:01Z")
    vectors.append(
        {
            "id": "string-escapes",
            "origin": "producer-canonical",
            "covers": ["control character, tab, quote, backslash, accented letter and emoji"],
            "producer_input": {"runtime_log_message": STRING_ESCAPES},
            "raw": raw(batch),
            "expected": accept(batch, typed_roundtrip_equal=True),
        }
    )

    vectors.extend(_identifier_vectors())
    vectors.extend(_lineage_vectors())
    vectors.extend(_decimal_spelling_vectors())
    vectors.extend(_decimal_range_vectors())
    vectors.extend(_omitted_nullable_vectors())

    largest = 9_223_372_036_854_775_807
    batch = signed_batch(
        "integer-i64-max",
        [lifecycle_fact(WHOLE_SECOND_NS)],
        emitted_at="2026-10-04T13:51:00Z",
        source_seq_start=largest,
    )
    vectors.append(
        {
            "id": "integer-i64-max",
            "origin": "producer-canonical",
            "covers": ["the largest sequence a signed 64-bit store column holds"],
            "producer_input": {"source_seq_start": largest},
            "raw": raw(batch),
            "expected": accept(batch, typed_roundtrip_equal=True),
        }
    )
    return vectors


def _execution_fill(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "venue": "BINANCE",
        "venue_trade_id": "T-1",
        "venue_order_id": "V-1",
        "instrument": "BTCUSDT-PERP",
        "side": "BUY",
        "quantity": "0.5",
        "price": "60000",
        "fee": "0.01",
        "currency": "USDT",
        "occurred_at": "2026-10-04T13:48:00Z",
        "client_order_id": None,
        "event_id": UUID(FILL_EVENT),
    }
    arguments.update(overrides)
    return execution_fill(**arguments)


def _equity(amount: Any) -> dict[str, Any]:
    return equity_snapshot(
        event_id=UUID("92000000-0000-4000-8000-000000000006"),
        amount=amount,
        currency="USDT",
        observed_at="2026-10-04T13:52:00Z",
    )


def _position_snapshot() -> dict[str, Any]:
    return position_snapshot(
        event_id=UUID("92000000-0000-4000-8000-000000000007"),
        positions=[
            {"instrument": "BTCUSDT-PERP", "quantity": "1", "mark_price": "1", "currency": "USDT"}
        ],
        observed_at="2026-10-04T13:53:00Z",
    )


def _identifier_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, value, outcome, extra in IDENTIFIER_VECTORS:
        if outcome == "accept":
            fact = _execution_fill(venue_trade_id=value)
        else:
            fact = _execution_fill()
            fact["venue_trade_id"] = value
        batch = signed_batch(vector_id, [fact], emitted_at="2026-10-04T13:48:02Z")
        vector: dict[str, Any] = {
            "id": vector_id,
            "origin": "producer-canonical" if outcome == "accept" else "tolerance",
            "covers": [
                "execution_fill.venue_trade_id against the consumer's identifier bound: "
                "256 UTF-8 bytes, no control character"
            ],
        }
        if outcome == "accept":
            vector["producer_input"] = {"venue_trade_id": value}
        vector["raw"] = raw(batch)
        vector["expected"] = (
            accept(batch, typed_roundtrip_equal=True)
            if outcome == "accept"
            else {**reject("schema_violation", "decode"), **(extra or {})}
        )
        vectors.append(vector)
    return vectors


def _decimal_spelling_vectors() -> list[dict[str, Any]]:
    vectors = []
    fields: tuple[tuple[str, Callable[[], dict[str, Any]], Callable[[dict, Any], None]], ...] = (
        ("execution-fill-price", _execution_fill, lambda fact, v: fact.update(price=v)),
        ("equity-amount", lambda: _equity("100"), lambda fact, v: fact.update(amount=v)),
        (
            "position-mark-price",
            _position_snapshot,
            lambda fact, v: fact["positions"][0].update(mark_price=v),
        ),
    )
    for field_id, build, place in fields:
        for spelling_id, value, error_class in DECIMAL_SPELLINGS:
            vector_id = f"{field_id}-{spelling_id}"
            fact = build()
            place(fact, value)
            batch = signed_batch(vector_id, [fact], emitted_at="2026-10-04T13:54:00Z")
            vectors.append(
                {
                    "id": vector_id,
                    "origin": "tolerance",
                    "covers": [f"a decimal spelling the runner never writes ({spelling_id})"],
                    "raw": raw(batch),
                    "expected": reject(error_class, "decode"),
                }
            )
    return vectors


def _decimal_range_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, amount, covers in (
        ("decimal-largest-mantissa", LARGEST_DECIMAL, "the largest mantissa, 2**96 - 1"),
        ("decimal-scale-28", "0." + "0" * 27 + "1", "28 fractional digits"),
    ):
        batch = signed_batch(vector_id, [_equity(amount)], emitted_at="2026-10-04T13:55:00Z")
        vectors.append(
            {
                "id": vector_id,
                "origin": "producer-canonical",
                "covers": [f"equity_snapshot.amount at the edge of the decimal type: {covers}"],
                "producer_input": {"equity_amount": amount},
                "raw": raw(batch),
                "expected": accept(batch, typed_roundtrip_equal=True),
            }
        )
    for vector_id, amount, covers, extra in (
        ("decimal-mantissa-overflow", "79228162514264337593543950336", "2**96", SCHEMA_COARSE),
        ("decimal-thirty-digits", "1" + "0" * 29, "30 digits", None),
    ):
        fact = _equity("1")
        fact["amount"] = amount
        batch = signed_batch(vector_id, [fact], emitted_at="2026-10-04T13:55:01Z")
        vectors.append(
            {
                "id": vector_id,
                "origin": "tolerance",
                "covers": [f"equity_snapshot.amount past the decimal type: {covers}"],
                "raw": raw(batch),
                "expected": {**reject("decimal_out_of_range", "decode"), **(extra or {})},
            }
        )
    return vectors


def _omitted_nullable_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, fact, member in (
        ("required-nullable-omitted-client-order-id", _execution_fill(), "client_order_id"),
        (
            "required-nullable-omitted-causation-id",
            runtime_log_fact(WHOLE_SECOND_NS),
            "causation_id",
        ),
    ):
        assert fact[member] is None
        del fact[member]
        batch = signed_batch(vector_id, [fact], emitted_at="2026-10-04T13:56:00Z")
        vectors.append(
            {
                "id": vector_id,
                "origin": "tolerance",
                "covers": [f"{member} is required and may be null; leaving it out is refused"],
                "raw": raw(batch),
                "expected": reject("schema_violation", "decode"),
            }
        )
    return vectors


# Order lineage and fill correction. Every fact below is signed on the lineage
# stream; the runner's builders write the accepted ones and an edit of a built
# fact writes each refused one, so a refusal comes from the edit alone.

LINEAGE_ORDER = "O-1"
LINEAGE_SOURCE = "c0000000-0000-4000-8000-0000000000aa"
LINEAGE_SAMPLE: dict[str, Any] = {
    "instrument": "BTCUSDT-PERP.BINANCE",
    "side": "buy",
    "order_type": "limit",
    "time_in_force": "gtc",
    "reduce_only": False,
    "order_purpose": "entry",
    "execution_relation": "original",
    "attribution_source": "order_tag",
    "signal_id": "d0000000-0000-4000-8000-0000000000bb",
    "unattributed_reason": "missing_signal_tag",
    "derived_signal_fact_id": "e0000000-0000-4000-8000-0000000000cc",
    "closes_signal_id": "f0000000-0000-4000-8000-0000000000dd",
    "parent_client_order_id": "O-0",
    "retry_of_client_order_id": "O-00",
    "replaces_client_order_id": "O-000",
    "exec_spawn_id": "O-SPAWN",
    "order_list_id": "OL-1",
    "venue_parent_order_id": "VP-1",
    "contingency_type": "oco",
    "quantity": "100",
    "quantity_multiplier": "0.01",
    "limit_price": "60000",
    "trigger_price": "59000",
    "requested_values_known": True,
    "requested_quantity": "50",
    "requested_limit_price": "61000",
    "requested_trigger_price": "58000",
    "reason_code": "price_band",
    "last_trigger_price": "58500",
    "voided_venue_trade_id": "T-1",
    "correction_id": "C-1",
    "voided_quantity": "30",
    "is_reopened": True,
    "from_reconciliation": False,
    "order_status_after": "partially_filled",
    "leaves_quantity_after": "40",
    "commission_voided": "0.3",
    "commission_voided_currency": "USDT",
    "fill_commission": "0.6",
    "fill_commission_currency": "USDT",
}
_CREATION = (
    "instrument",
    "side",
    "order_type",
    "time_in_force",
    "reduce_only",
    "order_purpose",
    "execution_relation",
    "attribution_source",
    "signal_id",
    "quantity",
    "quantity_multiplier",
    "limit_price",
)
LINEAGE_MINIMAL: dict[str, dict[str, Any]] = {
    "initialized": {name: LINEAGE_SAMPLE[name] for name in _CREATION},
    "refused": {
        **{name: LINEAGE_SAMPLE[name] for name in _CREATION},
        "reason_code": "runner_cap_exceeded",
    },
    "submitted": {},
    "updated": {"quantity": "80", "quantity_multiplier": "0.01"},
    "rejected": {"reason_code": "insufficient_margin"},
    "denied": {"reason_code": "risk_denied"},
    "canceled": {},
    "expired": {},
    "modify_refused": {
        "requested_values_known": True,
        "requested_quantity": "50",
        "quantity_multiplier": "0.01",
        "reason_code": "runner_cap_exceeded",
    },
    "modify_rejected": {"requested_values_known": False, "reason_code": "venue_rejected"},
    "cancel_rejected": {"reason_code": "order_not_found"},
    "fill_voided": {
        name: LINEAGE_SAMPLE[name]
        for name in (
            "instrument",
            "voided_venue_trade_id",
            "correction_id",
            "voided_quantity",
            "quantity_multiplier",
            "is_reopened",
            "from_reconciliation",
            "order_status_after",
            "leaves_quantity_after",
        )
    },
}
# The rows of the field-by-event matrix; one field stands for its row.
MATRIX_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("instrument", ("instrument",)),
    ("order elements", ("side", "order_type", "time_in_force", "reduce_only")),
    ("attribution", ("order_purpose", "execution_relation", "attribution_source")),
    ("attribution links", ("signal_id", "unattributed_reason", "derived_signal_fact_id")),
    (
        "relations",
        (
            "closes_signal_id",
            "parent_client_order_id",
            "retry_of_client_order_id",
            "replaces_client_order_id",
        ),
    ),
    ("engine records", ("exec_spawn_id", "order_list_id", "venue_parent_order_id")),
    ("confirmed quantity", ("quantity",)),
    ("quantity multiplier", ("quantity_multiplier",)),
    ("confirmed prices", ("limit_price", "trigger_price")),
    ("request known", ("requested_values_known",)),
    ("requested values", ("requested_limit_price", "requested_trigger_price")),
    ("reason code", ("reason_code",)),
    ("last trigger price", ("last_trigger_price",)),
    (
        "void",
        (
            "voided_venue_trade_id",
            "correction_id",
            "is_reopened",
            "from_reconciliation",
            "order_status_after",
        ),
    ),
    ("void commission", ("commission_voided", "commission_voided_currency")),
)
_BASE_OF = {
    "quantity": "base_quantity",
    "requested_quantity": "requested_base_quantity",
    "voided_quantity": "voided_base_quantity",
    "leaves_quantity_after": "leaves_base_quantity_after",
}


def order_event(event: str, **fields: Any) -> dict[str, Any]:
    """An order event the runner's builder writes on the lineage stream."""

    given = {**LINEAGE_MINIMAL[event], **fields}
    return order_event_fact(
        lineage_authority(),
        order_event=event,
        client_order_id=LINEAGE_ORDER,
        source_event_id=given.pop("source_event_id", LINEAGE_SOURCE),
        occurred_at=given.pop("occurred_at", "2026-10-19T08:00:00.123456Z"),
        **{name: value for name, value in given.items() if value is not None},
    )


def correction(**fields: Any) -> dict[str, Any]:
    given: dict[str, Any] = {
        "fill_id": "a0000000-0000-4000-8000-000000000011",
        "venue": "BINANCE",
        "voided_venue_trade_id": "T-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "order_side": "buy",
        "last_px": "60000",
        "source_event_id": LINEAGE_SOURCE,
        "correction_id": "C-1",
        "voided_quantity": "30",
        "quantity_multiplier": "0.01",
        "is_reopened": True,
        "from_reconciliation": False,
        "occurred_at": "2026-10-19T08:00:00.123456Z",
        **fields,
    }
    return fill_correction_fact(
        lineage_authority(), **{name: value for name, value in given.items() if value is not None}
    )


def _wire(fact: dict[str, Any], field: str, value: Any) -> None:
    """Set a field on a built fact, with its base quantity when it has one."""

    fact[field] = value
    base = _BASE_OF.get(field)
    multiplier = fact.get("quantity_multiplier")
    if base is not None and multiplier is not None:
        product = format(Decimal(value) * Decimal(multiplier), "f")
        if "." in product:
            product = product.rstrip("0").rstrip(".")
        fact[base] = product


def _lineage_vector(
    vector_id: str,
    covers: str,
    facts: list[dict[str, Any]],
    expected: dict[str, Any] | None = None,
    producer_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    batch = signed_batch(
        vector_id, facts, emitted_at="2026-10-19T08:00:01Z", auth=lineage_authority()
    )
    vector: dict[str, Any] = {
        "id": vector_id,
        "origin": "producer-canonical" if expected is None else "tolerance",
        "covers": [covers],
    }
    if expected is None:
        vector["producer_input"] = producer_input or {}
    vector["raw"] = raw(batch)
    vector["expected"] = accept(batch, typed_roundtrip_equal=True) if expected is None else expected
    return vector


def _matrix_vectors() -> list[dict[str, Any]]:
    vectors = []
    for event in ORDER_EVENTS:
        vectors.append(
            _lineage_vector(
                f"order-event-{event}",
                f"the smallest {event} order event",
                [order_event(event)],
                producer_input={"order_event": event, **LINEAGE_MINIMAL[event]},
            )
        )
        required, optional = ORDER_EVENT_FIELDS[event]
        for row, fields in MATRIX_ROWS:
            present = [name for name in fields if name in required | optional]
            if not present:
                fact = order_event(event)
                _wire(fact, fields[0], LINEAGE_SAMPLE[fields[0]])
                vectors.append(
                    _lineage_vector(
                        f"order-event-{event}-forbids-{row.replace(' ', '-')}",
                        f"{event} carries no {row} ({fields[0]})",
                        [fact],
                        reject("schema_violation", "decode"),
                    )
                )
            needed = [name for name in fields if name in required]
            if needed:
                fact = order_event(event)
                fact.pop(needed[0])
                fact.pop(_BASE_OF.get(needed[0], ""), None)
                vectors.append(
                    _lineage_vector(
                        f"order-event-{event}-needs-{row.replace(' ', '-')}",
                        f"{event} needs its {row} ({needed[0]})",
                        [fact],
                        reject("schema_violation", "decode"),
                    )
                )
    return vectors


def _price_vectors() -> list[dict[str, Any]]:
    vectors = []
    for order_type, (limit, trigger, _, _) in ORDER_PRICE_RULES.items():
        prices = {
            "order_type": order_type,
            "limit_price": "60000" if limit != "absent" else None,
            "trigger_price": "59000" if trigger != "absent" else None,
        }
        vectors.append(
            _lineage_vector(
                f"order-price-{order_type}",
                f"a {order_type} order carries the prices its type has",
                [order_event("initialized", **prices)],
                producer_input=prices,
            )
        )
        for field, rule in (("limit_price", limit), ("trigger_price", trigger)):
            if rule == "optional":
                continue
            fact = order_event("initialized", **prices)
            if rule == "required":
                fact.pop(field)
                outcome = "needs"
            else:
                fact[field] = "60000"
                outcome = "forbids"
            vectors.append(
                _lineage_vector(
                    f"order-price-{order_type}-{outcome}-{field.replace('_', '-')}",
                    f"a {order_type} order {outcome} {field}",
                    [fact],
                    reject("schema_violation", "decode"),
                )
            )
    return vectors


ATTRIBUTION_ROWS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("tagged", "order_tag", "entry", {"signal_id": LINEAGE_SAMPLE["signal_id"]}),
    (
        "derived",
        "derived",
        "entry",
        {"derived_signal_fact_id": LINEAGE_SAMPLE["derived_signal_fact_id"]},
    ),
    (
        "derived-protective",
        "derived",
        "protective_stop",
        {"unattributed_reason": "legacy_without_signal"},
    ),
    ("unattributed", "none", "flatten", {"unattributed_reason": "runner_shutdown_flatten"}),
)


def _attribution_vectors() -> list[dict[str, Any]]:
    vectors = []
    for row, source, purpose, links in ATTRIBUTION_ROWS:
        inputs = {
            "attribution_source": source,
            "order_purpose": purpose,
            "signal_id": None,
            **links,
        }
        built = order_event("initialized", **inputs)
        vectors.append(
            _lineage_vector(
                f"order-attribution-{row}",
                f"attribution {source} on a {purpose} order carries exactly its link",
                [built],
                producer_input={k: v for k, v in inputs.items() if v is not None},
            )
        )
        missing = dict(built)
        for link in links:
            missing.pop(link)
        vectors.append(
            _lineage_vector(
                f"order-attribution-{row}-without-link",
                f"attribution {source} without its link",
                [missing],
                reject("schema_violation", "decode"),
            )
        )
        extra_link = next(
            name
            for name in ("signal_id", "derived_signal_fact_id", "unattributed_reason")
            if name not in links
        )
        doubled = {**built, extra_link: LINEAGE_SAMPLE[extra_link]}
        vectors.append(
            _lineage_vector(
                f"order-attribution-{row}-with-another-link",
                f"attribution {source} with another link ({extra_link})",
                [doubled],
                reject("schema_violation", "decode"),
            )
        )
    wrong_reason = order_event(
        "initialized",
        attribution_source="derived",
        order_purpose="take_profit",
        signal_id=None,
        unattributed_reason="legacy_without_signal",
    )
    wrong_reason["unattributed_reason"] = "missing_signal_tag"
    vectors.append(
        _lineage_vector(
            "order-attribution-derived-protective-not-legacy",
            "a protective order under a derived signal is legacy_without_signal",
            [wrong_reason],
            reject("schema_violation", "decode"),
        )
    )
    unknown_purpose = order_event("initialized")
    unknown_purpose["order_purpose"] = "hedge"
    vectors.append(
        _lineage_vector(
            "order-purpose-unknown",
            "an order purpose outside the closed set",
            [unknown_purpose],
            reject("schema_violation", "decode"),
        )
    )
    unknown_relation = order_event("initialized")
    unknown_relation["execution_relation"] = "split"
    vectors.append(
        _lineage_vector(
            "order-relation-unknown",
            "an execution relation outside the closed set",
            [unknown_relation],
            reject("schema_violation", "decode"),
        )
    )
    closes = order_event("refused", closes_signal_id=LINEAGE_SAMPLE["closes_signal_id"])
    vectors.append(
        _lineage_vector(
            "order-refused-is-self-contained",
            "a refused order carries its own attribution, elements and reversal group",
            [closes],
            producer_input={
                "order_event": "refused",
                "closes_signal_id": closes["closes_signal_id"],
            },
        )
    )
    return vectors


def _request_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, inputs in (
        (
            "order-request-known-price-only",
            {
                "requested_values_known": True,
                "requested_quantity": None,
                "quantity_multiplier": None,
                "requested_limit_price": "61000",
            },
        ),
        (
            "order-request-unknown",
            {
                "requested_values_known": False,
                "requested_quantity": None,
                "quantity_multiplier": None,
            },
        ),
    ):
        vectors.append(
            _lineage_vector(
                vector_id,
                "a refused amendment says which requested values it knows",
                [order_event("modify_rejected", **inputs)],
                producer_input={k: v for k, v in inputs.items() if v is not None},
            )
        )
    known_empty = order_event("modify_rejected", requested_values_known=False)
    known_empty["requested_values_known"] = True
    vectors.append(
        _lineage_vector(
            "order-request-known-without-values",
            "a known request names at least one requested value",
            [known_empty],
            reject("schema_violation", "decode"),
        )
    )
    unknown_with_value = order_event("modify_rejected", requested_values_known=False)
    unknown_with_value["requested_limit_price"] = "61000"
    vectors.append(
        _lineage_vector(
            "order-request-unknown-with-value",
            "an unknown request names no requested value",
            [unknown_with_value],
            reject("schema_violation", "decode"),
        )
    )
    multiplier_alone = order_event(
        "modify_rejected",
        requested_values_known=True,
        requested_quantity=None,
        quantity_multiplier=None,
        requested_limit_price="61000",
    )
    multiplier_alone["quantity_multiplier"] = "0.01"
    vectors.append(
        _lineage_vector(
            "order-request-multiplier-without-quantity",
            "a refused amendment carries quantity_multiplier only with requested_quantity",
            [multiplier_alone],
            reject("schema_violation", "decode"),
        )
    )
    return vectors


def _fee(voided: str | None, charged: str | None, currency: str = "USDT") -> dict[str, Any]:
    return {
        "commission_voided": voided,
        "commission_voided_currency": currency if voided is not None else None,
        "fill_commission": charged,
        "fill_commission_currency": "USDT" if charged is not None else None,
    }


VOID_FEES_ACCEPTED = (
    ("none", None, None),
    ("zero-on-no-charge", "0", None),
    ("charge-not-yet-reversed", None, "0.6"),
    ("part-of-charge", "0.3", "0.6"),
    ("part-of-rebate", "-0.1", "-0.2"),
    ("zero-on-zero-charge", "0", "0"),
)
VOID_FEES_REFUSED = (
    ("something-on-no-charge", "0.1", None, "USDT"),
    ("other-currency", "0.3", "0.6", "USDC"),
    ("charge-as-rebate", "-0.3", "0.6", "USDT"),
    ("rebate-as-charge", "0.1", "-0.2", "USDT"),
    ("zero-on-charge", "0", "0.6", "USDT"),
    ("zero-on-rebate", "0", "-0.2", "USDT"),
    ("over-charge", "0.7", "0.6", "USDT"),
    ("over-rebate", "-0.3", "-0.2", "USDT"),
)


def _void_vectors() -> list[dict[str, Any]]:
    vectors = []
    for case, voided, charged in VOID_FEES_ACCEPTED:
        fee = _fee(voided, charged)
        vectors.append(
            _lineage_vector(
                f"fill-void-commission-{case}",
                "a commission correction the engine takes, on both facts of one void",
                [order_event("fill_voided", **fee), correction(**fee)],
                producer_input={k: v for k, v in fee.items() if v is not None},
            )
        )
    for case, voided, charged, currency in VOID_FEES_REFUSED:
        fact = order_event("fill_voided")
        for name, value in _fee(voided, charged, currency).items():
            if value is not None:
                fact[name] = value
        vectors.append(
            _lineage_vector(
                f"fill-void-commission-{case}",
                "a commission correction the engine refuses",
                [fact],
                {**reject("invariant_violation", "decode"), **SCHEMA_COARSE},
            )
        )
    half = order_event("fill_voided", **_fee("0.3", "0.6"))
    half.pop("commission_voided_currency")
    vectors.append(
        _lineage_vector(
            "fill-void-commission-without-currency",
            "a commission amount without its currency",
            [half],
            reject("schema_violation", "decode"),
        )
    )
    zero = order_event("fill_voided")
    _wire(zero, "voided_quantity", "0")
    vectors.append(
        _lineage_vector(
            "fill-void-zero-quantity",
            "a void moves some quantity",
            [zero],
            reject("schema_violation", "decode"),
        )
    )
    left = order_event("fill_voided")
    left["order_status_after"] = "voided"
    vectors.append(
        _lineage_vector(
            "fill-void-voided-with-leaves",
            "a voided order has nothing left to fill",
            [left],
            reject("schema_violation", "decode"),
        )
    )
    filled = order_event("fill_voided")
    filled["order_status_after"] = "filled"
    vectors.append(
        _lineage_vector(
            "fill-void-ends-filled",
            "a void never leaves the order filled",
            [filled],
            reject("schema_violation", "decode"),
        )
    )
    synthetic = order_event(
        "fill_voided",
        voided_venue_trade_id="VOID-V-1",
        is_reopened=False,
        order_status_after="voided",
        leaves_quantity_after="0",
    )
    vectors.append(
        _lineage_vector(
            "fill-void-synthetic-trade",
            "a reconciliation void without a local fill is an order event",
            [synthetic],
            producer_input={"voided_venue_trade_id": "VOID-V-1"},
        )
    )
    synthetic_correction = correction()
    synthetic_correction["voided_venue_trade_id"] = "VOID-V-1"
    vectors.append(
        _lineage_vector(
            "fill-correction-synthetic-trade",
            "no signed fill exists under a synthetic void trade id (the runner also refuses "
            "it; whether a fill was signed is decided by the runner's signed-event ledger)",
            [synthetic_correction],
            reject("unknown_reference", "projection"),
        )
    )
    raw_position = correction()
    raw_position["position_id"] = "BTCUSDT-PERP.BINANCE-NETTING"
    vectors.append(
        _lineage_vector(
            "fill-correction-raw-position-id",
            "position_id names a lifecycle, not the engine's position slot",
            [raw_position],
            reject("schema_violation", "decode"),
        )
    )
    lifecycle = "a0000000-0000-5000-8000-000000000022"
    vectors.append(
        _lineage_vector(
            "fill-correction-with-position-lifecycle",
            "a correction that names its position lifecycle",
            [correction(position_id=lifecycle)],
            producer_input={"position_id": lifecycle},
        )
    )
    return vectors


def _quantity_vectors() -> list[dict[str, Any]]:
    vectors = [
        _lineage_vector(
            "order-quantity-times-multiplier",
            "base_quantity is the native quantity times the multiplier (100 x 0.01)",
            [order_event("initialized", quantity="100", quantity_multiplier="0.01")],
            producer_input={"quantity": "100", "quantity_multiplier": "0.01"},
        ),
        _lineage_vector(
            "order-quantity-fractional-multiplier",
            "a multiplier that is not a power of ten (37 x 1.3)",
            [order_event("initialized", quantity="37", quantity_multiplier="1.3")],
            producer_input={"quantity": "37", "quantity_multiplier": "1.3"},
        ),
    ]
    mismatch = order_event("initialized", quantity="37", quantity_multiplier="1.3")
    mismatch["base_quantity"] = "48"
    vectors.append(
        _lineage_vector(
            "order-quantity-base-differs",
            "a base quantity that is not native times multiplier",
            [mismatch],
            {**reject("invariant_violation", "decode"), **SCHEMA_COARSE},
        )
    )
    # 7000000000000000000000000001 x 1.3 is 9100000000000000000000000001.3, past the
    # decimal type; a 28-digit context rounds it to 9100000000000000000000000000,
    # which fits. The runner refuses to sign; a consumer must not accept the rounding.
    rounded = order_event("initialized", quantity="1", quantity_multiplier="1.3")
    rounded["quantity"] = "7000000000000000000000000001"
    rounded["base_quantity"] = "9100000000000000000000000000"
    vectors.append(
        _lineage_vector(
            "order-quantity-rounded-product",
            "a product rounded into range is not the exact product",
            [rounded],
            {**reject("invariant_violation", "decode"), **SCHEMA_COARSE},
        )
    )
    tiny = order_event("initialized", quantity="1", quantity_multiplier="0.1")
    tiny["quantity"] = "0." + "0" * 27 + "1"
    tiny["base_quantity"] = "0." + "0" * 28 + "1"
    vectors.append(
        _lineage_vector(
            "order-quantity-product-past-scale",
            "an exact product with 29 fractional digits",
            [tiny],
            reject("decimal_out_of_range", "decode"),
        )
    )
    return vectors


def _lineage_identifier_vectors() -> list[dict[str, Any]]:
    vectors = []
    for vector_id, instrument, accepted, extra in (
        ("lineage-instrument-127-bytes", "I" * 127, True, None),
        ("lineage-instrument-128-bytes", "I" * 128, True, None),
        ("lineage-instrument-129-bytes", "I" * 129, False, None),
        # 50 characters but 150 UTF-8 bytes: the schema counts characters.
        ("lineage-instrument-multibyte-over-bytes", "€" * 50, False, SCHEMA_COARSE),
    ):
        if accepted:
            vectors.append(
                _lineage_vector(
                    vector_id,
                    "instrument against the batch stream's bound of 128 UTF-8 bytes",
                    [order_event("fill_voided", instrument=instrument)],
                    producer_input={"instrument": instrument},
                )
            )
            continue
        fact = order_event("fill_voided")
        fact["instrument"] = instrument
        vectors.append(
            _lineage_vector(
                vector_id,
                "instrument against the batch stream's bound of 128 UTF-8 bytes",
                [fact],
                {**reject("schema_violation", "decode"), **(extra or {})},
            )
        )
    for vector_id, given in (
        ("lineage-uuid-upper-case-input", "C0000000-0000-4000-8000-0000000000AA"),
        ("lineage-uuid-braced-input", "{c0000000-0000-4000-8000-0000000000aa}"),
    ):
        vectors.append(
            _lineage_vector(
                vector_id,
                "a UUID given in another spelling is written lower case and hyphenated",
                [order_event("submitted", source_event_id=given)],
                producer_input={"source_event_id": given},
            )
        )
    for vector_id, written in (
        ("lineage-uuid-upper-case-wire", "C0000000-0000-4000-8000-0000000000AA"),
        ("lineage-uuid-version-zero-wire", "c0000000-0000-0000-8000-0000000000aa"),
        ("lineage-uuid-variant-wire", "c0000000-0000-4000-c000-0000000000aa"),
        ("lineage-uuid-nil-wire", "00000000-0000-0000-0000-000000000000"),
    ):
        fact = order_event("submitted")
        fact["source_event_id"] = written
        vectors.append(
            _lineage_vector(
                vector_id,
                "a UUID on the wire in a spelling the runner never writes",
                [fact],
                reject("schema_violation", "decode"),
            )
        )
    for vector_id, value, error_class in (
        ("lineage-decimal-scale-28", "0." + "0" * 27 + "1", None),
        ("lineage-decimal-scale-29", "0." + "0" * 28 + "1", "decimal_out_of_range"),
        ("lineage-decimal-negative", "-30", "schema_violation"),
        ("lineage-decimal-exponent", "1e-5", "non_canonical_decimal"),
        ("lineage-decimal-leading-zero", "030", "non_canonical_decimal"),
    ):
        if error_class is None:
            vectors.append(
                _lineage_vector(
                    vector_id,
                    "a fill void's quantity at the edge of the decimal type",
                    [order_event("fill_voided", voided_quantity=value, quantity_multiplier="1")],
                    producer_input={"voided_quantity": value, "quantity_multiplier": "1"},
                )
            )
            continue
        fact = order_event("fill_voided", quantity_multiplier="1")
        fact["voided_quantity"] = fact["voided_base_quantity"] = value
        vectors.append(
            _lineage_vector(
                vector_id,
                "a fill void's quantity in a spelling the runner never writes",
                [fact],
                reject(error_class, "decode"),
            )
        )
    return vectors


def _lineage_vectors() -> list[dict[str, Any]]:
    return [
        *_matrix_vectors(),
        *_price_vectors(),
        *_attribution_vectors(),
        *_request_vectors(),
        *_void_vectors(),
        *_quantity_vectors(),
        *_lineage_identifier_vectors(),
        _undeclared_capability_vector(),
    ]


def _undeclared_capability_vector() -> dict[str, Any]:
    batch = signed_batch(
        "lineage-kinds-under-the-canonical-capability",
        [order_event("submitted")],
        emitted_at="2026-10-19T08:00:01Z",
    )
    return {
        "id": "lineage-kinds-under-the-canonical-capability",
        "origin": "tolerance",
        "covers": ["an order event under a capability that does not declare order lineage"],
        "raw": raw(batch),
        "expected": {**reject("capability_mismatch", "decode"), **SCHEMA_COARSE},
    }


def capability_shape_vectors() -> list[dict[str, Any]]:
    """Every combination of the four optional capability flags, with the verdict
    both sides of the contract give it."""

    known = list(RUNNER_FACT_CAPABILITY_SHAPES)
    current_projectors, current_contracts = runner_fact_capability_shape(
        order_lineage=True, fill_correction=True
    )
    flags = (
        ("terminal_valuation", "RunnerInstanceTerminalValuationFact.v1", "settlement"),
        ("valuation_checkpoint", "RunnerValuationCheckpointFact.v1", "reconciliation"),
        ("order_lineage", "RunnerOrderEventFact.v1", "reconciliation"),
        ("fill_correction", "RunnerFillCorrectionFact.v1", "settlement"),
    )
    shapes = []
    for mask in range(16):
        declared = {flag: bool(mask & (1 << index)) for index, (flag, _, _) in enumerate(flags)}
        projectors = dict(current_projectors)
        contracts = {name: dict(contract) for name, contract in current_contracts.items()}
        for flag, kind, projector in flags:
            if not declared[flag]:
                projectors.pop(kind)
                contracts[projector].pop(flag)
        shapes.append(
            {
                "id": "capability-"
                + ("-".join(flag for flag, _, _ in flags if declared[flag]) or "none"),
                "declares": declared,
                "fact_kind_projectors": projectors,
                "runner_fact_contracts": contracts,
                "expected": "accept" if (projectors, contracts) in known else "reject",
            }
        )
    if sum(shape["expected"] == "accept" for shape in shapes) != len(known):
        raise RuntimeError("the capability shape vectors differ from the accepted shapes")
    return shapes


# Sequences: what a consumer's fold must make of several facts in order. Each
# step is one signed batch; its expectation is written here by hand, from the
# engine's rules, and never computed by the code under test.

SEQUENCE_FILL = "T-1"
SEQUENCE_KEYS = {
    "outcome": "accept, or reject when the consumer must not apply the step",
    "void_version": (
        "for a fill void: new (a version the engine takes), duplicate (the same "
        "event again) or regression (a version the engine refuses)"
    ),
    "order_state": "the order's coarse state after the step",
    "order_confirmed_base_quantity": "the order quantity no refused request overrides",
    "fill_effective_base_quantity": "the fill's quantity less its cumulative void",
    "commission_reversal_step": "the commission reversal this step adds, in its currency",
    "flags": "integrity marks the consumer raises on the order",
}


def _sequence_events() -> dict[str, Any]:
    """The building blocks the sequences share."""

    def at(second: int) -> str:
        return f"2026-10-19T08:00:{second:02d}Z"

    def initialized(**fields: Any) -> dict[str, Any]:
        return order_event(
            "initialized",
            source_event_id="b1000000-0000-4000-8000-000000000001",
            occurred_at=at(1),
            **fields,
        )

    def submitted() -> dict[str, Any]:
        return order_event(
            "submitted", source_event_id="b1000000-0000-4000-8000-000000000002", occurred_at=at(2)
        )

    def fill(fee: str = "0.6") -> dict[str, Any]:
        return execution_fill(
            event_id=UUID("b2000000-0000-4000-8000-000000000003"),
            venue="BINANCE",
            venue_trade_id=SEQUENCE_FILL,
            client_order_id=LINEAGE_ORDER,
            venue_order_id="V-1",
            instrument="BTCUSDT-PERP.BINANCE",
            side="buy",
            quantity="1",
            price="60000",
            fee=fee,
            currency="USDT",
            occurred_at=at(3),
        )

    def void(step: int, voided: str, **fields: Any) -> dict[str, Any]:
        given = {
            "voided_venue_trade_id": SEQUENCE_FILL,
            "correction_id": "C-1",
            "voided_quantity": voided,
            "is_reopened": True,
            "order_status_after": "partially_filled",
            "leaves_quantity_after": voided,
            "fill_commission": "0.6",
            "fill_commission_currency": "USDT",
            **fields,
        }
        return order_event(
            "fill_voided",
            source_event_id=f"b3000000-0000-4000-8000-00000000000{step}",
            occurred_at=at(10 + step),
            **given,
        )

    return {"initialized": initialized, "submitted": submitted, "fill": fill, "void": void}


def _sequence(
    sequence_id: str, covers: str, steps: list[tuple[list[dict[str, Any]], dict[str, Any]]]
) -> dict[str, Any]:
    signed = []
    next_seq = 1
    for index, (facts, expected) in enumerate(steps):
        batch = signed_batch(
            f"{sequence_id}-{index}",
            facts,
            emitted_at=f"2026-10-19T08:01:{index:02d}Z",
            source_seq_start=next_seq,
            auth=lineage_authority(),
        )
        next_seq += len(facts)
        signed.append({"raw": raw(batch), "expected": expected})
    return {"id": sequence_id, "covers": [covers], "steps": signed}


def runner_fact_sequences() -> list[dict[str, Any]]:
    e = _sequence_events()
    init, submit, fill, void = e["initialized"], e["submitted"], e["fill"], e["void"]
    opened = [
        ([init()], {"outcome": "accept", "order_state": "initialized"}),
        ([submit()], {"outcome": "accept", "order_state": "working"}),
        (
            [fill()],
            {"outcome": "accept", "order_state": "filled", "fill_effective_base_quantity": "1"},
        ),
    ]
    no_reversal = {"commission_reversal_step": {"amount": "0", "currency": "USDT"}}

    def reversal(amount: str) -> dict[str, Any]:
        return {"commission_reversal_step": {"amount": amount, "currency": "USDT"}}

    def applied(effective: str, state: str = "partially_filled") -> dict[str, Any]:
        return {
            "outcome": "accept",
            "void_version": "new",
            "order_state": state,
            "fill_effective_base_quantity": effective,
        }

    regression = {
        "outcome": "reject",
        "void_version": "regression",
        "flags": ["void_regression"],
    }
    first_void = void(1, "20")
    sequences = [
        _sequence(
            "void-grows",
            "one fill voided twice, the cumulative quantity growing",
            [
                *opened,
                ([first_void], {**applied("0.8"), **no_reversal}),
                ([void(2, "50")], {**applied("0.5"), **no_reversal}),
            ],
        ),
        _sequence(
            "void-commission-completed",
            "the same correction again, now with its commission: a new version",
            [
                *opened,
                ([first_void], {**applied("0.8"), **no_reversal}),
                (
                    [void(2, "20", commission_voided="0.12", commission_voided_currency="USDT")],
                    {**applied("0.8"), **reversal("0.12")},
                ),
            ],
        ),
        _sequence(
            "void-reopen-changed",
            "the same quantity with the reopen flag changed: a new version",
            [
                *opened,
                (
                    [
                        void(
                            1,
                            "20",
                            is_reopened=False,
                            order_status_after="voided",
                            leaves_quantity_after="0",
                        )
                    ],
                    {**applied("0.8", "voided"), **no_reversal},
                ),
                ([void(2, "20")], {**applied("0.8"), **no_reversal}),
            ],
        ),
        _sequence(
            "void-redelivered",
            "the same fact delivered again is one fact",
            [
                *opened,
                ([first_void], {**applied("0.8"), **no_reversal}),
                (
                    [first_void],
                    {
                        "outcome": "accept",
                        "void_version": "duplicate",
                        "order_state": "partially_filled",
                        "fill_effective_base_quantity": "0.8",
                        **no_reversal,
                    },
                ),
            ],
        ),
        _sequence(
            "void-commission-shrinks",
            "a smaller cumulative commission reversal is refused",
            [
                *opened,
                (
                    [void(1, "20", commission_voided="0.12", commission_voided_currency="USDT")],
                    {**applied("0.8"), **reversal("0.12")},
                ),
                (
                    [void(2, "20", commission_voided="0.06", commission_voided_currency="USDT")],
                    {**regression, "fill_effective_base_quantity": "0.8"},
                ),
            ],
        ),
        _sequence(
            "void-commission-withdrawn",
            "with a charged fill, a reversal once given cannot become absent",
            [
                *opened,
                (
                    [void(1, "20", commission_voided="0.12", commission_voided_currency="USDT")],
                    {**applied("0.8"), **reversal("0.12")},
                ),
                ([void(2, "30")], {**regression, "fill_effective_base_quantity": "0.8"}),
            ],
        ),
        _sequence(
            "void-commission-absent-on-uncharged-fill",
            "with a fill that charged nothing, a zero reversal may later be absent",
            [
                opened[0],
                opened[1],
                (
                    [fill(fee="0")],
                    {
                        "outcome": "accept",
                        "order_state": "filled",
                        "fill_effective_base_quantity": "1",
                    },
                ),
                (
                    [
                        void(
                            1,
                            "20",
                            fill_commission=None,
                            fill_commission_currency=None,
                            commission_voided="0",
                            commission_voided_currency="USDT",
                        )
                    ],
                    {**applied("0.8"), **no_reversal},
                ),
                (
                    [void(2, "30", fill_commission=None, fill_commission_currency=None)],
                    {**applied("0.7"), **no_reversal},
                ),
            ],
        ),
        _sequence(
            "void-rebate",
            "a rebate is reversed with its own sign",
            [
                opened[0],
                opened[1],
                (
                    [fill(fee="-0.2")],
                    {
                        "outcome": "accept",
                        "order_state": "filled",
                        "fill_effective_base_quantity": "1",
                    },
                ),
                (
                    [
                        void(
                            1,
                            "20",
                            fill_commission="-0.2",
                            commission_voided="-0.04",
                            commission_voided_currency="USDT",
                        )
                    ],
                    {**applied("0.8"), **reversal("-0.04")},
                ),
                (
                    [
                        void(
                            2,
                            "50",
                            fill_commission="-0.2",
                            commission_voided="-0.1",
                            commission_voided_currency="USDT",
                        )
                    ],
                    {**applied("0.5"), **reversal("-0.06")},
                ),
            ],
        ),
        _sequence(
            "void-quantity-shrinks",
            "a smaller cumulative void quantity is refused",
            [
                *opened,
                ([void(1, "50")], {**applied("0.5"), **no_reversal}),
                ([void(2, "20")], {**regression, "fill_effective_base_quantity": "0.5"}),
            ],
        ),
        _sequence(
            "void-over-fill",
            "a void larger than its fill is refused",
            [
                *opened,
                (
                    [void(1, "120", leaves_quantity_after="100")],
                    {
                        "outcome": "reject",
                        "void_version": "regression",
                        "order_state": "filled",
                        "fill_effective_base_quantity": "1",
                        "flags": ["over_void"],
                    },
                ),
            ],
        ),
        _sequence(
            "void-without-local-fill",
            "a reconciliation void of what never filled ends the order, with no correction",
            [
                opened[0],
                opened[1],
                (
                    [
                        void(
                            1,
                            "100",
                            voided_venue_trade_id="VOID-V-1",
                            is_reopened=False,
                            order_status_after="voided",
                            leaves_quantity_after="0",
                            fill_commission=None,
                            fill_commission_currency=None,
                        )
                    ],
                    {"outcome": "accept", "void_version": "new", "order_state": "voided"},
                ),
            ],
        ),
    ]
    for case, fields, flag in (
        (
            "reopened",
            {
                "is_reopened": True,
                "order_status_after": "partially_filled",
                "leaves_quantity_after": "100",
            },
            "void_without_fill_reopened",
        ),
        (
            "with-commission",
            {
                "commission_voided": "0",
                "commission_voided_currency": "USDT",
                "fill_commission": None,
                "fill_commission_currency": None,
                "commission_override": "0.1",
            },
            "void_without_fill_commission",
        ),
        ("over-order", {"voided_quantity_override": "120"}, "over_void"),
    ):
        overrides = {
            "voided_venue_trade_id": "VOID-V-1",
            "is_reopened": False,
            "order_status_after": "voided",
            "leaves_quantity_after": "0",
            "fill_commission": None,
            "fill_commission_currency": None,
        }
        commission = fields.pop("commission_override", None)
        quantity = fields.pop("voided_quantity_override", "100")
        overrides.update(fields)
        fact = void(1, "100", **overrides)
        if commission is not None:
            fact["commission_voided"] = commission
        if quantity != "100":
            _wire(fact, "voided_quantity", quantity)
        sequences.append(
            _sequence(
                f"void-without-local-fill-{case}",
                "a void of what never filled cannot reopen, reverse commission or exceed the order",
                [
                    opened[0],
                    opened[1],
                    ([fact], {"outcome": "reject", "order_state": "working", "flags": [flag]}),
                ],
            )
        )
    sequences.extend(
        [
            _sequence(
                "refused-amendment-keeps-the-order",
                "an amendment the venue refused does not change the confirmed quantity",
                [
                    opened[0],
                    opened[1],
                    (
                        [
                            order_event(
                                "modify_rejected",
                                requested_values_known=True,
                                requested_quantity="50",
                                quantity_multiplier="0.01",
                                source_event_id="b4000000-0000-4000-8000-000000000001",
                            )
                        ],
                        {
                            "outcome": "accept",
                            "order_state": "working",
                            "order_confirmed_base_quantity": "1",
                        },
                    ),
                    (
                        [
                            execution_fill(
                                event_id=UUID("b2000000-0000-4000-8000-000000000004"),
                                venue="BINANCE",
                                venue_trade_id="T-2",
                                client_order_id=LINEAGE_ORDER,
                                venue_order_id="V-1",
                                instrument="BTCUSDT-PERP.BINANCE",
                                side="buy",
                                quantity="0.5",
                                price="60000",
                                fee="0.3",
                                currency="USDT",
                                occurred_at="2026-10-19T08:00:20Z",
                            )
                        ],
                        {
                            "outcome": "accept",
                            "order_state": "partially_filled",
                            "order_confirmed_base_quantity": "1",
                        },
                    ),
                ],
            ),
            _sequence(
                "refused-price-amendment",
                "a refused price-only amendment names no quantity and keeps the order's",
                [
                    opened[0],
                    opened[1],
                    (
                        [
                            order_event(
                                "modify_rejected",
                                requested_values_known=True,
                                requested_quantity=None,
                                quantity_multiplier=None,
                                requested_limit_price="61000",
                                source_event_id="b4000000-0000-4000-8000-000000000002",
                            )
                        ],
                        {
                            "outcome": "accept",
                            "order_state": "working",
                            "order_confirmed_base_quantity": "1",
                        },
                    ),
                ],
            ),
        ]
    )
    for order_type, prices, update, accepted in (
        ("limit", {"limit_price": "60000"}, {"trigger_price": "59000"}, False),
        ("market_to_limit", {"limit_price": None}, {"limit_price": "60500"}, True),
        ("market", {"limit_price": None}, {"limit_price": "60500"}, False),
    ):
        sequences.append(
            _sequence(
                f"update-prices-{order_type.replace('_', '-')}",
                "an update carries only the prices the order's type has",
                [
                    (
                        [init(order_type=order_type, **prices)],
                        {"outcome": "accept", "order_state": "initialized"},
                    ),
                    (
                        [
                            order_event(
                                "updated",
                                quantity="100",
                                quantity_multiplier="0.01",
                                source_event_id="b5000000-0000-4000-8000-000000000001",
                                **update,
                            )
                        ],
                        {
                            "outcome": "accept",
                            "order_state": "initialized",
                            "order_confirmed_base_quantity": "1",
                        }
                        if accepted
                        else {
                            "outcome": "reject",
                            "order_state": "initialized",
                            "flags": ["price_for_type"],
                        },
                    ),
                ],
            )
        )
    return sequences


def runner_fact_sequence_document() -> dict[str, Any]:
    contract_id, revision = RUNNER_FACT_CONTRACT
    return {
        "sequence_set_version": 1,
        "contract_id": contract_id,
        "contract_revision": revision,
        "stream": "every step is one signed batch on the lineage stream, in order",
        "expected_keys": SEQUENCE_KEYS,
        "fold_rules": [
            "a fill's versions are compared by voided quantity, commission reversal and "
            "reopen flag together; a fact delivered again is recognised by its event_id",
            "a version with a smaller cumulative void quantity, a smaller absolute "
            "commission reversal, or, for a charged fill, no reversal after one was given "
            "is not applied",
            "a refused amendment never overrides the confirmed quantity or prices",
            "the signed order_status_after decides the order's state after a void",
        ],
        "test_only": True,
        "sequences": runner_fact_sequences(),
    }


def runner_fact_vector_document() -> dict[str, Any]:
    signer = identity()
    contract_id, revision = RUNNER_FACT_CONTRACT
    return {
        "vector_set_version": 1,
        "contract_id": contract_id,
        "contract_revision": revision,
        "canonicalization": "utf8-json-sort-keys-compact-v1",
        "digest": "sha256-hex",
        "error_classes": list(ERROR_CLASSES),
        "stages": {
            "decode": "refused while parsing the envelope, before any state changes",
            "projection": "accepted into the inbox, then refused by a projector",
        },
        "schema_outcome": (
            "present only where the published schema admits a vector the consumer "
            "refuses: maxLength counts characters where the consumer counts UTF-8 bytes, "
            "and a pattern cannot compare a mantissa with 2**96 - 1"
        ),
        "test_only": True,
        "test_signing_seed_hex": TEST_SIGNING_SEED.hex(),
        "test_public_key_hex": signer.public_key_bytes.hex(),
        "test_key_id": signer.key_id,
        "vectors": runner_fact_vectors(),
        "capability_shapes": capability_shape_vectors(),
    }


def _tagged(value: Any) -> Any:
    """Decode the vectors' tagged input: {"$decimal": s} and {"$float": s}."""

    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$float"}:
            return float(value["$float"])
        return {key: _tagged(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_tagged(item) for item in value]
    return value


STRATEGY_INPUTS: tuple[tuple[str, str, Any], ...] = (
    ("strategy-decimal-trailing-zero", "a decimal is normalised", {"price": {"$decimal": "1.50"}}),
    (
        "strategy-decimal-exponent",
        "an exponent decimal is written in plain notation",
        {"small": {"$decimal": "1E-8"}, "large": {"$decimal": "1E+2"}, "zero": {"$decimal": "-0"}},
    ),
    ("strategy-null-kept", "null members stay in the digest", {"a": None, "b": 1}),
    ("strategy-float-refused", "a binary float is refused", {"price": {"$float": "1.5"}}),
    (
        "strategy-key-order-unicode",
        "members sort by code point",
        {"b": 1, "a": 2, "aa": 3, "Z": 4, "é": 5, "😀": 6, "éa": 7},
    ),
    ("strategy-string-escapes", "string escaping", {"s": STRING_ESCAPES}),
)


def strategy_vector_document() -> dict[str, Any]:
    vectors = []
    for vector_id, covers, tagged in STRATEGY_INPUTS:
        vector: dict[str, Any] = {
            "id": vector_id,
            "origin": "producer-canonical",
            "covers": [covers],
            "input": tagged,
        }
        try:
            encoded = canonical_json_bytes(_tagged(tagged))
        except TypeError:
            vector["expected"] = {"outcome": "reject", "error_class": "binary_float"}
        else:
            vector["expected"] = {
                "outcome": "accept",
                "canonical_utf8": encoded.decode("utf-8"),
                "sha256": _sha256(encoded),
            }
        vectors.append(vector)
    contract_id, revision = STRATEGY_CANONICAL_CONTRACT
    return {
        "vector_set_version": 1,
        "contract_id": contract_id,
        "contract_revision": revision,
        "canonicalization": "sha256-canonical-json-v1",
        "input_tags": {
            "$decimal": "a decimal.Decimal built from the string",
            "$float": "a binary float built from the string",
        },
        "error_classes": ["binary_float"],
        "vectors": vectors,
    }


def build_assets() -> dict[Path, bytes]:
    assets: dict[Path, bytes] = {}
    documents: dict[Path, Callable[[], dict[str, Any]]] = {
        RUNNER_FACT_VECTORS_PATH: runner_fact_vector_document,
        RUNNER_FACT_SEQUENCES_PATH: runner_fact_sequence_document,
        STRATEGY_VECTORS_PATH: strategy_vector_document,
    }
    for path, build in documents.items():
        payload = _pretty(build())
        assets[path] = payload
        assets[path.with_name(path.name + ".sha256")] = _sidecar(path, payload)
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    drift: list[str] = []
    for relative, payload in build_assets().items():
        path = ROOT / relative
        if args.check:
            if not path.is_file() or path.read_bytes() != payload:
                drift.append(str(relative))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if drift:
        for relative in drift:
            print(f"conformance vectors differ: {relative}", file=sys.stderr)
        return 1
    print("conformance vectors are exact" if args.check else "generated conformance vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

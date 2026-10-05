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
    RUNNER_FACT_SCHEMA_VERSION,
    RunnerFactAuthority,
    RunnerFactIdentity,
    execution_fill,
    position_closed,
    runner_fact_signing_header,
    runner_fact_signing_preimage,
)
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactEventBridge
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
from custos.core.utc_time import render_utc_nanos

ROOT = Path(__file__).resolve().parents[1]
RUNNER_FACT_VECTORS_PATH = Path("docs/authority/conformance/runner-fact-batch-v1.vectors.json")
STRATEGY_VECTORS_PATH = Path("docs/authority/conformance/strategy-canonical-json-v1.vectors.json")
RUNNER_FACT_CONTRACT = ("alephain.custos.runner_fact_batch.v1", 2)
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
) -> dict[str, Any]:
    """Sequence, digest and sign facts the way the runner's outbox does."""

    sequenced = [{**fact, "seq": source_seq_start + offset} for offset, fact in enumerate(facts)]
    auth = authority()
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
        "test_only": True,
        "test_signing_seed_hex": TEST_SIGNING_SEED.hex(),
        "test_public_key_hex": signer.public_key_bytes.hex(),
        "test_key_id": signer.key_id,
        "vectors": runner_fact_vectors(),
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

"""Closed Native evidence protocol; fixture JSON does not prove venue execution."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import uuid4

import pytest

from custos.core.runtime_authority import RuntimeFenceStale, RuntimeStorageUnavailable
from tests.test_native_runtime_storage import storage as storage


@pytest.fixture
def evidence(storage):
    _, _, native, build = storage
    reader = build()
    document = {
        "schema_version": 1,
        "journal": deepcopy(native.document),
        "client_id": "PAPERTRADE",
        "account_id": "PAPERTRADE-001",
        "client_order_id": "close-7",
        "kind": "proven_no_fill",
        "binding": {
            "intent_id": "0x" + "11" * 32,
            "command_keccak256": "0x" + "22" * 32,
            "action": "closePositions",
            "target": "7",
            "instrument_id": "BTC-USD-PERP.PAPERTRADE",
            "side": "SELL",
            "quantity": "0.005",
            "quote_quantity": False,
            "reduce_only": True,
            "deadline": 100,
        },
        "proof": {
            "schema_version": 1,
            "journal_id": native.document["journal_id"],
            "command_keccak256": "0x" + "22" * 32,
            "sealed_at_revision": 1,
            "source": {"kind": "never_submitted"},
        },
    }
    calls = []

    def read(identifier):
        calls.append(identifier)
        if native.closed:
            raise RuntimeError("injected released Native owner")
        return native.raw if native.raw is not None else json.dumps(document)

    native.order_evidence_json = read
    return reader, native, document, calls


def expired(document):
    document["proof"]["source"] = {
        "kind": "expired_with_coverage",
        "coverage": {
            "deadline": 100,
            "coverage_block": 50,
            "scanned_through": 50,
            "block_timestamp": 101,
            "account_block": 50,
            "account_timestamp": 102,
            "account_revision": 1,
            "account_snapshot_keccak256": "0x" + "33" * 32,
        },
    }


@pytest.mark.parametrize("source", ["never_submitted", "expired_with_coverage"])
def test_reads_immutable_bound_no_fill_evidence_from_the_held_probe(evidence, source):
    reader, _, document, calls = evidence
    if source == "expired_with_coverage":
        expired(document)
    observed = reader.order_evidence("close-7")
    assert observed.kind == "proven_no_fill"
    assert observed.binding.quantity == Decimal("0.005")
    assert observed.proof.source == source
    assert observed.journal.store_id == reader.bindings.journal.store_id
    assert calls == ["close-7"]
    with pytest.raises(FrozenInstanceError):
        observed.binding.quantity = Decimal("1")


@pytest.mark.parametrize("kind", ["missing", "unproven", "filled"])
def test_nonproof_observations_cannot_be_upgraded_by_the_consumer(evidence, kind):
    reader, _, document, _ = evidence
    document["kind"] = kind
    del document["proof"]
    if kind == "missing":
        del document["binding"]
    observed = reader.order_evidence("close-7")
    assert observed.kind == kind
    assert observed.proof is None
    assert (observed.binding is None) == (kind == "missing")


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("unknown",), "extra"),
        (("kind",), "rejected"),
        (("client_id",), "OTHER"),
        (("account_id",), "PAPERTRADE-002"),
        (("client_order_id",), "other"),
        (("journal", "journal_id"), str(uuid4())),
        (("journal", "revision"), 0),
        (("journal", "canonical_path"), "/wrong.redb"),
        (("journal", "owner"), "another-wallet"),
        (("binding", "quantity"), "1e-3"),
        (("binding", "quantity"), 1),
        (("binding", "quantity"), "NaN"),
        (("binding", "quantity"), "0"),
        (("binding", "quantity"), "00.5"),
        (("binding", "side"), "BOTH"),
        (("binding", "target"), "07"),
        (("binding", "target"), str(2**256)),
        (("binding", "target"), 7),
        (("binding", "reduce_only"), 1),
        (("binding", "quote_quantity"), "false"),
        (("binding", "deadline"), True),
        (("binding", "deadline"), 2**64),
        (("binding", "command_keccak256"), "0x" + "AB" * 32),
        (("binding", "unknown"), "extra"),
        (("proof", "schema_version"), True),
        (("proof", "journal_id"), str(uuid4())),
        (("proof", "command_keccak256"), "0x" + "44" * 32),
        (("proof", "sealed_at_revision"), True),
        (("proof", "sealed_at_revision"), 0),
        (("proof", "sealed_at_revision"), 2),
        (("proof", "source", "unknown"), True),
        (("proof", "source", "kind"), "service_rejected"),
    ],
)
def test_refuses_ambiguous_cross_owner_or_conflicting_proof(evidence, path, value):
    reader, _, document, _ = evidence
    node = document
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("close-7")


@pytest.mark.parametrize(
    "field,value",
    [
        ("deadline", 99),
        ("coverage_block", True),
        ("scanned_through", 49),
        ("account_block", 49),
        ("block_timestamp", 100),
        ("account_timestamp", 100),
        ("account_timestamp", 101.0),
        ("account_revision", 0),
        ("account_snapshot_keccak256", "bad"),
        ("unknown", "extra"),
    ],
)
def test_expiry_requires_complete_strictly_past_deadline_coverage(evidence, field, value):
    reader, _, document, _ = evidence
    expired(document)
    document["proof"]["source"]["coverage"][field] = value
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("close-7")


@pytest.mark.parametrize(
    "raw", ["[]", "null", "{}", '{"kind":"missing","kind":"missing"}', "x" * 16385]
)
def test_refuses_nonobject_duplicate_or_unbounded_payloads(evidence, raw):
    reader, native, _, _ = evidence
    native.raw = raw
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("close-7")


def test_proof_revision_advances_the_shared_floor_and_never_uses_cached_success(evidence):
    reader, native, document, _ = evidence
    document["journal"]["revision"] = 5
    assert reader.order_evidence("close-7").journal.sequence == 5
    document["journal"]["revision"] = 4
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("close-7")
    document["journal"]["revision"] = 5
    native.closed = True
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("close-7")


def test_invalid_order_id_and_inherited_pid_refuse_before_native_call(evidence, monkeypatch):
    reader, _, _, calls = evidence
    with pytest.raises(RuntimeStorageUnavailable):
        reader.order_evidence("bad\nidentifier")
    assert calls == []
    monkeypatch.setattr(os, "getpid", lambda: reader._pid + 1)
    with pytest.raises(RuntimeFenceStale):
        reader.order_evidence("close-7")
    assert calls == []

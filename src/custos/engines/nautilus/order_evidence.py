"""Decode evidence produced by the held Papertrade journal, without granting execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from custos.core.local_runtime_state import RuntimeStoreIdentity


@dataclass(frozen=True)
class NativeIntentBinding:
    intent_id: str
    command_keccak256: str
    action: Literal["openPosition", "closePositions"]
    target: str | None
    instrument_id: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    quote_quantity: bool
    reduce_only: bool
    deadline: int


@dataclass(frozen=True)
class NativeExpiryCoverage:
    deadline: int
    coverage_block: int
    scanned_through: int
    block_timestamp: int
    account_block: int
    account_timestamp: int
    account_revision: int
    account_snapshot_keccak256: str


@dataclass(frozen=True)
class NativeNoFillProof:
    journal_id: UUID
    command_keccak256: str
    sealed_at_revision: int
    source: Literal["never_submitted", "expired_with_coverage"]
    coverage: NativeExpiryCoverage | None


@dataclass(frozen=True)
class NativeOrderEvidence:
    journal: RuntimeStoreIdentity
    client_id: str
    account_id: str
    client_order_id: str
    kind: Literal["missing", "unproven", "filled", "proven_no_fill"]
    binding: NativeIntentBinding | None
    proof: NativeNoFillProof | None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Native evidence field")
        result[key] = value
    return result


def decode_document(raw: str, *, maximum_bytes: int) -> dict[str, object]:
    if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= maximum_bytes:
        raise ValueError("invalid Native evidence payload size")
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if type(document) is not dict:
        raise ValueError("Native evidence must be an object")
    return cast(dict[str, object], document)


def _fields(value: object, names: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != names:
        raise ValueError("invalid Native evidence fields")
    return cast(dict[str, object], value)


def _uint(value: object, *, maximum: int = 2**64 - 1, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid Native evidence integer")
    return value


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("invalid Native journal UUID")
    identity = UUID(value)
    if identity.version != 4 or str(identity) != value:
        raise ValueError("noncanonical Native journal UUID")
    return identity


def _hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"0x[0-9a-f]{64}", value) is None:
        raise ValueError("invalid Native Keccak digest")
    return value


def _token(value: object, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid Native evidence identifier")
    return value


def journal_identity(value: object, *, owner: str, path: Path) -> RuntimeStoreIdentity:
    fields = _fields(value, {"schema_version", "journal_id", "revision", "owner", "canonical_path"})
    if _uint(fields["schema_version"]) != 1:
        raise ValueError("unsupported Native journal schema")
    if fields["owner"] != owner or fields["canonical_path"] != str(path):
        raise ValueError("Native journal owner or path differs")
    return RuntimeStoreIdentity(
        _uuid(fields["journal_id"]),
        _uint(fields["revision"], minimum=1, maximum=2**63 - 1),
    )


def _binding(value: object) -> NativeIntentBinding:
    fields = _fields(
        value,
        {
            "intent_id",
            "command_keccak256",
            "action",
            "target",
            "instrument_id",
            "side",
            "quantity",
            "quote_quantity",
            "reduce_only",
            "deadline",
        },
    )
    quantity = fields["quantity"]
    if (
        type(quantity) is not str
        or len(quantity) > 128
        or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", quantity) is None
        or Decimal(quantity) <= 0
    ):
        raise ValueError("invalid Native intent quantity")
    if fields["action"] not in {"openPosition", "closePositions"} or fields["side"] not in {
        "BUY",
        "SELL",
    }:
        raise ValueError("invalid Native intent action or side")
    if type(fields["quote_quantity"]) is not bool or type(fields["reduce_only"]) is not bool:
        raise ValueError("invalid Native intent flags")
    target = fields["target"]
    if target is not None and (
        type(target) is not str
        or len(target) > 78
        or re.fullmatch(r"0|[1-9][0-9]*", target) is None
        or int(target) >= 2**256
    ):
        raise ValueError("invalid Native intent position target")
    return NativeIntentBinding(
        _hash(fields["intent_id"]),
        _hash(fields["command_keccak256"]),
        fields["action"],
        target,
        _token(fields["instrument_id"]),
        fields["side"],
        Decimal(quantity),
        fields["quote_quantity"],
        fields["reduce_only"],
        _uint(fields["deadline"]),
    )


def _coverage(value: object, *, deadline: int) -> NativeExpiryCoverage:
    fields = _fields(
        value,
        {
            "deadline",
            "coverage_block",
            "scanned_through",
            "block_timestamp",
            "account_block",
            "account_timestamp",
            "account_revision",
            "account_snapshot_keccak256",
        },
    )
    result = NativeExpiryCoverage(
        deadline=_uint(fields["deadline"]),
        coverage_block=_uint(fields["coverage_block"]),
        scanned_through=_uint(fields["scanned_through"]),
        block_timestamp=_uint(fields["block_timestamp"]),
        account_block=_uint(fields["account_block"]),
        account_timestamp=_uint(fields["account_timestamp"]),
        account_revision=_uint(fields["account_revision"]),
        account_snapshot_keccak256=_hash(fields["account_snapshot_keccak256"]),
    )
    if (
        result.deadline != deadline
        or result.scanned_through < result.coverage_block
        or result.account_block < result.coverage_block
        or result.block_timestamp <= deadline
        or result.account_timestamp < result.block_timestamp
        or result.account_revision == 0
        or result.account_timestamp > (2**64 - 1) // 1_000_000_000
    ):
        raise ValueError("incomplete Native expiry coverage")
    return result


def _proof(
    value: object, *, journal: RuntimeStoreIdentity, binding: NativeIntentBinding
) -> NativeNoFillProof:
    fields = _fields(
        value, {"schema_version", "journal_id", "command_keccak256", "sealed_at_revision", "source"}
    )
    identity = _uuid(fields["journal_id"])
    digest = _hash(fields["command_keccak256"])
    revision = _uint(fields["sealed_at_revision"], minimum=1, maximum=journal.sequence)
    if (
        _uint(fields["schema_version"]) != 1
        or identity != journal.store_id
        or digest != binding.command_keccak256
    ):
        raise ValueError("Native proof differs from its intent or journal")
    source = fields["source"]
    if type(source) is not dict:
        raise ValueError("invalid Native proof source")
    if source.get("kind") == "never_submitted":
        _fields(source, {"kind"})
        coverage = None
    elif source.get("kind") == "expired_with_coverage":
        source = _fields(source, {"kind", "coverage"})
        coverage = _coverage(source["coverage"], deadline=binding.deadline)
    else:
        raise ValueError("unsupported Native proof source")
    return NativeNoFillProof(
        identity,
        digest,
        revision,
        cast(Literal["never_submitted", "expired_with_coverage"], source["kind"]),
        coverage,
    )


def order_evidence(
    raw: str,
    *,
    owner: str,
    path: Path,
    account_id: str,
    client_id: str,
    client_order_id: str,
) -> NativeOrderEvidence:
    document = decode_document(raw, maximum_bytes=16384)
    kind = document.get("kind")
    names = {"schema_version", "journal", "client_id", "account_id", "client_order_id", "kind"}
    if kind not in {"missing", "unproven", "filled", "proven_no_fill"}:
        raise ValueError("unsupported Native order observation")
    if kind != "missing":
        names.add("binding")
    if kind == "proven_no_fill":
        names.add("proof")
    _fields(document, names)
    if (
        _uint(document["schema_version"]) != 1
        or document["client_id"] != client_id
        or document["account_id"] != account_id
        or document["client_order_id"] != client_order_id
    ):
        raise ValueError("Native evidence routing differs")
    journal = journal_identity(document["journal"], owner=owner, path=path)
    binding = _binding(document["binding"]) if kind != "missing" else None
    proof = (
        _proof(document["proof"], journal=journal, binding=cast(NativeIntentBinding, binding))
        if kind == "proven_no_fill"
        else None
    )
    return NativeOrderEvidence(
        journal,
        client_id,
        account_id,
        client_order_id,
        kind,
        binding,
        proof,
    )


def require_client_order_id(value: str) -> None:
    _token(value)

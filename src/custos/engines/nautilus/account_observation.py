"""Decode source-coherent Native account checkpoints without granting readiness."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from custos.core.local_runtime_state import RuntimeStoreIdentity
from custos.engines.nautilus.order_evidence import (
    _fields,
    _hash,
    _token,
    _uint,
    _uuid,
    decode_document,
    journal_identity,
)


@dataclass(frozen=True)
class NativeAccountPosition:
    position_id: str
    instrument_id: str
    opening_intent_id: str | None
    is_long: bool
    use_debt: bool
    margin_raw: int
    leverage: int
    entry_price_raw: int
    price_scale_raw: int
    opened_at_ms: int


@dataclass(frozen=True)
class NativeAccountObservation:
    journal: RuntimeStoreIdentity
    checkpoint_revision: int
    source_epoch: UUID
    account_snapshot_keccak256: str
    account_material_keccak256: str
    account_id: str
    client_id: str
    account_source_healthy: bool
    session_key_address: str | None
    session_key_expiry_raw: int | None
    account_revision: int
    source_block: int
    source_timestamp: int
    wallet_revision: int
    trading_covered_block: int
    reconciliation_complete: bool
    balance_raw: int
    locked_raw: int
    queued_raw: int
    consumable_debt_raw: int
    queue_revision_raw: int
    queue_node_count: int
    cashflow_counters: tuple[int, int, int]
    positions: tuple[NativeAccountPosition, ...]
    venue_operation_intent_ids: tuple[str, ...]
    pending_client_order_ids: tuple[str, ...]


def _raw(value: object, *, positive: bool = False) -> int:
    if type(value) is not str or len(value) > 78 or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise ValueError("invalid canonical Native account raw integer")
    number = int(value)
    if number >= 2**256 or (positive and number == 0):
        raise ValueError("Native account raw integer is out of range")
    return number


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid Native account boolean")
    return value


def _array(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid bounded Native account collection")
    return value


def _position(value: object, source_timestamp: int) -> NativeAccountPosition:
    row = _fields(
        value,
        {
            "position_id",
            "instrument_id",
            "opening_intent_id",
            "is_long",
            "use_debt",
            "margin_raw",
            "leverage",
            "entry_price_raw",
            "price_scale_raw",
            "opened_at_ms",
        },
    )
    identifier = _token(row["position_id"], maximum=81)
    if not identifier.startswith("PT-"):
        raise ValueError("invalid Native account position identifier")
    _raw(identifier[3:], positive=True)
    instrument = _token(row["instrument_id"])
    if not instrument.endswith(".PAPERTRADE"):
        raise ValueError("Native account position belongs to another venue")
    scale = _raw(row["price_scale_raw"], positive=True)
    if scale > 10**18 or re.fullmatch(r"10*", str(scale)) is None:
        raise ValueError("invalid Native account price scale")
    return NativeAccountPosition(
        identifier,
        instrument,
        None if row["opening_intent_id"] is None else _hash(row["opening_intent_id"]),
        _flag(row["is_long"]),
        _flag(row["use_debt"]),
        _raw(row["margin_raw"], positive=True),
        _uint(row["leverage"], minimum=1, maximum=2**32 - 1),
        _raw(row["entry_price_raw"], positive=True),
        scale,
        _uint(row["opened_at_ms"], maximum=min(source_timestamp * 1000, (2**64 - 1) // 10**6)),
    )


def account_observation(
    raw: str,
    *,
    owner: str,
    path: Path,
    account_id: str,
    client_id: str,
) -> NativeAccountObservation:
    fields = _fields(
        decode_document(raw, maximum_bytes=262144),
        {
            "schema_version",
            "journal",
            "checkpoint_revision",
            "source_epoch",
            "account_snapshot_keccak256",
            "account_material_keccak256",
            "account_id",
            "client_id",
            "account_source_healthy",
            "session_key_address",
            "session_key_expiry_raw",
            "account_revision",
            "source_block",
            "source_timestamp",
            "wallet_revision",
            "trading_covered_block",
            "reconciliation_complete",
            "balance_raw",
            "locked_raw",
            "queued_raw",
            "consumable_debt_raw",
            "queue_revision_raw",
            "queue_node_count",
            "cashflow_counters",
            "positions",
            "venue_operation_intent_ids",
            "pending_client_order_ids",
        },
    )
    if _uint(fields["schema_version"]) != 1:
        raise ValueError("unsupported Native account observation schema")
    if fields["account_id"] != account_id or fields["client_id"] != client_id:
        raise ValueError("Native account observation route differs")
    selected = fields["session_key_address"]
    if selected is not None and (
        type(selected) is not str or re.fullmatch(r"0x[0-9a-f]{40}", selected) is None
    ):
        raise ValueError("Native selected session address is not canonical")
    expiry = fields["session_key_expiry_raw"]
    if selected is None and expiry is not None:
        raise ValueError("Native session expiry has no selected address")
    journal = journal_identity(fields["journal"], owner=owner, path=path)
    source_block = _uint(fields["source_block"])
    timestamp = _uint(fields["source_timestamp"], maximum=(2**64 - 1) // 10**9)
    positions = tuple(_position(row, timestamp) for row in _array(fields["positions"], 64))
    if len({row.position_id for row in positions}) != len(positions):
        raise ValueError("duplicate Native account position")
    locked = _raw(fields["locked_raw"])
    if sum(row.margin_raw for row in positions) != locked:
        raise ValueError("Native account locked collateral differs from its positions")
    counters = _fields(
        fields["cashflow_counters"],
        {
            "deposited_raw",
            "withdrawn_raw",
            "rewards_claimed_raw",
        },
    )
    operations = tuple(_hash(value) for value in _array(fields["venue_operation_intent_ids"], 256))
    pending = tuple(_token(value) for value in _array(fields["pending_client_order_ids"], 256))
    if len(set(operations)) != len(operations) or len(set(pending)) != len(pending):
        raise ValueError("duplicate Native account operation or pending intent")
    return NativeAccountObservation(
        journal,
        _uint(fields["checkpoint_revision"], minimum=1, maximum=journal.sequence),
        _uuid(fields["source_epoch"]),
        _hash(fields["account_snapshot_keccak256"]),
        _hash(fields["account_material_keccak256"]),
        account_id,
        client_id,
        _flag(fields["account_source_healthy"]),
        selected,
        None if expiry is None else _raw(expiry),
        _uint(fields["account_revision"], minimum=1),
        source_block,
        timestamp,
        _uint(fields["wallet_revision"], maximum=source_block),
        _uint(fields["trading_covered_block"]),
        _flag(fields["reconciliation_complete"]),
        _raw(fields["balance_raw"]),
        locked,
        _raw(fields["queued_raw"]),
        _raw(fields["consumable_debt_raw"]),
        _raw(fields["queue_revision_raw"]),
        _uint(fields["queue_node_count"]),
        (
            _raw(counters["deposited_raw"]),
            _raw(counters["withdrawn_raw"]),
            _raw(counters["rewards_claimed_raw"]),
        ),
        positions,
        operations,
        pending,
    )


def require_account_progress(
    previous: NativeAccountObservation,
    current: NativeAccountObservation,
) -> None:
    if current.session_key_address != previous.session_key_address:
        raise ValueError("Native execution client selected session changed")
    account_contents = replace(current, account_source_healthy=previous.account_source_healthy)
    if current.journal.sequence == previous.journal.sequence and account_contents != previous:
        raise ValueError("Native account contents changed without journal progress")
    if (
        current.checkpoint_revision < previous.checkpoint_revision
        or current.source_block < previous.source_block
        or current.source_timestamp < previous.source_timestamp
        or current.wallet_revision < previous.wallet_revision
        or current.queue_revision_raw < previous.queue_revision_raw
        or current.trading_covered_block < previous.trading_covered_block
    ):
        raise ValueError("Native account source coverage regressed")
    if (
        current.checkpoint_revision == previous.checkpoint_revision
        and replace(
            account_contents,
            journal=previous.journal,
            pending_client_order_ids=previous.pending_client_order_ids,
        )
        != previous
    ):
        raise ValueError("Native account checkpoint has conflicting contents")
    if current.source_epoch == previous.source_epoch and (
        current.account_revision < previous.account_revision
        or (
            current.account_revision == previous.account_revision
            and current.account_snapshot_keccak256 != previous.account_snapshot_keccak256
        )
    ):
        raise ValueError("Native account revision regressed or conflicted within its source epoch")
    if (
        current.source_epoch != previous.source_epoch
        and current.source_block == previous.source_block
        and current.source_timestamp == previous.source_timestamp
        and current.wallet_revision == previous.wallet_revision
        and current.queue_revision_raw == previous.queue_revision_raw
        and current.account_material_keccak256 != previous.account_material_keccak256
    ):
        raise ValueError("Native account material conflicted at unchanged global coverage")

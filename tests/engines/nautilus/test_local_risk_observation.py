"""Synthetic source/cache cohorts exercise collection, never production readiness."""

from __future__ import annotations

import hashlib
import importlib
import json
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("nautilus_trader")
from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    Price,
    Quantity,
    QuoteTick,
)

from custos.core.order_nonexecution import NativeDispatchBinding
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.engines.nautilus.local_reductions import LocalPapertradeReductions
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from custos.engines.nautilus.runtime_storage import PapertradeRuntimeStorageProbe
from tests.engines.nautilus.test_local_reduction_boundary import (
    FixtureHeldJournal,
    FixtureNativeFactory,
)
from tests.test_local_loss_budget import timestamp
from tests.test_local_reservations import local_risk as local_risk
from tests.test_local_reservations import reserve
from tests.test_runtime_authority import account

BTC = "BTC-USD-PERP.PAPERTRADE"
INSTRUMENT = InstrumentId.from_str(BTC)
UNIT = 10**18


class AccountJournal(FixtureHeldJournal):
    def __init__(self, risk):
        super().__init__(risk)
        self.reads = 0
        self.after_read = None
        self.document = {
            "schema_version": 1,
            "checkpoint_revision": self.revision,
            "source_epoch": str(uuid4()),
            "account_snapshot_keccak256": "0x" + "11" * 32,
            "account_material_keccak256": "0x" + "22" * 32,
            "account_id": "PAPERTRADE-001",
            "client_id": "PAPERTRADE",
            "account_source_healthy": True,
            "session_key_address": "0x" + "88" * 20,
            "session_key_expiry_raw": str(timestamp(risk) // 10**9 + 4 * 86400),
            "account_revision": 1,
            "source_block": 100,
            "source_timestamp": timestamp(risk) // 10**9,
            "wallet_revision": 100,
            "trading_covered_block": 100,
            "reconciliation_complete": True,
            "balance_raw": str(1000 * UNIT),
            "locked_raw": "0",
            "queued_raw": "0",
            "consumable_debt_raw": "0",
            "queue_revision_raw": "1",
            "queue_node_count": 0,
            "cashflow_counters": {
                "deposited_raw": str(1000 * UNIT),
                "withdrawn_raw": "0",
                "rewards_claimed_raw": "0",
            },
            "positions": [],
            "venue_operation_intent_ids": [],
            "pending_client_order_ids": [],
        }

    def account_observation_json(self):
        if self.closed:
            raise RuntimeError("synthetic original Native owner released")
        self.reads += 1
        raw = json.dumps({**self.document, "journal": self.identity()})
        if self.after_read:
            self.after_read(self.reads)
        return raw


def stamp(context):
    doc = context.native.document
    return SimpleNamespace(
        account_id=AccountId("PAPERTRADE-001"),
        ts_event=doc["source_timestamp"] * 10**9,
        info={
            "papertrade_journal_id": str(context.risk.bindings.journal.store_id),
            "papertrade_checkpoint_revision": doc["checkpoint_revision"],
            "papertrade_account_snapshot_keccak256": doc["account_snapshot_keccak256"],
            "papertrade_account_material_keccak256": doc["account_material_keccak256"],
            "papertrade_source_epoch": doc["source_epoch"],
        },
    )


def quote(context, bid="25000.1", ask="25000.1", **changes):
    values = dict(
        instrument_id=INSTRUMENT,
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str("0"),
        ask_size=Quantity.from_str("0"),
        ts_event=timestamp(context.risk),
        ts_init=timestamp(context.risk),
    )
    values.update(changes)
    return QuoteTick(**values)


@pytest.fixture
def source_context(local_risk):
    risk = local_risk
    native = AccountJournal(risk)
    storage = PapertradeRuntimeStorageProbe(
        state=risk.events,
        journal_path=risk.bindings.journal.path,
        account=account(),
        adapter_account_id="PAPERTRADE-001",
        adapter_client_id="PAPERTRADE",
        native_probe=native,
    )
    context = SimpleNamespace(risk=risk, native=native, storage=storage, positions=[], quotes={})
    context.account = SimpleNamespace(last_event=stamp(context))
    instrument = SimpleNamespace(
        id=INSTRUMENT,
        price_precision=1,
        size_precision=16,
        price_increment=Price.from_str("0.1"),
        size_increment=Quantity.from_str("0.0000000000000001"),
    )

    def positions_open(*, account_id):
        assert str(account_id) == "PAPERTRADE-001"
        return list(context.positions)

    context.cache = SimpleNamespace(
        account=lambda _: context.account,
        positions_open=positions_open,
        instrument=lambda _: instrument,
        quote=lambda identifier: context.quotes.get(str(identifier)),
    )
    context.quotes[BTC] = quote(context)
    context.semantics = NautilusCachedOrderSemantics(context.cache)
    context.port = LocalPapertradeReductions(
        store=risk.store,
        account_id="PAPERTRADE-001",
        client_id="PAPERTRADE",
        storage_probe=storage,
        native_factory=FixtureNativeFactory,
    )
    context.port.bind_runtime(context.semantics)
    api = importlib.import_module("custos.engines.nautilus.local_risk_observation")
    context.source = api.PapertradeRiskSource(
        port=context.port,
        storage_probe=storage,
        semantics=context.semantics,
        minimum_quote_init_ns=timestamp(risk),
    )
    return context


def seed_position(context, *, side="BUY", entry="250000", quantity="0.02"):
    risk, native = context.risk, context.native
    reserve(risk)
    binding = NativeDispatchBinding(
        "entry",
        "PAPERTRADE-001",
        "PAPERTRADE",
        BTC,
        side,
        Decimal("500"),
        True,
        False,
        None,
        FixtureNativeFactory.submission_params_keccak256(None),
    )
    risk.store._native_dispatch.prepare(binding=binding, missing_revision=native.revision)
    risk.store._native_dispatch.begin_dispatch("entry")
    digest = hashlib.sha256(b"risk-source-open").hexdigest()
    risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{digest}:entry",
        runtime_id=risk.manifest.run_id,
        client_order_id="entry",
        fill_notional=Decimal("500"),
        fill_quantity=Decimal(quantity),
        leaves_quantity=Decimal(0),
        position_id="PT-7",
        instrument_id=BTC,
        side=side,
    )
    native.revision += 1
    native._save_revision()
    native.document.update(
        checkpoint_revision=native.revision,
        account_revision=2,
        account_snapshot_keccak256="0x" + "33" * 32,
        balance_raw=str(900 * UNIT),
        locked_raw=str(100 * UNIT),
        positions=[
            dict(
                position_id="PT-7",
                instrument_id=BTC,
                opening_intent_id="0x" + "44" * 32,
                is_long=side == "BUY",
                use_debt=False,
                margin_raw=str(100 * UNIT),
                leverage=5,
                entry_price_raw=entry,
                price_scale_raw="10",
                opened_at_ms=native.document["source_timestamp"] * 1000,
            )
        ],
    )
    native.orders["entry"] = dict(
        kind="filled",
        binding=dict(
            intent_id="0x" + "44" * 32,
            command_keccak256="0x" + "55" * 32,
            submission_params_keccak256=binding.submission_params_keccak256,
            action="openPosition",
            target=None,
            instrument_id=BTC,
            side=side,
            quantity="500",
            quote_quantity=True,
            reduce_only=False,
            deadline=native.document["source_timestamp"] + 3600,
        ),
    )
    context.positions.append(
        SimpleNamespace(
            id=PositionId("PT-7"),
            account_id=AccountId("PAPERTRADE-001"),
            instrument_id=INSTRUMENT,
            quantity=Quantity.from_str(quantity),
            is_closed=False,
            is_long=side == "BUY",
            is_short=side == "SELL",
            avg_px_open=float("nan"),
            events=lambda: [SimpleNamespace(client_order_id=ClientOrderId("entry"))],
        )
    )
    context.account.last_event = stamp(context)


def test_flat_collection_keeps_raw_cash_and_requires_the_current_market_quote(source_context):
    c = source_context
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cash_equity_raw == 1000 * UNIT
    assert sample.observation.cohort_ready is True
    assert sample.observation.market_timestamps_ns == ((BTC, timestamp(c.risk)),)
    assert sample.fault is None


def test_missing_quote_is_pending_without_inventing_an_equity_or_a_mark(source_context):
    c = source_context
    c.quotes.clear()
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cohort_ready is False
    assert sample.observation.market_timestamps_ns == ()
    assert sample.observation.cash_equity_raw == 1000 * UNIT


def test_cohort_uses_raw_margin_and_entry_instead_of_float_average_price(source_context):
    c = source_context
    seed_position(c)
    c.quotes[BTC] = quote(c, bid="23500.1", ask="23500.1")
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cohort_ready is True
    assert sample.observation.adverse_pnl == (Fraction(-30),)
    assert sample.observation.cash_equity_raw == 1000 * UNIT


def test_cache_quantity_delivery_gap_does_not_claim_a_complete_valuation(source_context):
    c = source_context
    seed_position(c)
    c.positions[0].quantity = Quantity.from_str("0.01")
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cohort_ready is False
    assert sample.observation.adverse_pnl == ()
    assert sample.observation.cash_equity_raw == 1000 * UNIT


def test_cache_stamp_delivery_gap_is_pending(source_context):
    c = source_context
    c.account.last_event.info["papertrade_checkpoint_revision"] = 1
    assert c.source.sample(now_ns=timestamp(c.risk)).observation.cohort_ready is False


def test_original_native_owner_release_cannot_supply_another_sample(source_context):
    c = source_context
    c.native.closed = True
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        c.source.sample(now_ns=timestamp(c.risk))


def test_usedebt_is_explicitly_unsupported_even_when_consumable_debt_is_zero(source_context):
    c = source_context
    seed_position(c)
    c.native.document["positions"][0]["use_debt"] = True
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        c.source.sample(now_ns=timestamp(c.risk))


def test_changed_checkpoint_during_collection_keeps_the_latest_cash_without_mixing_cohorts(
    source_context,
):
    c = source_context

    def change(reads):
        if reads == 1:
            c.native.revision += 1
            c.native._save_revision()
            c.native.document.update(
                checkpoint_revision=c.native.revision,
                account_revision=2,
                account_snapshot_keccak256="0x" + "66" * 32,
                balance_raw=str(970 * UNIT),
            )

    c.native.after_read = change
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cash_equity_raw == 970 * UNIT
    assert sample.observation.source_digest == "0x" + "66" * 32
    assert sample.observation.cohort_ready is False


def test_rebinding_the_semantics_to_another_cache_refuses_collection(source_context):
    c = source_context
    c.semantics._cache = SimpleNamespace()
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        c.source.sample(now_ns=timestamp(c.risk))


def test_short_uses_adverse_ask_and_reaches_the_exact_thirty_dollar_boundary(source_context):
    c = source_context
    seed_position(c, side="SELL")
    c.quotes[BTC] = quote(c, bid="26499.9", ask="26499.9")
    observation = c.source.sample(now_ns=timestamp(c.risk)).observation
    assert observation.cohort_ready is True
    assert observation.adverse_pnl == (Fraction(-30),)


def test_nonintegral_base_quantity_preserves_the_full_raw_notional(source_context):
    c = source_context
    seed_position(c, entry="830001", quantity="0.0060240891276034")
    observation = c.source.sample(now_ns=timestamp(c.risk)).observation
    assert observation.cohort_ready is True
    assert c.risk.store.risk_cohort().orders[0].reservation.filled_exposure == Decimal("500")


def test_quote_from_before_this_run_is_not_accepted_as_live(source_context):
    c = source_context
    c.quotes[BTC] = quote(c, ts_init=timestamp(c.risk) - 1)
    assert c.source.sample(now_ns=timestamp(c.risk)).observation.cohort_ready is False


def test_session_expiry_exactly_at_the_exit_boundary_is_not_sufficient(source_context):
    c = source_context
    c.native.document["session_key_expiry_raw"] = str(
        c.risk.store.authorization.execution_window.exit_until
    )
    sample = c.source.sample(now_ns=timestamp(c.risk))
    assert sample.observation.cohort_ready is False
    assert sample.fault == "account"


def test_native_pending_ids_and_venue_operations_are_both_preserved(source_context):
    c = source_context
    c.native.document.update(
        pending_client_order_ids=["native-open"], venue_operation_intent_ids=["0x" + "77" * 32]
    )
    observation = c.source.sample(now_ns=timestamp(c.risk)).observation
    assert observation.cohort_ready is False
    assert set(observation.unresolved_intent_ids) == {"native-open", "0x" + "77" * 32}


def test_cache_change_between_reads_cannot_complete_a_valuation(source_context):
    c = source_context
    original, reads = c.cache.account, []

    def changed(identifier):
        account = original(identifier)
        reads.append(identifier)
        if len(reads) == 2:
            account.last_event.info["papertrade_checkpoint_revision"] += 1
        return account

    c.cache.account = changed
    assert c.source.sample(now_ns=timestamp(c.risk)).observation.cohort_ready is False

"""Bridge live native journal identity into the registered account store checks."""

from __future__ import annotations

import importlib
import json
import os
import select
import threading
from uuid import uuid4

import pytest

from custos.core.execution_events import ExecutionEventStore
from custos.core.runtime_authority import RuntimeFenceStale, RuntimeStorageUnavailable
from tests.test_runtime_authority import account


class NativeProbe:
    def __init__(self, owner, path):
        self.document = {
            "schema_version": 1,
            "journal_id": str(uuid4()),
            "revision": 1,
            "owner": owner,
            "canonical_path": str(path.resolve()),
        }
        self.raw = None
        self.calls = 0
        self.closed = False
        self.client_id = "PAPERTRADE"
        self.account_id = "PAPERTRADE-001"

    def snapshot_json(self):
        self.calls += 1
        if self.closed:
            raise RuntimeError("injected expired native owner")
        return self.raw if self.raw is not None else json.dumps(self.document)

    def order_evidence_json(self, identifier):
        self.calls += 1
        if self.closed:
            raise RuntimeError("injected expired native owner")
        return json.dumps(
            {
                "schema_version": 1,
                "journal": self.document,
                "client_id": self.client_id,
                "account_id": self.account_id,
                "client_order_id": identifier,
                "kind": "missing",
            }
        )


@pytest.fixture
def storage(tmp_path):
    module = importlib.import_module("custos.engines.nautilus.runtime_storage")
    state = ExecutionEventStore.initialize(tmp_path / "state.sqlite3")
    journal = tmp_path / "native.redb"
    journal.write_bytes(b"fake journal content for protocol unit tests only")
    identity = account()
    owner = f"{identity.chain_id}:{identity.exchange}:{identity.wallet_address}:PAPERTRADE-001"
    native = NativeProbe(owner, journal)

    def build():
        return module.PapertradeRuntimeStorageProbe(
            state=state,
            journal_path=journal,
            account=identity,
            adapter_account_id="PAPERTRADE-001",
            adapter_client_id="PAPERTRADE",
            native_probe=native,
        )

    return state, journal, native, build


def test_reads_current_native_revision_and_actual_sqlite_identity(storage):
    state, journal, native, build = storage
    reader = build()
    assert reader(state.path) == state.identity()
    first = reader(journal)
    assert first.store_id == reader.bindings.journal.store_id
    native.document["revision"] = 7
    assert reader(journal).sequence == 7
    assert native.calls == 3
    assert reader.bindings.state.store_id == state.store_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("revision", True),
        ("revision", 1.0),
        ("revision", 0),
        ("revision", -1),
        ("revision", 2**63),
        ("journal_id", "invalid"),
        ("journal_id", "00000000-0000-0000-0000-000000000000"),
        ("owner", "999:other-owner"),
        ("unknown", "unexpected"),
        ("canonical_path", "relative.redb"),
        ("canonical_path", "/another/journal.redb"),
    ],
)
def test_refuses_invalid_or_cross_account_native_identity(storage, field, value):
    _, _, native, build = storage
    native.document[field] = value
    with pytest.raises(RuntimeStorageUnavailable):
        build()


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        "null",
        '{"schema_version":1,"schema_version":1}',
        '{"revision":NaN}',
        "x" * 4097,
    ],
)
def test_refuses_ambiguous_or_unbounded_native_payload(storage, raw):
    _, _, native, build = storage
    native.raw = raw
    with pytest.raises(RuntimeStorageUnavailable):
        build()


def test_uuid_or_revision_change_is_not_a_new_registered_journal(storage):
    _, journal, native, build = storage
    reader = build()
    native.document["revision"] = 4
    assert reader(journal).sequence == 4
    native.document["revision"] = 3
    with pytest.raises(RuntimeStorageUnavailable):
        reader(journal)
    native.document["revision"] = 5
    native.document["journal_id"] = str(uuid4())
    with pytest.raises(RuntimeStorageUnavailable):
        reader(journal)


def test_owner_release_cannot_be_replaced_with_a_cached_snapshot(storage):
    _, journal, native, build = storage
    reader = build()
    native.closed = True
    with pytest.raises(RuntimeStorageUnavailable):
        reader(journal)


def test_only_two_bound_paths_can_be_read(storage, tmp_path):
    state, journal, native, build = storage
    reader = build()
    other = tmp_path / "other.sqlite3"
    other.write_text("not registered")
    with pytest.raises(RuntimeStorageUnavailable):
        reader(other)
    journal.unlink()
    journal.symlink_to(other)
    with pytest.raises(RuntimeStorageUnavailable):
        reader(journal)
    assert native.calls == 1
    state.path.rename(state.path.with_suffix(".retained"))
    with pytest.raises(RuntimeStorageUnavailable):
        reader(state.path)
    assert not state.path.exists()


@pytest.mark.filterwarnings("ignore:This process .*multi-threaded:DeprecationWarning")
@pytest.mark.parametrize("operation", ["identity", "order", "account"])
def test_fork_copy_refuses_before_native_calls_or_inherited_lock(storage, operation):
    _, journal, _, build = storage
    reader = build()
    entered, release = threading.Event(), threading.Event()

    def hold():
        with reader._lock:
            entered.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            if operation == "account":
                reader.account_observation()
            elif operation == "order":
                reader.order_evidence("close-7")
            else:
                reader(journal)
        except RuntimeFenceStale:
            os.write(write_fd, b"fenced")
        except Exception:
            os.write(write_fd, b"wrong-error")
        else:
            os.write(write_fd, b"unfenced")
        os._exit(0)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 2)[0]
        assert os.read(read_fd, 32) == b"fenced"
    finally:
        release.set()
        worker.join(2)
        os.close(read_fd)
        waited, _ = os.waitpid(child, os.WNOHANG)
        if waited == 0:
            os.kill(child, 9)
            os.waitpid(child, 0)


def test_legacy_native_snapshot_without_path_is_insufficient(storage):
    _, _, native, build = storage
    del native.document["canonical_path"]
    with pytest.raises(RuntimeStorageUnavailable):
        build()


@pytest.mark.parametrize(
    "account_id,client_id", [("OTHER-001", "PAPERTRADE"), ("PAPERTRADE-001", "OTHER")]
)
def test_route_assertion_refuses_declared_mismatch_before_native_read(
    storage, account_id, client_id
):
    _, _, native, build = storage
    reader = build()
    before = native.calls
    with pytest.raises(RuntimeStorageUnavailable):
        reader.require_route(account_id=account_id, client_id=client_id)
    assert native.calls == before


@pytest.mark.parametrize("field", ["client_id", "account_id"])
def test_route_assertion_checks_the_actual_native_response_not_just_its_constructor(storage, field):
    _, _, native, build = storage
    reader = build()
    setattr(native, field, "OTHER-001")
    with pytest.raises(RuntimeStorageUnavailable):
        reader.require_route(account_id="PAPERTRADE-001", client_id="PAPERTRADE")


def test_matching_route_assertion_uses_the_original_held_probe(storage):
    _, _, native, build = storage
    reader = build()
    reader.require_route(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    assert native.calls == 2
    native.closed = True
    with pytest.raises(RuntimeStorageUnavailable):
        reader.require_route(account_id="PAPERTRADE-001", client_id="PAPERTRADE")

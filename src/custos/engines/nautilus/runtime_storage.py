"""Read the held native journal without reopening it or granting runtime admission."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol

from custos.core.execution_events import ExecutionEventStore
from custos.core.local_runtime_state import (
    BoundRuntimeStore,
    RuntimeStorageBindings,
    RuntimeStoreIdentity,
)
from custos.core.runtime_authority import (
    RuntimeAccountIdentity,
    RuntimeFenceStale,
    RuntimeStorageUnavailable,
)
from custos.engines.nautilus.order_evidence import (
    NativeOrderEvidence,
    decode_document,
    journal_identity,
    order_evidence,
    require_client_order_id,
)


class NativeJournalProbe(Protocol):
    def snapshot_json(self) -> str: ...
    def order_evidence_json(self, client_order_id: str) -> str: ...


class PapertradeRuntimeStorageProbe:
    def __init__(
        self,
        *,
        state: ExecutionEventStore,
        journal_path: Path,
        account: RuntimeAccountIdentity,
        adapter_account_id: str,
        adapter_client_id: str,
        native_probe: NativeJournalProbe,
    ) -> None:
        if not isinstance(state, ExecutionEventStore) or not isinstance(
            account, RuntimeAccountIdentity
        ):
            raise RuntimeStorageUnavailable("native storage requires concrete bound identities")
        if (
            type(adapter_account_id) is not str
            or not 1 <= len(adapter_account_id) <= 128
            or not adapter_account_id.isascii()
            or any(ord(char) < 33 or ord(char) == 127 or char == ":" for char in adapter_account_id)
        ):
            raise RuntimeStorageUnavailable("native adapter account identifier is invalid")
        try:
            require_client_order_id(adapter_client_id)
        except Exception:
            raise RuntimeStorageUnavailable("native adapter client identifier is invalid") from None
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._state = state
        self._native = native_probe
        self._account_id = adapter_account_id
        self._client_id = adapter_client_id
        self._journal = Path(journal_path).absolute()
        self._regular(self._journal)
        self._journal = self._journal.resolve()
        if self._journal == state.path:
            raise RuntimeStorageUnavailable("native journal and state paths must differ")
        self._owner = (
            f"{account.chain_id}:{account.exchange}:{account.wallet_address}:{adapter_account_id}"
        )
        self._journal_identity = self._read_native()
        self._bindings = RuntimeStorageBindings(
            BoundRuntimeStore(state.path, state.store_id),
            BoundRuntimeStore(self._journal, self._journal_identity.store_id),
        )
        self(state.path)

    @staticmethod
    def _regular(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise RuntimeStorageUnavailable("bound runtime store is missing or unsafe")

    def _require_process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeFenceStale("native storage probe belongs to another process")

    @property
    def bindings(self) -> RuntimeStorageBindings:
        self._require_process()
        return self._bindings

    def _read_native(self) -> RuntimeStoreIdentity:
        try:
            value = decode_document(self._native.snapshot_json(), maximum_bytes=4096)
            return journal_identity(value, owner=self._owner, path=self._journal)
        except Exception:
            raise RuntimeStorageUnavailable(
                "held native journal identity cannot be verified"
            ) from None

    def _advance_identity(self, current: RuntimeStoreIdentity) -> None:
        previous = self._journal_identity
        if current.store_id != previous.store_id or current.sequence < previous.sequence:
            raise RuntimeStorageUnavailable("held native journal identity regressed or changed")
        self._journal_identity = current

    def order_evidence(self, client_order_id: str) -> NativeOrderEvidence:
        self._require_process()
        with self._lock:
            try:
                require_client_order_id(client_order_id)
                self._regular(self._journal)
                observed = order_evidence(
                    self._native.order_evidence_json(client_order_id),
                    owner=self._owner,
                    path=self._journal,
                    account_id=self._account_id,
                    client_id=self._client_id,
                    client_order_id=client_order_id,
                )
                self._regular(self._journal)
                self._advance_identity(observed.journal)
                return observed
            except Exception:
                raise RuntimeStorageUnavailable(
                    "held native order evidence cannot be verified"
                ) from None

    def __call__(self, path: Path) -> RuntimeStoreIdentity:
        self._require_process()
        with self._lock:
            path = Path(path).absolute()
            self._regular(path)
            if path == self._state.path:
                identity = self._state.identity()
                if identity.store_id != self._state.store_id:
                    raise RuntimeStorageUnavailable("registered state identity differs")
                return identity
            if path != self._journal:
                raise RuntimeStorageUnavailable("runtime store path is not bound")
            current = self._read_native()
            self._advance_identity(current)
            return current

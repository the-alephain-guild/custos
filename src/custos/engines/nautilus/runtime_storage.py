"""Read the held native journal without reopening it or granting runtime admission."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Protocol
from uuid import UUID

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


class NativeJournalProbe(Protocol):
    def snapshot_json(self) -> str: ...


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal identity field")
        result[key] = value
    return result


class PapertradeRuntimeStorageProbe:
    def __init__(
        self,
        *,
        state: ExecutionEventStore,
        journal_path: Path,
        account: RuntimeAccountIdentity,
        adapter_account_id: str,
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
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._state = state
        self._native = native_probe
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
            raw = self._native.snapshot_json()
            if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= 4096:
                raise ValueError("invalid journal identity payload")
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if type(value) is not dict or set(value) != {
                "schema_version",
                "journal_id",
                "revision",
                "owner",
                "canonical_path",
            }:
                raise ValueError("invalid journal identity fields")
            if type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise ValueError("invalid journal identity schema")
            if type(value["revision"]) is not int or not 1 <= value["revision"] <= 2**63 - 1:
                raise ValueError("invalid journal identity revision")
            if type(value["journal_id"]) is not str:
                raise ValueError("invalid journal UUID")
            identity = UUID(value["journal_id"])
            if identity.version != 4 or str(identity) != value["journal_id"]:
                raise ValueError("invalid journal UUID")
            if type(value["canonical_path"]) is not str or value["canonical_path"] != str(
                self._journal
            ):
                raise ValueError("native journal path differs")
            if value["owner"] != self._owner:
                raise ValueError("native journal owner differs")
            return RuntimeStoreIdentity(identity, value["revision"])
        except Exception:
            raise RuntimeStorageUnavailable(
                "held native journal identity cannot be verified"
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
            previous = self._journal_identity
            if current.store_id != previous.store_id or current.sequence < previous.sequence:
                raise RuntimeStorageUnavailable("held native journal identity regressed or changed")
            self._journal_identity = current
            return current

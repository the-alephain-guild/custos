"""Hold the account lock across synchronous preparation and ownership transfer."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from custos.core.local_runtime_state import (
    RuntimeAuthorityRegistry,
    RuntimeLease,
    RuntimeStorageBindings,
    RuntimeStoreIdentity,
)
from custos.core.runtime_authority import (
    RuntimeAccountIdentity,
    RuntimeAuthorityError,
    RuntimeFenceStale,
)


@dataclass(frozen=True)
class PreparedRuntimeStore:
    path: Path
    store_id: UUID
    sequence: int


@dataclass(frozen=True)
class PreparedRuntimeClaim:
    control_registry_id: UUID
    account_scope: str
    state: PreparedRuntimeStore | None
    journal: PreparedRuntimeStore | None
    authorization_epoch: int
    run_id: UUID
    permit_digest: str
    expected_authorization_epoch: int
    control_source: Literal["local", "platform"]
    registration: tuple | None


class RuntimeAccountGuard:
    """A preparation lock, with no authority to authorize or start a client."""

    def __init__(
        self,
        registry: RuntimeAuthorityRegistry,
        account: RuntimeAccountIdentity,
        descriptor: int,
        release: Callable[[int], None],
    ) -> None:
        self._registry = registry
        self._account = account
        self._descriptor = descriptor
        self._release = release
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._preparing_thread: int | None = None
        self._closed = False
        self._transferred = False
        self._checkpoint: PreparedRuntimeClaim | None = None

    def _require_process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeFenceStale("account guard belongs to another process")

    def _require_preparing(self) -> None:
        self._require_process()
        if self._closed or self._transferred:
            raise RuntimeFenceStale("account guard is closed or transferred")
        if self._preparing_thread != threading.get_ident():
            raise RuntimeAuthorityError("account guard requires its preparation context")

    @contextmanager
    def preparation(self):
        self._require_process()
        with self._lock:
            if self._closed or self._transferred or self._preparing_thread is not None:
                raise RuntimeFenceStale("account guard cannot enter preparation again")
            self._preparing_thread = threading.get_ident()
            try:
                yield
            finally:
                self._preparing_thread = None

    def prepare_claim(
        self,
        *,
        run_id: UUID,
        permit_digest: str,
        expected_authorization_epoch: int,
        control_source: Literal["local", "platform"] = "local",
    ) -> PreparedRuntimeClaim:
        self._require_preparing()
        if self._checkpoint is not None:
            raise RuntimeAuthorityError("account guard already has a prepared claim")
        self._checkpoint = self._registry._prepare_claim_locked(
            self._account,
            run_id=run_id,
            permit_digest=permit_digest,
            expected_authorization_epoch=expected_authorization_epoch,
            control_source=control_source,
            lock_descriptor=self._descriptor,
        )
        return self._checkpoint

    def claim(
        self,
        checkpoint: PreparedRuntimeClaim,
        *,
        stores: RuntimeStorageBindings,
        identity_probe: Callable[[Path], RuntimeStoreIdentity] | None = None,
    ) -> RuntimeLease:
        self._require_preparing()
        if checkpoint is not self._checkpoint or checkpoint is None:
            raise RuntimeFenceStale("prepared claim belongs to another guard")
        lease = self._registry._claim_locked(
            self._account,
            checkpoint=checkpoint,
            stores=stores,
            identity_probe=identity_probe,
            lock_descriptor=self._descriptor,
        )
        self._transferred = True
        return lease

    def close(self) -> None:
        self._require_process()
        with self._lock:
            if self._closed or self._transferred:
                return
            if self._preparing_thread is not None:
                raise RuntimeAuthorityError("cannot close an account guard during preparation")
            self._closed = True
            self._release(self._descriptor)

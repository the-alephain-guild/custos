"""Native-free observation ownership; this never grants physical account authority."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.platform_process_identity import PlatformProcessSnapshot
from custos.core.runner_fact import (
    FrozenLedgerCapture,
    RunnerFactAuthority,
    RunnerStateStore,
    _ledger_authority_copy,
)

_ORIGINS = {
    "global": "https://openapi.okx.com",
    "eea": "https://eea.okx.com",
    "us": "https://us.okx.com",
}
_RETIRE_REASONS = frozenset(
    {
        "replaced",
        "paused",
        "stopped",
        "failed",
        "run_exited",
        "desired_changed",
        "storage_changed",
        "uid_changed",
        "observation_invalid",
    }
)


class OkxLedgerBindingError(RuntimeError):
    pass


class OkxLedgerOwnerRetired(OkxLedgerBindingError):
    pass


class LedgerCredentialResolver(Protocol):
    async def resolve(self, verified: Any, credential_scope: object) -> dict[str, Any]: ...


class AccountConfigObservation(Protocol):
    data: Any
    payload_digest: str


def _scope(snapshot: PlatformProcessSnapshot) -> tuple[UUID, str, str]:
    spec = snapshot.context.spec
    if snapshot.context.authority.trading_mode not in {"testnet", "live"} or spec[
        "connector"
    ] not in {"okx", "okx_perpetual"}:
        raise OkxLedgerBindingError("OKX ledger requires a real-mode OKX desired command")
    scope = spec["credential_scope"]
    identifier = UUID(str(scope["scope_id"]))
    digest = scope["scope_digest"]
    region = spec.get("nautilus_config", {}).get("venue", {}).get("region", "global")
    if (
        identifier.int == 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
        or region not in _ORIGINS
    ):
        raise OkxLedgerBindingError("OKX ledger desired scope is invalid")
    return identifier, digest, _ORIGINS[region]


def _storage_key(snapshot: PlatformProcessSnapshot) -> tuple:
    storage = snapshot.storage
    return storage.store_id, storage.path, storage.file_identity


def _uid(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or len(value) > 64
        or value.startswith("0")
    ):
        raise OkxLedgerBindingError("OKX account observation UID is invalid")
    return value


@dataclass(frozen=True, slots=True)
class OkxLedgerAccountBinding:
    owner_epoch: UUID
    resolution_epoch: UUID
    command_fingerprint: str
    scope_id: UUID
    scope_digest: str
    api_origin: str
    uid: str
    main_uid: str
    account_payload_digest: str


class OkxLedgerObservationOwner:
    __slots__ = (
        "_store",
        "_authority",
        "_context_bytes",
        "_scope",
        "_storage",
        "_minimum_seq",
        "_lock",
        "_retired",
        "_uid_pair",
        "_epoch",
        "_resolution_epoch",
        "_fingerprint",
        "_capture_issuer",
        "_invalidated",
        "_held_capture",
    )

    def __init__(self) -> None:
        raise TypeError("OKX ledger owners are created by a current Store resolution")

    @classmethod
    def _create(
        cls,
        store: RunnerStateStore,
        authority: EngineLifecycleAuthority,
        snapshot: PlatformProcessSnapshot,
        resolution_epoch: UUID,
    ) -> Self:
        owner = object.__new__(cls)
        owner._store = store
        owner._authority = authority
        owner._context_bytes = snapshot.context.binding_bytes
        owner._scope = _scope(snapshot)
        owner._storage = _storage_key(snapshot)
        owner._minimum_seq = snapshot.storage.mutation_seq
        owner._lock = threading.RLock()
        owner._retired = None
        owner._uid_pair = None
        owner._epoch = uuid4()
        owner._resolution_epoch = resolution_epoch
        owner._fingerprint = snapshot.context.command_fingerprint
        owner._capture_issuer = object()
        owner._invalidated = threading.Event()
        owner._held_capture = None
        return owner

    @property
    def scope_id(self) -> UUID:
        return self._scope[0]

    @property
    def scope_digest(self) -> str:
        return self._scope[1]

    @property
    def api_origin(self) -> str:
        return self._scope[2]

    @property
    def epoch(self) -> UUID:
        return self._epoch

    @property
    def retired(self) -> bool:
        with self._lock:
            return self._retired is not None or self._invalidated.is_set()

    def retire(self, reason: str) -> None:
        if reason not in _RETIRE_REASONS:
            raise ValueError("unknown OKX ledger retirement reason")
        with self._lock:
            if self._retired is None:
                self._retired = reason
            self._invalidated.set()

    def retire_from_callback(self, reason: str) -> None:
        """Invalidate this epoch without waiting on its owner inside a Store writer."""
        if reason not in _RETIRE_REASONS:
            raise ValueError("unknown OKX ledger retirement reason")
        if self._store.defer_ledger_control_retirement(lambda: self.retire(reason)):
            self._invalidated.set()
            return
        self.retire(reason)

    def assert_current(self) -> None:
        with self._lock:
            if self._retired is not None or self._invalidated.is_set():
                raise OkxLedgerOwnerRetired("OKX ledger observation owner is retired")
            try:
                current = self._store.platform_process_snapshot(self._authority)
                if (
                    current.context.binding_bytes != self._context_bytes
                    or _scope(current) != self._scope
                ):
                    raise OkxLedgerBindingError("OKX ledger current desired differs")
                if (
                    _storage_key(current) != self._storage
                    or current.storage.mutation_seq < self._minimum_seq
                ):
                    raise OkxLedgerBindingError("OKX ledger storage identity regressed")
            except Exception as error:
                self.retire("desired_changed")
                raise OkxLedgerOwnerRetired(
                    "OKX ledger signed Store context is no longer current"
                ) from error
            self._minimum_seq = current.storage.mutation_seq

    def _require_fact_authority(self, authority: RunnerFactAuthority) -> RunnerFactAuthority:
        authority = _ledger_authority_copy(authority)
        context = json.loads(self._context_bytes)
        if (
            authority.tenant_id != context["tenant_id"]
            or str(authority.runner_id) != context["runner_id"]
            or authority.trading_mode != self._authority.trading_mode
            or authority.deployment_instance_id != self._authority.deployment_instance_id
            or authority.deployment_spec_id != self._authority.deployment_spec_id
            or authority.deployment_spec_digest != self._authority.deployment_spec_digest
            or authority.generation != self._authority.generation
            or str(authority.strategy_id) != context["spec"]["strategy_id"]
        ):
            raise OkxLedgerBindingError("ledger fact authority differs from signed desired")
        return authority

    def _require_account_binding(self, binding: OkxLedgerAccountBinding) -> None:
        if not isinstance(binding, OkxLedgerAccountBinding) or (
            binding.owner_epoch != self._epoch
            or binding.resolution_epoch != self._resolution_epoch
            or binding.command_fingerprint != self._fingerprint
            or (binding.scope_id, binding.scope_digest, binding.api_origin) != self._scope
            or self._uid_pair is None
            or (binding.uid, binding.main_uid) != self._uid_pair
        ):
            raise OkxLedgerBindingError("ledger account observation differs from its owner")

    def freeze_group(
        self,
        authority: RunnerFactAuthority,
        account_binding: OkxLedgerAccountBinding,
        raw_content_digest: str,
        batches: Sequence[Sequence[Mapping[str, Any]]],
    ) -> FrozenLedgerCapture:
        with self._lock:
            self.assert_current()
            authority = self._require_fact_authority(authority)
            self._require_account_binding(account_binding)
            return FrozenLedgerCapture._freeze(
                self, authority, account_binding, raw_content_digest, batches
            )

    @contextmanager
    def hold_capture(
        self,
        authority: RunnerFactAuthority,
        capture: FrozenLedgerCapture,
    ) -> Iterator[None]:
        with self._lock:
            if self._retired is not None or self._invalidated.is_set():
                raise OkxLedgerOwnerRetired("OKX ledger observation owner is retired")
            if (
                capture._owner is not self
                or capture._issuer is not self._capture_issuer
                or self._held_capture is not None
                or self._require_fact_authority(authority) != capture.authority
            ):
                raise OkxLedgerBindingError("ledger capture authority differs")
            self._require_account_binding(capture.account_binding)
            self._held_capture = capture
            try:
                yield
            finally:
                self._held_capture = None

    def require_capture_on(
        self,
        authority: RunnerFactAuthority,
        capture: FrozenLedgerCapture,
        current: PlatformProcessSnapshot,
    ) -> None:
        if (
            self._held_capture is not capture
            or self._invalidated.is_set()
            or self._retired is not None
        ):
            raise OkxLedgerOwnerRetired("OKX ledger capture is no longer current")
        if (
            self._require_fact_authority(authority) != capture.authority
            or current.context.binding_bytes != self._context_bytes
            or _scope(current) != self._scope
            or _storage_key(current) != self._storage
            or current.storage.mutation_seq < self._minimum_seq
        ):
            raise OkxLedgerBindingError("ledger capture differs from current transaction")
        self._require_account_binding(capture.account_binding)

    def acknowledge_ledger_commit(
        self,
        capture: FrozenLedgerCapture,
        mutation_seq: int,
    ) -> None:
        if self._held_capture is not capture:
            raise OkxLedgerBindingError("ledger commit requires its held capture")
        self._minimum_seq = max(self._minimum_seq, mutation_seq)

    def observe_account(self, observation: AccountConfigObservation) -> OkxLedgerAccountBinding:
        with self._lock:
            self.assert_current()
            try:
                data = observation.data
                digest = observation.payload_digest
                if (
                    not isinstance(data, list)
                    or len(data) != 1
                    or not isinstance(data[0], Mapping)
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                ):
                    raise OkxLedgerBindingError("OKX account observation is malformed")
                pair = _uid(data[0].get("uid")), _uid(data[0].get("mainUid"))
                if pair[0] != pair[1] or (self._uid_pair is not None and pair != self._uid_pair):
                    raise OkxLedgerBindingError("OKX observed main account changed")
            except Exception as error:
                self.retire("observation_invalid")
                raise OkxLedgerOwnerRetired(
                    "OKX ledger account observation cannot retain its owner"
                ) from error
            self._uid_pair = pair
            return OkxLedgerAccountBinding(
                self._epoch,
                self._resolution_epoch,
                self._fingerprint,
                self.scope_id,
                self.scope_digest,
                self.api_origin,
                *pair,
                digest,
            )


@dataclass(frozen=True, slots=True)
class ResolvedOkxLedgerOwner:
    owner: OkxLedgerObservationOwner
    credential: dict[str, Any] = field(repr=False)


class OkxLedgerOwnerSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resolution_epoch = None
        self._owner = None

    def retire(self, reason: str) -> None:
        if reason not in _RETIRE_REASONS:
            raise ValueError("unknown OKX ledger retirement reason")
        with self._lock:
            self._resolution_epoch = None
            if self._owner is not None:
                self._owner.retire(reason)
            self._owner = None

    @property
    def current(self) -> OkxLedgerObservationOwner:
        with self._lock:
            if self._owner is None or self._owner.retired:
                raise OkxLedgerOwnerRetired("OKX ledger slot has no current owner")
            return self._owner

    async def replace(
        self,
        store: RunnerStateStore,
        authority: EngineLifecycleAuthority,
        resolver: LedgerCredentialResolver,
    ) -> ResolvedOkxLedgerOwner:
        with self._lock:
            if self._owner is not None:
                self._owner.retire("replaced")
            self._owner = None
            epoch = uuid4()
            self._resolution_epoch = epoch
        try:
            if not isinstance(store, RunnerStateStore):
                raise OkxLedgerBindingError("OKX ledger requires the actual RunnerStateStore")
            before = await asyncio.to_thread(store.platform_process_snapshot, authority)
            scope_id, scope_digest, _ = _scope(before)
            desired = await store.load_durable_desired_command(authority.deployment_instance_id)
            if desired.command_fingerprint != before.context.command_fingerprint:
                raise OkxLedgerBindingError("OKX ledger desired changed before resolution")
            scope = desired.command.to_runtime_spec().credential_scope
            if (scope.scope_id, scope.scope_digest) != (scope_id, scope_digest):
                raise OkxLedgerBindingError(
                    "OKX ledger resolution scope differs from current desired"
                )
            credential = await resolver.resolve(desired, scope)
            if (
                not isinstance(credential, dict)
                or credential.get("scope_digest") != scope_digest
                or credential.get("permission_scope") != "trade_no_withdraw"
                or ("scope_id" in credential and str(credential["scope_id"]) != str(scope_id))
            ):
                raise OkxLedgerBindingError("OKX ledger resolved credential metadata differs")
            after = await asyncio.to_thread(store.platform_process_snapshot, authority)
            if (
                after.context.binding_bytes != before.context.binding_bytes
                or _scope(after) != _scope(before)
                or _storage_key(after) != _storage_key(before)
                or after.storage.mutation_seq < before.storage.mutation_seq
            ):
                raise OkxLedgerBindingError("OKX ledger Store changed during resolution")
            owner = OkxLedgerObservationOwner._create(store, authority, after, epoch)
            with self._lock:
                if self._resolution_epoch != epoch:
                    owner.retire("replaced")
                    raise OkxLedgerOwnerRetired("OKX ledger resolution was superseded")
                self._owner = owner
                self._resolution_epoch = None
            return ResolvedOkxLedgerOwner(owner, credential)
        except BaseException:
            with self._lock:
                if self._resolution_epoch == epoch:
                    self._resolution_epoch = None
            raise

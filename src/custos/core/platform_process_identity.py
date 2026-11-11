"""Closed platform ownership identities, independent of execution permission."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority

PLATFORM_STATE_TABLES = frozenset(
    """runner_fact_stream runner_fact_atomic_group runner_fact_seen_event runner_fact_outbox
    runner_fact_last_equity runner_fact_owed_month_close runner_terminal_valuation_fact
    runner_fact_stream_seal runner_fact_publication_receipt strategy_signal_stream
    strategy_signal_seen runner_order_identity runner_order_identity_release
    strategy_signal_outbox strategy_signal_publication_receipt runner_state_schema
    desired_deployments runner_opening_gate runner_opening_release_inbox
    runner_opening_acknowledgements runner_opening_permissions runner_opening_trade_events
    runner_opening_lifecycle
    artifact_activation runner_cap_policy
    runner_cap_policy_head runner_risk_latch runner_breaker_state applied_deployments
    command_outcomes runner_last_running_generation runner_stop_terminal runner_stop_pending_reap
    kept_command_unbound_notice command_in_progress_lease order_reservation
    runner_exposure_checkpoint runner_position_exposure_lot runner_order_reservation_event
    platform_process_bindings platform_process_heads""".split()
)


def platform_counter_triggers() -> dict[str, str]:
    """Return the closed trigger definition used by both installation and verification."""
    triggers = {
        "platform_store_identity_no_reinsert": """CREATE TRIGGER platform_store_identity_no_reinsert
            BEFORE INSERT ON runner_store_identity
            WHEN EXISTS(SELECT 1 FROM runner_store_identity)
            BEGIN SELECT RAISE(ABORT,'platform store identity is immutable'); END""",
        "platform_store_identity_immutable": """CREATE TRIGGER platform_store_identity_immutable
            BEFORE UPDATE ON runner_store_identity
            WHEN NEW.singleton<>OLD.singleton OR NEW.store_id<>OLD.store_id
              OR NEW.schema_revision<>OLD.schema_revision
              OR typeof(NEW.mutation_seq)<>'integer' OR NEW.mutation_seq<>OLD.mutation_seq+1
            BEGIN SELECT RAISE(ABORT,'platform store identity is immutable'); END""",
        "platform_store_identity_no_delete": """CREATE TRIGGER platform_store_identity_no_delete
            BEFORE DELETE ON runner_store_identity
            BEGIN SELECT RAISE(ABORT,'platform store identity is immutable'); END""",
    }
    for operation in ("UPDATE", "DELETE"):
        name = f"platform_process_bindings_no_{operation.lower()}"
        triggers[name] = (
            f"CREATE TRIGGER {name} BEFORE {operation} ON platform_process_bindings "
            "BEGIN SELECT RAISE(ABORT,'platform process binding is append-only'); END"
        )
    triggers["platform_process_bindings_no_replace"] = (
        "CREATE TRIGGER platform_process_bindings_no_replace BEFORE INSERT ON platform_process_bindings "
        "WHEN EXISTS(SELECT 1 FROM platform_process_bindings WHERE owner_id=NEW.owner_id) "
        "BEGIN SELECT RAISE(ABORT,'platform process binding is append-only'); END"
    )
    for table in sorted(PLATFORM_STATE_TABLES):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"platform_mutation_{table}_{operation.lower()}"
            triggers[name] = (
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} "
                "BEGIN UPDATE runner_store_identity SET mutation_seq=mutation_seq+1 "
                "WHERE singleton=1; END"
            )
    return triggers


PlatformFenceReason = Literal[
    "unsupported_storage",
    "unsupported_account_identity",
    "platform_account_occupied",
    "platform_account_identity_invalid",
    "platform_account_aliases_differ",
    "platform_adapter_identity_differs",
    "platform_alias_lock_replaced",
    "platform_alias_lock_unavailable",
    "platform_desired_invalid",
    "platform_desired_stale",
    "platform_desired_not_running",
    "platform_epoch_exhausted",
    "platform_owner_stale",
    "platform_owner_still_held",
    "platform_registry_already_exists",
    "platform_registry_binding_invalid",
    "platform_registry_continuity_regressed",
    "platform_registry_process_stale",
    "platform_registry_replaced",
    "platform_registry_schema_differs",
    "platform_registry_storage_unavailable",
    "platform_registry_storage_unsafe",
    "platform_registry_unavailable",
    "platform_storage_binding_differs",
    "platform_storage_continuity_regressed",
    "platform_storage_identity_invalid",
    "platform_storage_identity_missing",
    "platform_storage_replaced",
    "platform_storage_schema_differs",
    "platform_storage_unavailable",
    "platform_storage_unregistered",
]


class PlatformFenceError(RuntimeError):
    """A platform ownership or storage requirement cannot be proven."""

    def __init__(self, reason_code: PlatformFenceReason) -> None:
        if reason_code not in get_args(PlatformFenceReason):
            raise ValueError("unknown platform fence reason")
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class PlatformStorageSnapshot:
    path: Path
    store_id: UUID
    mutation_seq: int
    file_identity: tuple[int, int]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.store_id, UUID)
            or self.store_id.int == 0
            or not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or type(self.mutation_seq) is not int
            or not 0 <= self.mutation_seq < 2**63
            or not isinstance(self.file_identity, tuple)
            or len(self.file_identity) != 2
            or any(type(value) is not int or value < 0 for value in self.file_identity)
        ):
            raise PlatformFenceError("platform_storage_identity_invalid")


def canonical_platform_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class PlatformProcessContext:
    tenant_id: str
    runner_id: UUID
    authority: EngineLifecycleAuthority
    command_fingerprint: str
    _spec_bytes: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority, EngineLifecycleAuthority)
            or not isinstance(self.runner_id, UUID)
            or self.runner_id.int == 0
            or not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or not isinstance(self.command_fingerprint, str)
            or re.fullmatch("[0-9a-f]{64}", self.command_fingerprint) is None
            or type(self._spec_bytes) is not bytes
        ):
            raise PlatformFenceError("platform_desired_invalid")
        try:
            value = json.loads(self._spec_bytes)
        except (ValueError, UnicodeError) as error:
            raise PlatformFenceError("platform_desired_invalid") from error
        if not isinstance(value, dict) or canonical_platform_bytes(value) != self._spec_bytes:
            raise PlatformFenceError("platform_desired_invalid")

    @property
    def spec(self) -> dict:
        return json.loads(self._spec_bytes)

    @property
    def binding_bytes(self) -> bytes:
        return canonical_platform_bytes(
            {
                "tenant_id": self.tenant_id,
                "runner_id": str(self.runner_id),
                "command_fingerprint": self.command_fingerprint,
                "spec": self.spec,
            }
        )

    @classmethod
    def from_durable_desired(cls, desired) -> PlatformProcessContext:
        from custos.core.runner_fact import DurableDesiredCommand

        if (
            not isinstance(desired, DurableDesiredCommand)
            or desired.command.lifecycle_state != "running"
        ):
            raise PlatformFenceError("platform_desired_not_running")
        command = desired.command
        return cls(
            command.tenant_id,
            command.runner_id,
            EngineLifecycleAuthority.from_verified_command(desired),
            desired.command_fingerprint,
            canonical_platform_bytes(command.to_runtime_spec().model_dump(mode="json")),
        )


@dataclass(frozen=True, order=True, slots=True)
class PlatformAccountAlias:
    trading_mode: str
    kind: str
    value: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_platform_bytes(
                {
                    "domain": "custos.platform-process-alias.v1",
                    "trading_mode": self.trading_mode,
                    "kind": self.kind,
                    "value": self.value,
                }
            )
        ).hexdigest()


def signed_account_aliases(context: PlatformProcessContext) -> tuple[PlatformAccountAlias, ...]:
    mode = context.authority.trading_mode
    if mode == "sandbox":
        return (
            PlatformAccountAlias(
                mode, "platform_instance", str(context.authority.deployment_instance_id)
            ),
        )
    spec = context.spec
    if spec["connector"] not in {"sodex", "sodex_perpetual"}:
        raise PlatformFenceError("unsupported_account_identity")
    options = spec.get("nautilus_config", {}).get("venue", {})
    raw_wallet = options.get("wallet_address")
    if not isinstance(raw_wallet, str):
        raise PlatformFenceError("platform_account_identity_invalid")
    wallet = raw_wallet.strip(" ").lower()
    wallet = wallet.removeprefix("0x")
    if re.fullmatch("[0-9a-f]{40}", wallet) is None or int(wallet, 16) == 0:
        raise PlatformFenceError("platform_account_identity_invalid")
    account = options.get("sodex_account_id")
    if isinstance(account, str) and re.fullmatch("0|[1-9][0-9]*", account):
        account = int(account)
    if type(account) is not int or not 0 < account < 2**64:
        raise PlatformFenceError("platform_account_identity_invalid")
    scope = UUID(str(spec["credential_scope"]["scope_id"]))
    if scope.int == 0:
        raise PlatformFenceError("platform_account_identity_invalid")
    # This is the CONTROL resolved spec venue alias, not the native ordinal account id.
    venue = str(options.get("name", spec["connector"])).strip(" ").lower()
    if re.fullmatch("[a-z0-9_-]+", venue) is None:
        raise PlatformFenceError("platform_account_identity_invalid")
    return tuple(
        sorted(
            (
                PlatformAccountAlias(mode, "credential_scope", str(scope)),
                PlatformAccountAlias(mode, "venue_wallet_address", "0x" + wallet),
                PlatformAccountAlias(mode, "venue_sodex_account_id", f"{venue}:{account}"),
            )
        )
    )


@dataclass(frozen=True, slots=True, init=False)
class PlatformAccountAliases:
    context: PlatformProcessContext
    aliases: tuple[PlatformAccountAlias, ...]
    adapter_binding: bytes

    def __init__(self, *args, **kwargs) -> None:
        raise PlatformFenceError("platform_adapter_identity_differs")

    @classmethod
    def _from_native_resolution(cls, context, aliases, adapter_binding):
        """Internal construction after the native resolver checks the real config."""
        result = object.__new__(cls)
        object.__setattr__(result, "context", context)
        object.__setattr__(result, "aliases", aliases)
        object.__setattr__(result, "adapter_binding", adapter_binding)
        result.__post_init__()
        return result

    def __post_init__(self) -> None:
        if (
            not isinstance(self.context, PlatformProcessContext)
            or type(self.aliases) is not tuple
            or self.aliases != signed_account_aliases(self.context)
        ):
            raise PlatformFenceError("platform_account_aliases_differ")
        if type(self.adapter_binding) is not bytes:
            raise PlatformFenceError("platform_adapter_identity_differs")
        try:
            value = json.loads(self.adapter_binding)
        except (ValueError, UnicodeError) as error:
            raise PlatformFenceError("platform_adapter_identity_differs") from error
        if not isinstance(value, dict) or canonical_platform_bytes(value) != self.adapter_binding:
            raise PlatformFenceError("platform_adapter_identity_differs")
        connector = self.context.spec["connector"]
        if value.get("connector") != connector:
            raise PlatformFenceError("platform_adapter_identity_differs")
        if self.context.authority.trading_mode == "sandbox":
            if (
                set(value) != {"connector", "venue", "account_id", "account_type"}
                or not isinstance(value["venue"], str)
                or not value["venue"]
                or not isinstance(value["account_id"], str)
                or value["account_id"] in {"", "None"}
                or value["account_type"] not in {"CASH", "MARGIN", "BETTING", "WALLET"}
            ):
                raise PlatformFenceError("platform_adapter_identity_differs")
        else:
            expected_network = (
                "Network.TESTNET"
                if self.context.authority.trading_mode == "testnet"
                else "Network.MAINNET"
            )
            expected_market = "Market.PERPS" if connector == "sodex_perpetual" else "Market.SPOT"
            wallet = next(a.value for a in self.aliases if a.kind == "venue_wallet_address")
            account = int(self.context.spec["nautilus_config"]["venue"]["sodex_account_id"])
            if (
                set(value) != {"connector", "network", "market", "wallet_address", "account_id"}
                or value["network"] != expected_network
                or value["market"] != expected_market
                or value["wallet_address"] != wallet
                or type(value["account_id"]) is not int
                or value["account_id"] != account
            ):
                raise PlatformFenceError("platform_adapter_identity_differs")

    @property
    def aliases_bytes(self) -> bytes:
        return canonical_platform_bytes(
            [
                {"trading_mode": a.trading_mode, "kind": a.kind, "value": a.value}
                for a in self.aliases
            ]
        )


@dataclass(frozen=True, slots=True)
class PlatformProcessSnapshot:
    storage: PlatformStorageSnapshot
    context: PlatformProcessContext
    mirrors: tuple[PlatformProcessBinding, ...] = ()
    current_owner_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PlatformProcessBinding:
    owner_id: UUID
    registry_id: UUID
    process_epoch: int
    aliases_bytes: bytes
    context_bytes: bytes
    adapter_binding: bytes


@dataclass(frozen=True, slots=True)
class PlatformAuthorityBinding:
    authority_root: Path
    registry_id: UUID
    host_scope_id: UUID
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_root, Path)
            or not self.authority_root.is_absolute()
            or ".." in self.authority_root.parts
            or not isinstance(self.registry_id, UUID)
            or self.registry_id.int == 0
            or not isinstance(self.host_scope_id, UUID)
            or self.host_scope_id.int == 0
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise PlatformFenceError("platform_registry_binding_invalid")

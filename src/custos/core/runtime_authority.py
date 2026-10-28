"""Internal account identity and process fencing, without runtime admission."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

PAPERTRADE_CHAIN_ID = 999
PAPERTRADE_EXCHANGE = "0x6cd5661646289fb6e65ea5c032310fded797d0a2"


class RuntimeAuthorityError(ValueError):
    """Account ownership or recovery evidence cannot be accepted."""


class RuntimeAccountOccupied(RuntimeAuthorityError):
    """Another process holds the account's canonical lock."""


class RuntimeFenceStale(RuntimeAuthorityError):
    """An action belongs to an old or retired process owner."""


class RuntimeStorageUnavailable(RuntimeAuthorityError):
    """Registered storage is missing, replaced, or behind its checkpoint."""


class RuntimeRiskPending(RuntimeAuthorityError):
    """A recorded, retryable risk refusal happened before any venue dispatch."""


def _address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise RuntimeAuthorityError("account address must be a 20-byte EVM address")
    normalized = value.lower()
    if normalized == "0x" + "0" * 40:
        raise RuntimeAuthorityError("account address must not be zero")
    return normalized


@dataclass(frozen=True)
class RuntimeAccountIdentity:
    connector: str
    trading_mode: str
    chain_id: int
    exchange: str
    wallet_address: str

    def __post_init__(self) -> None:
        if self.connector != "papertrade" or self.trading_mode != "live":
            raise RuntimeAuthorityError("only the Papertrade live deployment is supported")
        if type(self.chain_id) is not int or self.chain_id != PAPERTRADE_CHAIN_ID:
            raise RuntimeAuthorityError("account chain differs from the supported adapter")
        object.__setattr__(self, "exchange", _address(self.exchange))
        object.__setattr__(self, "wallet_address", _address(self.wallet_address))
        if self.exchange != PAPERTRADE_EXCHANGE:
            raise RuntimeAuthorityError("account exchange differs from the supported adapter")

    @classmethod
    def from_papertrade_adapter(
        cls,
        *,
        wallet_address: str,
        adapter_chain_id: int,
        adapter_exchange: str,
        declared_chain_id: int,
        declared_exchange: str,
        trading_mode: str,
    ) -> RuntimeAccountIdentity:
        """Compare declarations with the trusted adapter binding supplied by the kernel."""
        if (
            type(adapter_chain_id) is not int
            or type(declared_chain_id) is not int
            or adapter_chain_id != PAPERTRADE_CHAIN_ID
            or declared_chain_id != adapter_chain_id
            or _address(adapter_exchange) != PAPERTRADE_EXCHANGE
            or _address(declared_exchange) != _address(adapter_exchange)
        ):
            raise RuntimeAuthorityError("declared account differs from actual adapter deployment")
        return cls("papertrade", trading_mode, adapter_chain_id, adapter_exchange, wallet_address)

    @property
    def scope_digest(self) -> str:
        value = [
            self.connector,
            self.trading_mode,
            self.chain_id,
            self.exchange,
            self.wallet_address,
        ]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class RuntimeFence:
    account_scope: str
    run_id: UUID
    control_source: Literal["local", "platform"]
    authorization_epoch: int
    process_generation: int
    owner_id: UUID

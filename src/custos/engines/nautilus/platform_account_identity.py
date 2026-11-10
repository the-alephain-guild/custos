"""Bind signed platform account aliases to concrete native execution configuration."""

from __future__ import annotations

import asyncio

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.platform_process_fence import PlatformProcessLease, PlatformProcessRegistry
from custos.core.platform_process_identity import (
    PlatformAccountAliases,
    PlatformFenceError,
    PlatformProcessContext,
    canonical_platform_bytes,
    signed_account_aliases,
)
from custos.core.runner_fact import RunnerStateStore
from custos.engines.nautilus.venues import venue_for_connector


def resolve_platform_account_identity(
    context: PlatformProcessContext, exec_config
) -> PlatformAccountAliases:
    """Resolve the entire alias set; a credential scope alone is not a physical identity."""
    spec = context.spec
    connector = spec["connector"]
    aliases = signed_account_aliases(context)
    if context.authority.trading_mode == "sandbox":
        from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig

        if not isinstance(exec_config, SandboxExecutionClientConfig) or str(
            exec_config.venue
        ) != venue_for_connector(connector):
            raise PlatformFenceError("platform_adapter_identity_differs")
        binding = {
            "connector": connector,
            "venue": str(exec_config.venue),
            "account_id": str(exec_config.account_id),
            "account_type": str(exec_config.account_type),
        }
    else:
        from nautilus_trader.adapters.sodex import Market, Network, SodexExecClientConfig

        options = spec["nautilus_config"]["venue"]
        network = (
            Network.TESTNET if context.authority.trading_mode == "testnet" else Network.MAINNET
        )
        market = Market.PERPS if connector == "sodex_perpetual" else Market.SPOT
        wallet = next(alias.value for alias in aliases if alias.kind == "venue_wallet_address")
        if not isinstance(exec_config, SodexExecClientConfig):
            raise PlatformFenceError("platform_adapter_identity_differs")
        try:
            actual_wallet = exec_config.wallet_address
            actual_account = exec_config.account_id
        except AttributeError as error:
            raise PlatformFenceError("unsupported_account_identity") from error
        if (
            exec_config.network != network
            or exec_config.market != market
            or not isinstance(actual_wallet, str)
            or actual_wallet.lower() != wallet
            or actual_account != int(options["sodex_account_id"])
        ):
            raise PlatformFenceError("platform_adapter_identity_differs")
        binding = {
            "connector": connector,
            "network": str(network),
            "market": str(market),
            "wallet_address": wallet,
            "account_id": actual_account,
        }
    return PlatformAccountAliases._from_native_resolution(
        context, aliases, canonical_platform_bytes(binding)
    )


class PlatformExecutionProcessFence:
    """Acquire the registered process lease for the host's concrete account."""

    def __init__(self, registry: PlatformProcessRegistry, state_store: RunnerStateStore) -> None:
        self._registry = registry
        self._state_store = state_store

    async def claim_closed(
        self, authority: EngineLifecycleAuthority, exec_config
    ) -> PlatformProcessLease:
        snapshot = await asyncio.to_thread(self._state_store.platform_process_snapshot, authority)
        account = resolve_platform_account_identity(snapshot.context, exec_config)
        return await self._registry.claim_closed_async(self._state_store, account)

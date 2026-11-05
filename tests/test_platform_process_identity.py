"""Platform account aliases come from signed desired and actual native configuration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.platform_process_identity import PlatformFenceError
from tests.test_runner_fact_store import _runner_fact_store, _verified_command


async def test_actual_sandbox_config_is_bound_to_a_stable_signed_instance_alias(tmp_path):
    pytest.importorskip("nautilus_trader")
    _, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    snapshot = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    )
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    config = build_exec_client_config_sandbox(snapshot.context.spec, {}, ["1 USDT"])
    account = resolve_platform_account_identity(snapshot.context, config)
    assert [(alias.trading_mode, alias.kind, alias.value) for alias in account.aliases] == [
        ("sandbox", "platform_instance", str(verified.command.deployment_instance_id))
    ]
    spec = snapshot.context.spec
    spec["connector"] = "changed"
    assert snapshot.context.spec["connector"] != "changed"


async def test_platform_context_refuses_canonical_command_detached_from_verified_exact_bytes(
    tmp_path,
):
    outbox, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    with sqlite3.connect(outbox.path) as connection:
        value = json.loads(
            connection.execute("SELECT canonical_command FROM desired_deployments").fetchone()[0]
        )
        value["issued_at"] = "2026-11-05T00:00:00Z"
        assert value["issued_at"] != verified.command.issued_at
        connection.execute(
            "UPDATE desired_deployments SET canonical_command=?", (json.dumps(value),)
        )
    with pytest.raises(PlatformFenceError, match="platform_desired_invalid"):
        store.platform_process_snapshot(EngineLifecycleAuthority.from_verified_command(verified))


async def test_cr_aligned_independent_sodex_aliases_are_canonical_across_tenants(tmp_path):
    from custos.core.platform_process_identity import signed_account_aliases
    from tests.integration.platform_process_fence_worker import signed_process_store

    scope = str(uuid4())
    options = {
        "trading_mode": "testnet",
        "connector": "sodex",
        "scope_id": scope,
        "wallet_address": "0x" + "AB" * 20,
        "sodex_account_id": "100",
    }
    store, verified = await signed_process_store(tmp_path / "first.sqlite3", options)
    first = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    ).context
    store, verified = await signed_process_store(
        tmp_path / "second.sqlite3",
        {
            **options,
            "tenant_id": "other_tenant",
            "runner_id": str(uuid4()),
            "instance_id": str(uuid4()),
            "wallet_address": "0x" + "ab" * 20,
            "sodex_account_id": 100,
        },
    )
    second = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    ).context
    aliases = signed_account_aliases(first)
    assert [(alias.trading_mode, alias.kind, alias.value) for alias in aliases] == [
        ("testnet", "credential_scope", scope),
        ("testnet", "venue_sodex_account_id", "sodex:100"),
        ("testnet", "venue_wallet_address", "0x" + "ab" * 20),
    ]
    assert signed_account_aliases(second) == aliases
    assert [alias.digest for alias in aliases] == [
        alias.digest for alias in signed_account_aliases(second)
    ]


async def test_actual_sodex_config_requires_public_native_account_getters(tmp_path):
    pytest.importorskip("nautilus_trader.adapters.sodex")
    from nautilus_trader.adapters.sodex import SodexExecClientConfig

    from tests.integration.platform_process_fence_worker import account_for, signed_process_store

    store, verified = await signed_process_store(
        tmp_path / "runner.sqlite3",
        {
            "trading_mode": "testnet",
            "connector": "sodex",
            "scope_id": str(uuid4()),
            "wallet_address": "0x" + "11" * 20,
            "sodex_account_id": 100,
        },
    )
    if hasattr(SodexExecClientConfig, "wallet_address") and hasattr(
        SodexExecClientConfig, "account_id"
    ):
        assert len(account_for(store, verified).aliases) == 3
    else:
        with pytest.raises(PlatformFenceError, match="unsupported_account_identity"):
            account_for(store, verified)


@pytest.mark.parametrize(
    "mutation",
    [
        "network",
        "market",
        "wallet",
        "account",
        "missing_wallet",
        "missing_account",
        "zero_account",
        "invalid_wallet",
        "wrong_type",
    ],
)
async def test_actual_sodex_config_must_match_the_signed_physical_account(tmp_path, mutation):
    pytest.importorskip("nautilus_trader.adapters.sodex")
    from nautilus_trader.adapters.sodex import (
        Market,
        Network,
        SodexDataClientConfig,
        SodexExecClientConfig,
    )

    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from tests.integration.platform_process_fence_worker import signed_process_store

    if not hasattr(SodexExecClientConfig, "wallet_address") or not hasattr(
        SodexExecClientConfig, "account_id"
    ):
        pytest.skip("installed native ABI has no public SoDEX account identity getters")
    wallet = "0x" + "11" * 20
    store, verified = await signed_process_store(
        tmp_path / "runner.sqlite3",
        {
            "trading_mode": "testnet",
            "connector": "sodex",
            "scope_id": str(uuid4()),
            "wallet_address": wallet,
            "sodex_account_id": 100,
        },
    )
    context = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    ).context
    values = {
        "network": Network.TESTNET,
        "market": Market.SPOT,
        "wallet_address": wallet,
        "account_id": 100,
    }
    field, replacement = {
        "network": ("network", Network.MAINNET),
        "market": ("market", Market.PERPS),
        "wallet": ("wallet_address", "0x" + "22" * 20),
        "account": ("account_id", 101),
        "missing_wallet": ("wallet_address", None),
        "missing_account": ("account_id", None),
        "zero_account": ("account_id", 0),
        "invalid_wallet": ("wallet_address", "0x" + "11" * 19),
        "wrong_type": ("account_id", 100),
    }[mutation]
    values[field] = replacement
    config = (
        SodexDataClientConfig() if mutation == "wrong_type" else SodexExecClientConfig(**values)
    )
    with pytest.raises(PlatformFenceError, match="platform_adapter_identity_differs"):
        resolve_platform_account_identity(context, config)


@pytest.mark.parametrize("mode", ["testnet", "live"])
@pytest.mark.parametrize("connector", ["sodex", "sodex_perpetual"])
async def test_real_native_account_mapping_preserves_network_and_live_admission(
    tmp_path, mode, connector
):
    pytest.importorskip("nautilus_trader.adapters.sodex")
    from nautilus_trader.adapters.sodex import Market, Network, SodexExecClientConfig

    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from tests.integration.platform_process_fence_worker import signed_process_store

    wallet = "0x" + "AB" * 20
    store, verified = await signed_process_store(
        tmp_path / "runner.sqlite3",
        {
            "trading_mode": mode,
            "connector": connector,
            "scope_id": str(uuid4()),
            "wallet_address": wallet,
            "sodex_account_id": 100,
        },
    )
    authority = EngineLifecycleAuthority.from_verified_command(verified)
    if mode == "live":
        # The actual signed fixture has no promotion evidence. Keep the existing
        # live admission guard rather than inventing a promotion for a getter test.
        with pytest.raises(PlatformFenceError, match="platform_desired_invalid") as rejected:
            store.platform_process_snapshot(authority)
        assert "control-plane-signed promotion evidence" in str(rejected.value.__cause__)
        return
    if not hasattr(SodexExecClientConfig, "wallet_address") or not hasattr(
        SodexExecClientConfig, "account_id"
    ):
        pytest.skip("installed native ABI has no public SoDEX account identity getters")
    context = store.platform_process_snapshot(authority).context
    network = Network.TESTNET if mode == "testnet" else Network.MAINNET
    market = Market.SPOT if connector == "sodex" else Market.PERPS
    config = SodexExecClientConfig(
        network=network, market=market, wallet_address=wallet, account_id=100
    )
    account = resolve_platform_account_identity(context, config)
    assert config.wallet_address == wallet
    assert config.account_id == 100
    binding = json.loads(account.adapter_binding)
    assert binding == {
        "connector": connector,
        "network": str(network),
        "market": str(market),
        "wallet_address": wallet.lower(),
        "account_id": 100,
    }
    assert len(account.aliases) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"authority_root": "relative"},
        {"registry_id": None},
        {"host_scope_id": UUID(int=0)},
        {"schema_version": True},
        {"schema_version": 2},
    ],
)
def test_trusted_registry_binding_rejects_invalid_typed_fields(tmp_path, changes):
    from custos.core.platform_process_identity import PlatformAuthorityBinding

    with pytest.raises(PlatformFenceError, match="platform_registry_binding_invalid"):
        PlatformAuthorityBinding(
            **{
                "authority_root": tmp_path,
                "registry_id": uuid4(),
                "host_scope_id": uuid4(),
                **changes,
            }
        )


async def test_identity_does_not_accept_mutable_aliases_or_byte_buffers(tmp_path):
    from tests.test_platform_process_fence import signed_sandbox_account

    _, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    with pytest.raises(PlatformFenceError):
        replace(account, aliases=list(account.aliases))
    with pytest.raises(PlatformFenceError):
        replace(account, adapter_binding=bytearray(account.adapter_binding))
    with pytest.raises(PlatformFenceError):
        replace(account.context, _spec_bytes=bytearray(account.context._spec_bytes))

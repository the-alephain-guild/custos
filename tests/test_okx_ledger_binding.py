"""Ledger ownership starts from actual signed durable desired, not a caller spec."""

import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from custos.contracts.deployment import (
    DOMAIN_EVENT_ENCODING,
    DOMAIN_EVENT_SIGNATURE_PROFILE,
    canonical_deployment_spec_digest,
)
from custos.core import okx_ledger_binding
from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.runner_command_intake import CrucibleRunnerCommandAuthenticator
from tests.test_runner_fact_store import (
    COMMAND_SIGNING_KEY,
    RUNNER_ID,
    _encode_base64url,
    _runner_fact_store,
)


def signed_okx_command(*, generation=1, state="running", scope_digest="a" * 64):
    path = (
        Path(__file__).resolve().parents[1]
        / "docs/authority/runner-deployment-command-golden-v1.json"
    )
    event = copy.deepcopy(json.loads(path.read_text())["cases"][0]["event_document"])
    event["event_id"] = str(uuid4())
    event["event_plane"]["trading_mode"] = "testnet"
    event["aggregate_version"] = generation
    payload = event["payload"]
    payload["mode"] = "testnet"
    payload["generation"] = generation
    payload["lifecycle_state"] = state
    if generation > 1:
        event["event_type"] = (
            f"DeploymentInstanceDesiredStateChanged.{payload['runner_id']}.{payload['deployment_instance_id']}"
        )
    spec = payload["deployment_spec"]
    spec["trading_mode"] = "testnet"
    spec["execution_config"]["connector"] = "okx"
    spec["execution_config"]["sandbox"] = None
    spec["execution_config"]["nautilus_config"] = {"venue": {"region": "global"}}
    spec["execution_channel"]["channel_type"] = "testnet_venue"
    spec["credential_scope"]["scope_digest"] = scope_digest
    spec["venue_source_policy"] = [{"venue": "OKX", "ledger_source": "venue_api"}]
    spec["source_policy_digest"] = hashlib.sha256(
        json.dumps(spec["venue_source_policy"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["deployment_spec_digest"] = canonical_deployment_spec_digest(spec)
    subject = f"crucible.runner.command.v1.acme.{RUNNER_ID}.testnet"
    exact = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    subject_bytes = subject.encode()
    framed = (
        b"CRUCIBLE-DOMAIN-EVENT-V1\0"
        + len(subject_bytes).to_bytes(4, "big")
        + subject_bytes
        + len(exact).to_bytes(8, "big")
        + exact
    )
    envelope = json.dumps(
        {
            "schema_version": 1,
            "signature_profile": DOMAIN_EVENT_SIGNATURE_PROFILE,
            "event_encoding": DOMAIN_EVENT_ENCODING,
            "event_bytes": _encode_base64url(exact),
            "signature_key_id": "crucible-command-key-a",
            "signature": _encode_base64url(COMMAND_SIGNING_KEY.sign(framed)),
        },
        separators=(",", ":"),
    ).encode()
    return CrucibleRunnerCommandAuthenticator(
        expected_tenant_id="acme",
        expected_runner_id=RUNNER_ID,
        allowed_trading_modes=frozenset({"testnet"}),
        signature_keys={"crucible-command-key-a": COMMAND_SIGNING_KEY.public_key()},
    ).verify(subject=subject, signed_envelope_bytes=envelope)


async def running_store(path, **command_options):
    outbox, store = _runner_fact_store(path)
    verified = signed_okx_command(**command_options)
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    return outbox, store, EngineLifecycleAuthority.from_verified_command(verified), verified


class ScopeVault:
    def __init__(self, scope_id, scope_digest):
        self.scope_id = str(scope_id)
        self.scope_digest = scope_digest
        self.calls = []

    def decrypt(self, reference):
        self.calls.append(reference)
        assert reference == self.scope_id
        return {
            "scope_digest": self.scope_digest,
            "permission_scope": "trade_no_withdraw",
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        }


async def test_real_signed_testnet_store_predecessor_is_running_and_scope_bound(tmp_path):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    snapshot = store.platform_process_snapshot(authority)
    assert snapshot.context.command_fingerprint == verified.command_fingerprint
    assert snapshot.context.spec["connector"] == "okx"
    assert snapshot.context.authority.trading_mode == "testnet"
    assert snapshot.context.spec["credential_scope"]["scope_digest"] == "a" * 64


async def test_new_owner_resolves_the_store_scope_and_keeps_only_pure_metadata(tmp_path):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    vault = ScopeVault(scope.scope_id, scope.scope_digest)
    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    resolution = await slot.replace(store, authority, VaultRunnerCredentialResolverV1(vault))
    assert resolution.owner.scope_id == scope.scope_id
    assert resolution.owner.scope_digest == scope.scope_digest
    assert vault.calls == [str(scope.scope_id)]
    assert resolution.credential["api_key"] == "fixture-key"
    assert "fixture-secret" not in repr(resolution.owner)


async def test_account_binding_is_immutable_observation_data_and_same_scope_replacement_retires_old(
    tmp_path,
):
    from dataclasses import FrozenInstanceError

    import pytest

    from custos.engines.nautilus.ledger_http import decode_exact_venue_response

    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    vault = ScopeVault(scope.scope_id, scope.scope_digest)
    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    first = await slot.replace(store, authority, VaultRunnerCredentialResolverV1(vault))
    observation = decode_exact_venue_response(b'{"code":"0","data":[{"uid":"42","mainUid":"42"}]}')
    binding = first.owner.observe_account(observation)
    assert binding.uid == binding.main_uid == "42"
    assert binding.command_fingerprint == verified.command_fingerprint
    assert binding.api_origin == "https://openapi.okx.com"
    with pytest.raises(FrozenInstanceError):
        binding.uid = "99"
    second = await slot.replace(store, authority, VaultRunnerCredentialResolverV1(vault))
    second_binding = second.owner.observe_account(observation)
    assert first.owner.retired and second.owner is slot.current
    assert second_binding.uid == binding.uid
    assert second_binding.owner_epoch != binding.owner_epoch
    assert second_binding.resolution_epoch != binding.resolution_epoch
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        first.owner.assert_current()
    assert "fixture-key" not in repr(second) and "fixture-secret" not in repr(second.owner)


async def test_unrelated_legal_outbox_write_advances_watermark_without_retiring_owner(tmp_path):
    from uuid import uuid4

    from tests.test_runner_fact_store import _runner_authority

    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    owner = (
        await okx_ledger_binding.OkxLedgerOwnerSlot().replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    before = outbox.platform_storage_snapshot()
    await outbox.enqueue(
        _runner_authority(verified),
        store._identity,
        [
            {
                "kind": "heartbeat",
                "event_id": str(uuid4()),
                "status": "online",
                "observed_at": "2026-11-22T00:00:00Z",
            }
        ],
    )
    after = outbox.platform_storage_snapshot()
    assert after.store_id == before.store_id and after.mutation_seq > before.mutation_seq
    owner.assert_current()
    assert not owner.retired

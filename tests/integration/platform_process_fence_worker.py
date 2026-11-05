"""Independent process exercising real signed desired and closed account ownership."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


async def signed_process_store(database: Path, options: dict):
    from custos.contracts.deployment import canonical_deployment_spec_digest
    from custos.core.runner_command_intake import CrucibleRunnerCommandAuthenticator
    from custos.core.runner_fact import RunnerFactOutbox, RunnerStateStore
    from tests.test_runner_fact_store import (
        COMMAND_SIGNING_KEY,
        RUNNER_ID,
        _identity,
        _runner_authority,
        _signed_command_fixture,
    )

    tenant = options.get("tenant_id", "acme")
    runner = UUID(options.get("runner_id", str(RUNNER_ID)))
    mode = options.get("trading_mode", "sandbox")

    def mutate(event):
        payload = event["payload"]
        spec = payload["deployment_spec"]
        instance = options.get("instance_id", payload["deployment_instance_id"])
        generation = options.get("generation", payload["generation"])
        spec.update(tenant_id=tenant, trading_mode=mode, target_runner_id=str(runner))
        if "scope_id" in options:
            spec["credential_scope"]["scope_id"] = options["scope_id"]
        execution = spec["execution_config"]
        execution["connector"] = options.get("connector", execution["connector"])
        if mode != "sandbox":
            execution["nautilus_config"] = {
                "venue": {
                    "wallet_address": options["wallet_address"],
                    "sodex_account_id": options["sodex_account_id"],
                }
            }
        spec["execution_channel"]["channel_type"] = {
            "sandbox": "sandbox_sim_engine",
            "testnet": "testnet_venue",
            "live": "live_venue",
        }[mode]
        payload.update(
            tenant_id=tenant,
            mode=mode,
            runner_id=str(runner),
            deployment_instance_id=instance,
            generation=generation,
            lifecycle_state=options.get("lifecycle_state", "running"),
            deployment_spec_digest=canonical_deployment_spec_digest(spec),
        )
        event.update(
            tenant_id=tenant,
            aggregate_id=instance,
            aggregate_version=generation,
            event_plane={"kind": "mode", "trading_mode": mode},
            event_type=f"DeploymentSpecReadyForRunner.{runner}.{instance}",
        )

    raw, _ = _signed_command_fixture(mutate_event=mutate)
    envelope = json.loads(raw)
    exact = base64.urlsafe_b64decode(
        envelope["event_bytes"] + "=" * (-len(envelope["event_bytes"]) % 4)
    )
    subject = f"crucible.runner.command.v1.{tenant}.{runner}.{mode}"
    encoded = subject.encode()
    preimage = b"CRUCIBLE-DOMAIN-EVENT-V1\0" + len(encoded).to_bytes(4, "big") + encoded
    preimage += len(exact).to_bytes(8, "big") + exact
    envelope["signature"] = (
        base64.urlsafe_b64encode(COMMAND_SIGNING_KEY.sign(preimage)).rstrip(b"=").decode()
    )
    verified = CrucibleRunnerCommandAuthenticator(
        expected_tenant_id=tenant,
        expected_runner_id=runner,
        allowed_trading_modes=frozenset({mode}),
        signature_keys={"crucible-command-key-a": COMMAND_SIGNING_KEY.public_key()},
    ).verify(
        subject=subject, signed_envelope_bytes=json.dumps(envelope, separators=(",", ":")).encode()
    )
    store = RunnerStateStore(
        outbox=RunnerFactOutbox(database),
        identity=_identity(),
        tenant_id=tenant,
        runner_id=runner,
        authority_resolver=_runner_authority,
    )
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    return store, verified


def account_for(store, verified):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity

    context = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    ).context
    if context.authority.trading_mode == "sandbox":
        from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

        config = build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    else:
        from custos.engines.nautilus.venue_sodex import build_exec_client_config_testnet

        config = build_exec_client_config_testnet(
            context.spec, {"api_key": "test-fixture-key", "api_secret": "01" * 32}
        )
    return resolve_platform_account_identity(context, config)


def emit(value: dict) -> None:
    print(json.dumps({"pid": os.getpid(), **value}), flush=True)


async def main() -> int:
    from custos.core.platform_process_fence import PlatformProcessRegistry
    from custos.core.platform_process_identity import PlatformAuthorityBinding, PlatformFenceError

    options = json.loads(sys.argv[1])
    store, verified = await signed_process_store(Path(options["database"]), options)
    registry = PlatformProcessRegistry.open_registered(
        PlatformAuthorityBinding(
            Path(options["authority_root"]),
            UUID(options["registry_id"]),
            UUID(options["host_scope_id"]),
        )
    )
    try:
        account = account_for(store, verified)
        registry.register_store(store)
        if options.get("pause_after"):
            transaction = registry._transaction
            writes = 0

            @contextmanager
            def observe_transaction(*, write=True):
                nonlocal writes
                with transaction(write=write) as connection:
                    yield connection
                if write:
                    writes += 1
                    phase = {1: "pending", 2: "finalize"}.get(writes)
                    if options["pause_after"] == phase:
                        emit({"status": phase})
                        if sys.stdin.readline().strip() != "continue":
                            raise RuntimeError("claim continuation missing")

            registry._transaction = observe_transaction
            record = store._record_platform_process_binding

            def observe_record(*args):
                result = record(*args)
                if options["pause_after"] == "mirror":
                    emit({"status": "mirror"})
                    if sys.stdin.readline().strip() != "continue":
                        raise RuntimeError("claim continuation missing")
                return result

            store._record_platform_process_binding = observe_record
        lease = registry.claim_closed(store, account)
    except PlatformFenceError as error:
        emit({"status": "refused", "reason_code": error.reason_code})
        if options.get("hold_refused"):
            if sys.stdin.readline().strip() != "release":
                raise RuntimeError("refused worker continuation missing") from error
        registry.close()
        if options.get("hold_refused"):
            emit({"status": "refusal_complete"})
        return 0
    emit({"status": lease.phase, "owner_id": str(lease.owner_id), "epoch": lease.process_epoch})
    for command in sys.stdin:
        if command.strip() == "release":
            lease.close()
            registry.close()
            emit({"status": "released"})
            return 0
        if command.strip() == "assert":
            lease.assert_current()
            emit({"status": "current"})
        if command.strip() == "fork":
            child = os.fork()
            if child == 0:
                try:
                    lease.assert_current()
                except PlatformFenceError as error:
                    emit({"status": "fork_refused", "reason_code": error.reason_code})
                    os._exit(0)
                os._exit(1)
            _, status = os.waitpid(child, 0)
            if status != 0:
                raise RuntimeError("fork inherited usable owner")
            emit({"status": "parent_current"})
        if command.strip() == "exec":
            os.execv(
                sys.executable,
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import json,os,sys; print(json.dumps({'status':'exec_ready','pid':os.getpid()}),flush=True); sys.stdin.readline()",
                ],
            )
    lease.close()
    registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Real signed desired-state fixtures whose opening gates remain closed."""

from __future__ import annotations

import base64
import copy
import json
from types import SimpleNamespace

from custos.cli._daemon import _build_runner_safety_boundary_factory
from custos.contracts.deployment import (
    DOMAIN_EVENT_ENCODING,
    DOMAIN_EVENT_SIGNATURE_PROFILE,
    canonical_deployment_spec_digest,
)
from custos.core.runner_command_intake import CrucibleRunnerCommandAuthenticator
from custos.core.runner_fact import RunnerFactOutbox, RunnerStateStore
from tests.cli.test_runner_safety_daemon_composition import _Resolver
from tests.test_runner_fact_store import (
    COMMAND_GOLDEN_PATH,
    COMMAND_SIGNING_KEY,
    RUNNER_ID,
    _identity,
    _runner_authority,
)


async def closed_platform_runtime(database, spec, *, tenant_id="tenant-a"):
    event = copy.deepcopy(json.loads(COMMAND_GOLDEN_PATH.read_text())["cases"][0]["event_document"])
    mode = spec["trading_mode"]
    instance = str(spec["deployment_instance_id"])
    payload = event["payload"]
    canonical = payload["deployment_spec"]
    canonical.update(
        tenant_id=tenant_id,
        trading_mode=mode,
        deployment_spec_id=str(spec["deployment_spec_id"]),
        strategy_id=str(spec["strategy_id"]),
        credential_scope=spec["credential_scope"],
    )
    canonical["artifact_source"]["snapshot"]["definition_id"] = str(spec["strategy_id"])
    canonical["execution_channel"]["channel_type"] = {
        "sandbox": "sandbox_sim_engine",
        "testnet": "testnet_venue",
        "live": "live_venue",
    }[mode]
    for field in (
        "connector",
        "pairs",
        "leverage",
        "strategy_config",
        "sandbox",
        "nautilus_config",
    ):
        if field in spec:
            canonical["execution_config"][field] = spec[field]
    digest = canonical_deployment_spec_digest(canonical)
    spec["deployment_spec_digest"] = digest
    payload.update(
        tenant_id=tenant_id,
        mode=mode,
        deployment_instance_id=instance,
        deployment_spec_id=str(spec["deployment_spec_id"]),
        deployment_spec_digest=digest,
        generation=spec["generation"],
    )
    event.update(
        tenant_id=tenant_id,
        aggregate_id=instance,
        aggregate_version=spec["generation"],
        event_plane={"kind": "mode", "trading_mode": mode},
        event_type=f"DeploymentSpecReadyForRunner.{RUNNER_ID}.{instance}",
    )
    subject = f"crucible.runner.command.v1.{tenant_id}.{RUNNER_ID}.{mode}"
    event_bytes = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    encoded_subject = subject.encode()
    preimage = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(encoded_subject).to_bytes(4, "big"),
            encoded_subject,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )

    def encoded(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    envelope = json.dumps(
        {
            "schema_version": 1,
            "signature_profile": DOMAIN_EVENT_SIGNATURE_PROFILE,
            "event_encoding": DOMAIN_EVENT_ENCODING,
            "event_bytes": encoded(event_bytes),
            "signature_key_id": "crucible-command-key-a",
            "signature": encoded(COMMAND_SIGNING_KEY.sign(preimage)),
        },
        separators=(",", ":"),
    ).encode()
    verified = CrucibleRunnerCommandAuthenticator(
        expected_tenant_id=tenant_id,
        expected_runner_id=RUNNER_ID,
        allowed_trading_modes=frozenset({mode}),
        signature_keys={"crucible-command-key-a": COMMAND_SIGNING_KEY.public_key()},
    ).verify(subject=subject, signed_envelope_bytes=envelope)
    outbox = RunnerFactOutbox(database)
    store = RunnerStateStore(
        outbox=outbox,
        identity=_identity(),
        tenant_id=tenant_id,
        runner_id=RUNNER_ID,
        authority_resolver=_runner_authority,
    )
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    boundaries = {}
    return SimpleNamespace(
        store=store,
        outbox=outbox,
        verified=verified,
        boundaries=boundaries,
        factory=_build_runner_safety_boundary_factory(
            state_store=store,
            safety_policy_resolver=_Resolver(),
            boundaries=boundaries,
        ),
    )

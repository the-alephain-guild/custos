"""Runtime health is a read-only projection of the sole RunnerFact store."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custos.cli.subcommands.health import _health
from custos.core.readiness import ReadinessFile, is_ready_file, read_health_file
from custos.core.runner_fact import RunnerFactOutbox


def _insert_desired(
    connection: sqlite3.Connection,
    *,
    instance_id: str,
    fingerprint: str,
    updated_at_ns: int,
) -> None:
    connection.execute(
        """
        INSERT INTO desired_deployments (
            deployment_instance_id, tenant_id, trading_mode, runner_id,
            deployment_spec_id, deployment_spec_digest, generation,
            command_event_id, exact_subject, command_fingerprint,
            verified_event_bytes_digest, signer_key_id, signature_profile,
            verification_receipt, canonical_command, exact_event_bytes,
            desired_status, quarantine_reason, updated_at_ns
        ) VALUES (?, 'tenant-a', 'sandbox', 'runner-a', ?, ?, 1, ?, ?, ?,
                  ?, 'domain-key', 'ed25519.v1', '{}', '{}', X'7B7D',
                  'running', NULL, ?)
        """,
        (
            instance_id,
            f"spec-{instance_id}",
            "a" * 64,
            f"event-{instance_id}",
            "crucible.runner.command.v1.tenant-a.runner-a.sandbox",
            fingerprint,
            "b" * 64,
            updated_at_ns,
        ),
    )


@pytest.mark.asyncio
async def test_runtime_metrics_project_store_health_without_a_second_journal(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    now_ns = int(now.timestamp() * 1_000_000_000)
    outbox = RunnerFactOutbox(tmp_path / "runner-state.sqlite3")
    with sqlite3.connect(outbox.path) as connection:
        connection.execute(
            """
            INSERT INTO runner_fact_outbox (
                batch_id, stream_key, subject, source_seq_start, source_seq_end,
                payload, created_at, attempts, last_error
            ) VALUES (?, 'stream-a', 'crucible.runner.fact.v1.tenant-a.runner-a.sandbox',
                      1, 1, X'7B7D', ?, 3, 'NoRespondersError')
            """,
            (
                "00000000-0000-4000-8000-000000000001",
                (now - timedelta(seconds=40)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO runner_fact_publication_receipt (
                batch_id, stream_key, subject, source_seq_start, source_seq_end,
                batch_payload_sha256, broker_stream, broker_sequence,
                broker_domain, duplicate, publish_attempts, published_at
            ) VALUES (?, 'stream-b',
                      'crucible.runner.fact.v1.tenant-a.runner-a.sandbox',
                      2, 2, ?, 'CRUCIBLE_RUNNER_FACT_V1', 19,
                      'SIM', 0, 1, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000002",
                "9" * 64,
                (now - timedelta(seconds=3)).isoformat(),
            ),
        )
        _insert_desired(
            connection,
            instance_id="00000000-0000-4000-8000-000000000010",
            fingerprint="c" * 64,
            updated_at_ns=now_ns - 35_000_000_000,
        )
        _insert_desired(
            connection,
            instance_id="00000000-0000-4000-8000-000000000020",
            fingerprint="d" * 64,
            updated_at_ns=now_ns - 20_000_000_000,
        )
        connection.execute(
            """
            INSERT INTO applied_deployments (
                deployment_instance_id, deployment_spec_id, deployment_spec_digest,
                generation, command_fingerprint, engine_handle, observed_status,
                restart_count, quarantine_reason, updated_at_ns
            ) VALUES (?, ?, ?, 1, ?, 'engine-20', 'quarantined', 2,
                      'restart_budget_exhausted', ?)
            """,
            (
                "00000000-0000-4000-8000-000000000020",
                "spec-00000000-0000-4000-8000-000000000020",
                "a" * 64,
                "d" * 64,
                now_ns - 5_000_000_000,
            ),
        )
        connection.execute(
            """
            INSERT INTO command_in_progress_lease (
                deployment_instance_id, delivery_id, generation,
                command_fingerprint, lease_until_ns, restart_count,
                last_reason_code, updated_at_ns
            ) VALUES (?, 'delivery-10', 1, ?, ?, 1, 'engine_starting', ?)
            """,
            (
                "00000000-0000-4000-8000-000000000010",
                "c" * 64,
                now_ns - 1,
                now_ns - 10_000_000_000,
            ),
        )
        connection.execute(
            """
            INSERT INTO command_outcomes (
                outcome_id, delivery_id, tenant_id, trading_mode, runner_id,
                deployment_instance_id, generation, command_fingerprint,
                exact_subject, raw_envelope_digest, outcome, reason_code,
                durable_disposition, lifecycle_batch_id, recorded_at_ns
            ) VALUES ('outcome-1', 'delivery-20', 'tenant-a', 'sandbox', 'runner-a',
                      ?, 1, ?, 'subject', ?, 'quarantined', 'restart_budget',
                      'term', NULL, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000020",
                "d" * 64,
                "e" * 64,
                now_ns - 4_000_000_000,
            ),
        )
        policy_id = "00000000-0000-4000-8000-000000000030"
        connection.execute(
            """
            INSERT INTO runner_cap_policy (
                policy_id, policy_revision, policy_digest, tenant_scope,
                trading_mode, runner_id, settlement_currency, max_order_notional,
                max_notional, effective_at_ns, expires_at_ns, policy_status,
                signer_key_id, signature_profile, exact_subject, fingerprint,
                verified_event_bytes_digest, exact_event_bytes, signed_policy,
                policy_json, consumed_at_ns
            ) VALUES (?, 1, ?, 'tenant-a', 'sandbox', 'runner-a', 'USDT',
                      '10', '100', ?, ?, 'active', 'domain-key', 'ed25519.v1',
                      'policy.subject', ?, ?, X'7B7D', X'7B7D', '{}', ?)
            """,
            (
                policy_id,
                "f" * 64,
                now_ns - 60_000_000_000,
                now_ns - 5_000_000_000,
                "1" * 64,
                "2" * 64,
                now_ns - 60_000_000_000,
            ),
        )
        connection.execute(
            """
            INSERT INTO runner_cap_policy_head (
                tenant_scope, trading_mode, runner_id, policy_id,
                policy_revision, policy_digest, updated_at_ns
            ) VALUES ('tenant-a', 'sandbox', 'runner-a', ?, 1, ?, ?)
            """,
            (policy_id, "f" * 64, now_ns - 60_000_000_000),
        )

    metrics = await outbox.runtime_metrics(now=now)

    assert metrics.sqlite_quick_check == "ok"
    assert metrics.pending_fact_batches == 1
    assert metrics.oldest_pending_fact_age_seconds == 40
    assert metrics.fact_publish_attempts == 3
    assert metrics.published_fact_batches == 1
    assert metrics.last_fact_puback_age_seconds == 3
    assert metrics.desired_deployments == 2
    assert metrics.desired_applied_drift == 1
    assert metrics.oldest_desired_applied_drift_age_seconds == 35
    assert metrics.quarantined_deployments == 1
    assert metrics.restart_count_total == 2
    assert metrics.in_progress_commands == 1
    assert metrics.overdue_in_progress_commands == 1
    assert metrics.command_outcomes == 1
    assert metrics.terminal_command_outcomes == 1
    assert metrics.last_command_outcome_age_seconds == 4
    assert metrics.policy_heads == 1
    assert metrics.expired_policy_heads == 1
    assert metrics.next_policy_expiry_seconds == -5
    assert metrics.database_bytes > 0
    assert metrics.disk_free_bytes > 0


@pytest.mark.asyncio
async def test_readiness_json_exposes_exact_mode_and_runtime_health(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready_path = tmp_path / "runner-ready.json"
    metrics = await RunnerFactOutbox(tmp_path / "runner-state.sqlite3").runtime_metrics()
    readiness = ReadinessFile(
        ready_path,
        tenant_id="tenant-a",
        runner_id="runner-a",
        credential_id="00000000-0000-4000-8000-000000000001",
        credential_version=1,
        credential_valid_until="2099-01-01T00:00:00Z",
        machine_key_id="machine-key-a",
    )
    readiness.mark_ready(
        strategy_id=None,
        nats_connected=True,
        deployment_subscription=True,
        transport_modes={"sandbox": True},
        runtime_metrics=metrics.to_dict(),
    )

    assert is_ready_file(ready_path)
    assert read_health_file(ready_path) is not None
    assert _health(argparse.Namespace(ready_file=ready_path, json=True)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["transport_modes"] == {"sandbox": True}
    assert output["runtime_metrics"]["schema_version"] == (
        "alephain.custos.runner-runtime-metrics.v1"
    )


def test_readiness_rejects_a_hidden_failed_transport_mode(tmp_path: Path) -> None:
    ready_path = tmp_path / "runner-ready.json"
    readiness = ReadinessFile(
        ready_path,
        tenant_id="tenant-a",
        runner_id="runner-a",
        credential_id="00000000-0000-4000-8000-000000000001",
        credential_version=1,
        credential_valid_until="2099-01-01T00:00:00Z",
        machine_key_id="machine-key-a",
    )
    runtime_metrics = {
        "schema_version": "alephain.custos.runner-runtime-metrics.v1",
        "collected_at": "2026-07-23T12:00:00Z",
        "database_bytes": 1,
        "wal_bytes": 0,
        "disk_free_bytes": 1,
        "sqlite_quick_check": "ok",
        "pending_fact_batches": 0,
        "oldest_pending_fact_age_seconds": None,
        "fact_publish_attempts": 0,
        "published_fact_batches": 0,
        "last_fact_puback_age_seconds": None,
        "desired_deployments": 0,
        "desired_applied_drift": 0,
        "oldest_desired_applied_drift_age_seconds": None,
        "quarantined_deployments": 0,
        "restart_count_total": 0,
        "in_progress_commands": 0,
        "overdue_in_progress_commands": 0,
        "command_outcomes": 0,
        "terminal_command_outcomes": 0,
        "last_command_outcome_age_seconds": None,
        "policy_heads": 0,
        "expired_policy_heads": 0,
        "next_policy_expiry_seconds": None,
    }
    readiness.mark_ready(
        strategy_id=None,
        nats_connected=True,
        deployment_subscription=True,
        transport_modes={"sandbox": True, "live": False},
        runtime_metrics=runtime_metrics,
    )

    assert not is_ready_file(ready_path)
    state = read_health_file(ready_path)
    assert state is not None
    assert state["ready"] is False
    assert _health(argparse.Namespace(ready_file=ready_path, json=True)) == 1

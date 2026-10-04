"""Every time this runner writes into a signed fact renders exactly as Crucible's chrono does.

Crucible deserializes these fields into ``DateTime<Utc>`` and serializes them back with
chrono's serde implementation: RFC 3339, ``Z`` suffix, ``SecondsFormat::AutoSi``. AutoSi
writes no fraction for a whole second and otherwise the shortest of 3, 6 or 9 digits that
keeps every non-zero digit. A second rendering of the same instant would make the bytes
the runner signs differ from the bytes Crucible would produce, so any field that later
enters a digest preimage on both sides would stop matching.

The expected strings were produced by chrono 0.4.45, the version crucible-rust locks,
serializing ``DateTime::<Utc>::from_timestamp(1_791_076_923, nanos)`` through serde_json.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from custos.core import nats_transport
from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
)
from custos.core.runner_fact_producer import RunnerCapitalBasisSnapshot, _capital_basis_fact
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
EPOCH_SECONDS = 1_791_076_923
BASE = "2026-10-04T01:22:03"

# (sub-second nanoseconds, chrono rendering). Each input is chosen to separate AutoSi from
# a specific wrong renderer:
#   0            whole second; the fixed nine-digit renderers wrote ".000000000"
#   632_000_000  whole milliseconds; isoformat wrote ".632000", nine-digit ".632000000"
#   120_000_000  a zero inside the millisecond group stays; stripping zeros gives ".12"
#   123_456_000  whole microseconds; nine-digit renderers wrote ".123456000"
#   100_000      leading zeros keep the group width
#   632_100_001  a non-zero nanosecond digit keeps all nine digits
#   100          sub-microsecond only; nine digits with leading zeros
CHRONO_AUTOSI: tuple[tuple[int, str], ...] = (
    (0, f"{BASE}Z"),
    (632_000_000, f"{BASE}.632Z"),
    (120_000_000, f"{BASE}.120Z"),
    (123_456_000, f"{BASE}.123456Z"),
    (100_000, f"{BASE}.000100Z"),
    (632_100_001, f"{BASE}.632100001Z"),
    (100, f"{BASE}.000000100Z"),
)
MICROSECOND_CASES = tuple(case for case in CHRONO_AUTOSI if case[0] % 1_000 == 0)


class CapturingEmitter:
    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []

    async def emit(
        self,
        authority: RunnerFactAuthority,
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        del authority
        self.facts.extend(dict(fact) for fact in facts)

    def emit_sync(
        self,
        authority: RunnerFactAuthority,
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        del authority
        self.facts.extend(dict(fact) for fact in facts)


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="acme",
        trading_mode="sandbox",
        runner_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000002"),
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000003"),
        deployment_spec_digest=SHA,
        generation=7,
        strategy_id=UUID("40000000-0000-4000-8000-000000000004"),
        capability_version_id=UUID("50000000-0000-4000-8000-000000000005"),
        capability_version=1,
        capability_manifest_digest=SHA,
    )


def _capability() -> RunnerCapabilityReceipt:
    return RunnerCapabilityReceipt.load(
        ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
    )


def _instant(nanos: int) -> datetime:
    return datetime.fromtimestamp(EPOCH_SECONDS, UTC).replace(microsecond=nanos // 1_000)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, module: str, nanos: int) -> None:
    epoch_nanoseconds = EPOCH_SECONDS * 1_000_000_000 + nanos
    monkeypatch.setattr(f"{module}.time.time_ns", lambda: epoch_nanoseconds)


@pytest.mark.parametrize(("nanos", "expected"), MICROSECOND_CASES)
def test_capital_basis_occurred_at_renders_like_chrono(nanos: int, expected: str) -> None:
    fact = _capital_basis_fact(
        _authority(),
        observed_at=_instant(nanos),
        venue_equity="4470",
        snapshot=RunnerCapitalBasisSnapshot(
            currency="USDT",
            venue_available="4463.27",
            strategy_sizing_basis="4463.27",
            configured_initial_capital="10000",
            capital_mode="compound",
            reserved_notional="7",
            open_exposure="443",
            total_exposure="450",
            max_total_notional="1000",
            within_policy=True,
        ),
    )

    assert fact["occurred_at"] == expected


def test_capital_basis_converts_a_non_utc_observation_to_z() -> None:
    # 09:22:03.632 at +08:00 is the base instant; chrono writes it in UTC with Z.
    observed_at = datetime(2026, 10, 4, 9, 22, 3, 632_000, tzinfo=timezone(timedelta(hours=8)))

    fact = _capital_basis_fact(
        _authority(),
        observed_at=observed_at,
        venue_equity="1",
        snapshot=RunnerCapitalBasisSnapshot(
            currency="USDT",
            venue_available="1",
            strategy_sizing_basis="1",
            configured_initial_capital="1",
            capital_mode="compound",
            reserved_notional="0",
            open_exposure="0",
            total_exposure="0",
            max_total_notional="1",
            within_policy=True,
        ),
    )

    assert fact["occurred_at"] == f"{BASE}.632Z"


@pytest.mark.asyncio
@pytest.mark.parametrize(("nanos", "expected"), CHRONO_AUTOSI)
async def test_runtime_log_occurred_at_renders_like_chrono(
    monkeypatch: pytest.MonkeyPatch, nanos: int, expected: str
) -> None:
    _freeze_clock(monkeypatch, "custos.core.runtime_log_fact", nanos)
    capture = CapturingEmitter()
    emitter = RunnerRuntimeLogEmitter(
        emitter=cast(RunnerFactEmitter, capture),
        capability=_capability(),
        redactor=RuntimeLogRedactor(),
    )

    await emitter.emit(
        _authority(),
        level="INFO",
        component="local_cap",
        message="runner_cap_checked",
        structured_fields={"reason_code": "within_cap"},
        correlation_id=UUID("73000000-0000-4000-8000-000000000007"),
    )

    assert capture.facts[0]["occurred_at"] == expected


@pytest.mark.parametrize(("nanos", "expected"), CHRONO_AUTOSI)
def test_lifecycle_observed_at_renders_like_chrono(
    monkeypatch: pytest.MonkeyPatch, nanos: int, expected: str
) -> None:
    _freeze_clock(monkeypatch, "custos.core.runner_deployment_lifecycle_fact", nanos)

    fact = RunnerDeploymentLifecycleFact.observed(
        _authority(),
        generation=7,
        lifecycle_state="running",
        command_fingerprint="b" * 64,
        outcome="applied",
    )
    wire = fact.to_wire()

    assert wire["observed_at"] == expected
    assert wire["occurred_at"] == expected


_NATS_GOLDEN = (
    ROOT / "docs/authority/vendor/crucible-runner-nats-transport-authority-golden-v1.json"
)
_AUTHORITY_TIMES = ("issued_at", "not_before", "expires_at")


def _crucible_authority_digest(document: Mapping[str, Any]) -> str:
    # The rule Crucible uses for authority_digest, proven against its golden below:
    # SHA-256 over sorted, compact JSON of the document without authority_digest.
    body = {key: value for key, value in document.items() if key != "authority_digest"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _custos_authority_digest(document: Mapping[str, Any]) -> str:
    # What the runner does on receipt: parse each time, render it again, digest the result.
    reparsed = dict(document)
    for field in _AUTHORITY_TIMES:
        parsed = nats_transport._required_timestamp(document[field], field)
        reparsed[field] = nats_transport._timestamp_text(parsed)
    return _crucible_authority_digest(reparsed)


def test_crucible_golden_authority_digest_survives_the_runner_time_round_trip() -> None:
    golden = json.loads(_NATS_GOLDEN.read_text(encoding="utf-8"))
    document = json.loads(golden["canonical_json"])

    assert _crucible_authority_digest(document) == golden["authority_digest"]
    assert _custos_authority_digest(document) == golden["authority_digest"]


@pytest.mark.parametrize(
    "fraction",
    (
        # Crucible stamps authorities from a PostgreSQL timestamptz, which keeps microseconds;
        # a millisecond-aligned value is written by chrono with three digits.
        ".632",
        ".120",
        ".123456",
    ),
)
def test_sub_second_crucible_authority_times_reproduce_the_crucible_digest(
    fraction: str,
) -> None:
    golden = json.loads(_NATS_GOLDEN.read_text(encoding="utf-8"))
    document = json.loads(golden["canonical_json"])
    for field in _AUTHORITY_TIMES:
        document[field] = document[field].removesuffix("Z") + fraction + "Z"
    document["authority_digest"] = _crucible_authority_digest(document)

    assert _custos_authority_digest(document) == document["authority_digest"]

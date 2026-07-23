from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.contracts import (
    CrucibleDomainEventVerifier,
    CrucibleRunnerDeploymentCommandV1,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"
SIDECAR = FIXTURE.with_suffix(FIXTURE.suffix + ".sha256")
SNAPSHOT = ROOT / "docs/authority/ecosystem-authority.json"
SIBLING = ROOT.parent / "crucible-rust/docs/authority/runner-deployment-command-golden-v1.json"
KEY_ID = "fixture-domain-key-v1"
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed(case: dict) -> tuple[bytes, CrucibleDomainEventVerifier]:
    subject = case["subject"]
    event_bytes = json.dumps(
        case["event_document"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    subject_bytes = subject.encode("utf-8")
    framed = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
    envelope = {
        "schema_version": 1,
        "signature_profile": "crucible-domain-event-v1-exact-bytes",
        "event_encoding": "application/json;base64url",
        "event_bytes": _base64url(event_bytes),
        "signature_key_id": KEY_ID,
        "signature": _base64url(PRIVATE_KEY.sign(framed)),
    }
    data = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return data, CrucibleDomainEventVerifier(KEY_ID, PRIVATE_KEY.public_key())


@pytest.mark.parametrize(
    ("case_name", "generation", "lifecycle_state", "deployment_instance_id", "development"),
    [
        (
            "deployment_spec_ready_for_runner",
            1,
            "running",
            "20000000-0000-4000-8000-000000000002",
            False,
        ),
        (
            "deployment_instance_desired_state_changed",
            2,
            "paused",
            "20000000-0000-4000-8000-000000000002",
            False,
        ),
        (
            "deployment_spec_ready_for_runner_development_source",
            1,
            "running",
            "22000000-0000-4000-8000-000000000022",
            True,
        ),
    ],
)
def test_crucible_golden_commands_parse_through_real_signature_verifier(
    case_name: str,
    generation: int,
    lifecycle_state: str,
    deployment_instance_id: str,
    development: bool,
) -> None:
    case = next(value for value in _fixture()["cases"] if value["name"] == case_name)
    payload = case["event_document"]["payload"]
    assert payload["mode"] == "sandbox"
    assert "trading_mode" not in payload
    assert payload["deployment_spec"]["trading_mode"] == "sandbox"

    data, verifier = _signed(case)
    verifier.verify(subject=case["subject"], data=data)
    command = CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
        signed_envelope_bytes=data,
        subject=case["subject"],
    )
    spec = command.to_runtime_spec()

    assert spec.generation == generation
    assert spec.lifecycle_state.value == lifecycle_state
    assert str(spec.deployment_instance_id) == deployment_instance_id
    assert command.is_development_source is development
    if development:
        assert spec.trading_mode.value == "sandbox"
        assert spec.artifact_source.snapshot.promotable is False
        assert spec.promotion_id is None
        assert spec.promotion_evidence_digest is None


def test_outer_trading_mode_alias_is_rejected_without_fallback() -> None:
    case = deepcopy(_fixture()["cases"][0])
    payload = case["event_document"]["payload"]
    payload["trading_mode"] = payload.pop("mode")
    data, verifier = _signed(case)

    verifier.verify(subject=case["subject"], data=data)
    with pytest.raises(ValueError, match="open or incomplete"):
        CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
            signed_envelope_bytes=data,
            subject=case["subject"],
        )


def test_risk_policy_snapshot_digest_is_verified_without_business_interpretation() -> None:
    case = deepcopy(_fixture()["cases"][0])
    case["event_document"]["payload"]["deployment_spec"]["risk_policy"]["max_notional_leverage"] = (
        "99"
    )
    data, verifier = _signed(case)

    verifier.verify(subject=case["subject"], data=data)
    with pytest.raises(ValueError, match="risk policy digest differs"):
        CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
            signed_envelope_bytes=data,
            subject=case["subject"],
        )


def test_golden_hash_matches_snapshot_sidecar_and_optional_sibling() -> None:
    raw = FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    authority = snapshot["runner_command_golden_fixture"]

    assert authority["sha256"] == digest
    assert SIDECAR.read_text(encoding="ascii") == f"{digest}  {FIXTURE.name}\n"
    if SIBLING.is_file():
        assert SIBLING.read_bytes() == raw

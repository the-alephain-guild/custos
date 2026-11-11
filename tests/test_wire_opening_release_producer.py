"""Verify the pinned owner command at the exact consumer boundary."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker

from custos.core.opening_inventory_contract import (
    _canonical_amount,
    _canonical_time,
    _canonical_uuid,
)
from custos.core.runner_command_intake import (
    CommandVerificationError,
    CrucibleRunnerCommandAuthenticator,
)
from custos.core.runner_fact import RunnerFactContractError
from custos.core.runner_opening_release import RunnerOpeningReleaseCommandV1

CONTRACT_ID = "alephain.crucible.runner_opening_release_command.v1"
ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "docs/authority/vendor/contract-pins-v1.json"


def _assets() -> tuple[dict, dict, dict, dict]:
    pin = json.loads(PINS.read_text())["contracts"][CONTRACT_ID]
    golden_path = next(
        asset["local_path"]
        for asset in pin["assets"]
        if asset["producer_path"].endswith("crucible-runner-opening-release-command-v1.json")
    )
    return (
        pin,
        json.loads((ROOT / pin["schema_local_path"]).read_text()),
        json.loads((ROOT / golden_path).read_text()),
        json.loads((ROOT / pin["vectors_local_path"]).read_text()),
    )


PIN, SCHEMA, GOLDEN, VECTORS = _assets()


def _check_format(value: object, validate) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate(value)
    except (ValueError, TypeError, RunnerFactContractError):
        return False
    return True


@pytest.fixture(scope="module")
def command_schema() -> Draft202012Validator:
    formats = FormatChecker()
    for name, validate in [
        ("crucible-opening-uuid", _canonical_uuid),
        ("crucible-opening-decimal96-unsigned", _canonical_amount),
        ("crucible-opening-utc-autosi", _canonical_time),
    ]:
        formats.checks(name)(lambda value, validate=validate: _check_format(value, validate))
    schema = dict(SCHEMA)
    schema["$ref"] = "#/$defs/command"
    return Draft202012Validator(schema, format_checker=formats)


def _event(payload: dict) -> tuple[str, bytes]:
    document = copy.deepcopy(GOLDEN["fixtures"][0]["event"])
    document["payload"] = copy.deepcopy(payload)
    document["tenant_id"] = payload.get("tenant_id")
    document["aggregate_id"] = payload.get("release_id")
    document["occurred_at"] = payload.get("issued_at")
    document["event_plane"]["trading_mode"] = payload.get("trading_mode")
    subject = (
        f"crucible.runner.opening-release.v1.{payload.get('tenant_id')}."
        f"{payload.get('runner_id')}.{payload.get('trading_mode')}"
    )
    return subject, json.dumps(document, separators=(",", ":")).encode()


def _auth(fixture: dict) -> CrucibleRunnerCommandAuthenticator:
    key = GOLDEN["verifying_key_base64url"]
    public = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    )
    payload = fixture["event"]["payload"]
    return CrucibleRunnerCommandAuthenticator(
        payload["tenant_id"],
        UUID(payload["runner_id"]),
        frozenset({payload["trading_mode"]}),
        {GOLDEN["key_id"]: public},
    )


def test_opening_release_has_a_pinned_producer_revision() -> None:
    assert PIN["producer_authority"] == "alephain.crucible"
    assert SCHEMA["x-contract-id"] == GOLDEN["contract_id"] == VECTORS["contract_id"] == CONTRACT_ID
    assert (
        SCHEMA["x-contract-revision"]
        == GOLDEN["contract_revision"]
        == VECTORS["contract_revision"]
        == PIN["revision"]
    )


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda vector: vector["name"])
def test_shared_wire_domain_matches_the_verified_event_parser(vector: dict, command_schema) -> None:
    payload = vector["command"]
    subject, document = _event(payload)
    if vector["accept"]:
        parsed = RunnerOpeningReleaseCommandV1.from_verified_event(subject, document)
        assert parsed.model_dump(mode="json") == payload
    else:
        with pytest.raises((ValueError, TypeError, RunnerFactContractError)):
            RunnerOpeningReleaseCommandV1.from_verified_event(subject, document)
    assert command_schema.is_valid(payload) == vector.get("schema_accept", vector["accept"])


@pytest.mark.parametrize("fixture", GOLDEN["fixtures"], ids=lambda fixture: fixture["name"])
def test_owner_signed_vectors_verify_without_reserializing_the_event(fixture: dict) -> None:
    wire = json.dumps(fixture["envelope"], separators=(",", ":")).encode()
    verified = _auth(fixture).verify_opening_release(
        subject=fixture["subject"], signed_envelope_bytes=wire
    )
    assert verified.command.model_dump(mode="json") == fixture["event"]["payload"]
    raw = fixture["envelope"]["event_bytes"]
    assert verified.exact_signed_event_bytes == base64.urlsafe_b64decode(
        raw + "=" * (-len(raw) % 4)
    )


@pytest.mark.parametrize("fixture", GOLDEN["fixtures"], ids=lambda fixture: fixture["name"])
@pytest.mark.parametrize("mutation", ["subject", "event_bytes", "signature", "key_id"])
def test_owner_signature_cannot_be_reused_for_changed_authority(
    fixture: dict, mutation: str
) -> None:
    envelope = dict(fixture["envelope"])
    subject = fixture["subject"]
    if mutation == "subject":
        subject += ".other"
    elif mutation == "event_bytes":
        envelope["event_bytes"] = "e30"
    elif mutation == "signature":
        envelope["signature"] = base64.urlsafe_b64encode(bytes(64)).rstrip(b"=").decode()
    else:
        envelope["signature_key_id"] = "unregistered-test-key"
    with pytest.raises(CommandVerificationError):
        _auth(fixture).verify_opening_release(
            subject=subject,
            signed_envelope_bytes=json.dumps(envelope, separators=(",", ":")).encode(),
        )

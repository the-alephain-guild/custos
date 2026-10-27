"""Ed25519 permits bind local documents without granting runtime readiness."""

from __future__ import annotations

import base64
import hashlib
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes

from custos.core.local_runtime_state import RuntimeAuthorityRegistry
from tests.test_local_run_manifest import api as manifest_api
from tests.test_local_run_manifest import manifest_document
from tests.test_local_runtime_state import probe, stores
from tests.test_runtime_authority import account

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def api():
    return importlib.import_module("custos.local.authorization")


def utc(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def materials(tmp_path):
    authority = api()
    manifest = manifest_api().parse_local_manifest(canonical_json_bytes(manifest_document()))
    key = Ed25519PrivateKey.generate()
    key_id = authority.local_operator_key_id(key.public_key())
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    registry_id = registry.registry_id
    document = {
        "schema_version": 1,
        "permit_id": str(uuid4()),
        "run_id": str(manifest.run_id),
        "manifest_digest": manifest.digest,
        "account_scope": account().scope_digest,
        "control_registry_id": str(registry_id),
        "authorization_epoch": 1,
        "operator_id": "competition-operator",
        "key_id": key_id,
        "issued_at_utc": utc(NOW - timedelta(seconds=1)),
        "not_before_utc": utc(NOW),
        "expires_at_utc": utc(NOW + timedelta(hours=1)),
    }
    permit = authority.LocalRunPermitV1.model_validate(document)
    trusted = authority.LocalOperatorAuthority("competition-operator", key.public_key())
    verifier = authority.LocalPermitAuthenticator({key_id: trusted})
    args = dict(
        manifest_bytes=manifest.canonical_bytes(),
        signed_permit_bytes=authority.sign_local_permit(permit, key),
        account=account(),
        control_registry_id=registry_id,
        checkpoint=None,
        now=NOW,
    )
    with registry.account_guard(account()) as guard:
        args["checkpoint"] = guard.prepare_claim(
            run_id=manifest.run_id,
            permit_digest=hashlib.sha256(args["signed_permit_bytes"]).hexdigest(),
            expected_authorization_epoch=0,
        )
    return manifest, permit, key, verifier, args


def test_valid_local_permit_binds_target_epoch_and_durable_registry_not_process_generation(
    tmp_path,
):
    manifest, permit, _key, verifier, args = materials(tmp_path)
    verified = verifier.verify(**args)
    assert verified.checkpoint is args["checkpoint"]
    assert verified.manifest == manifest
    assert verified.permit == permit
    assert verified.permit_digest == hashlib.sha256(args["signed_permit_bytes"]).hexdigest()
    assert not hasattr(verified, "ready")
    assert not hasattr(verified.permit, "process_generation")
    assert not hasattr(verified, "tenant_id")
    assert verifier.verify(**args) == verified
    assert "schema_version" in permit.model_json_schema()["required"]
    assert "schema_version" in api().SignedLocalRunPermitV1.model_json_schema()["required"]


@pytest.mark.parametrize(
    "damage",
    ["manifest", "account", "registry", "epoch", "naive_clock", "future", "expired", "at_expiry"],
)
def test_verifier_refuses_wrong_runtime_or_time_even_with_a_real_signature(damage, tmp_path):
    manifest, permit, _key, verifier, args = materials(tmp_path)
    if damage == "manifest":
        changed = manifest.model_dump(mode="json")
        changed["effective_config_digest"] = "f" * 64
        args["manifest_bytes"] = canonical_json_bytes(changed)
    elif damage == "account":
        args["account"] = account(wallet_address="0x" + "2" * 40)
    elif damage == "registry":
        args["control_registry_id"] = uuid4()
    elif damage == "epoch":
        args["checkpoint"] = replace(args["checkpoint"], authorization_epoch=2)
    elif damage == "naive_clock":
        args["now"] = NOW.replace(tzinfo=None)
    elif damage == "future":
        args["now"] = NOW - timedelta(seconds=2)
    else:
        args["now"] = NOW + timedelta(hours=1, seconds=int(damage == "expired"))
    with pytest.raises(ValueError):
        verifier.verify(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", str(uuid4())),
        ("account_scope", "e" * 64),
        ("operator_id", "another-operator"),
        ("manifest_digest", "f" * 64),
    ],
)
def test_signed_permit_fields_must_match_manifest_and_trusted_operator(field, value, tmp_path):
    _manifest, permit, key, verifier, args = materials(tmp_path)
    changed = permit.model_dump(mode="json")
    changed[field] = value
    changed = api().LocalRunPermitV1.model_validate(changed)
    args["signed_permit_bytes"] = api().sign_local_permit(changed, key)
    args["checkpoint"] = replace(
        args["checkpoint"], permit_digest=hashlib.sha256(args["signed_permit_bytes"]).hexdigest()
    )
    with pytest.raises(ValueError):
        verifier.verify(**args)


@pytest.mark.parametrize(
    "damage",
    ["signature", "wrong_key", "domain", "whitespace", "extra", "duplicate", "untrusted_key"],
)
def test_untrusted_or_noncanonical_signed_envelopes_are_refused(damage, tmp_path):
    _manifest, permit, key, verifier, args = materials(tmp_path)
    import json

    envelope = json.loads(args["signed_permit_bytes"])
    if damage == "signature":
        envelope["signature_base64"] = base64.b64encode(bytes(64)).decode()
    elif damage == "wrong_key":
        envelope["signature_base64"] = base64.b64encode(
            Ed25519PrivateKey.generate().sign(
                api().LOCAL_PERMIT_SIGNING_DOMAIN + canonical_json_bytes(envelope["permit"])
            )
        ).decode()
    elif damage == "domain":
        envelope["signature_base64"] = base64.b64encode(
            key.sign(b"another-domain\x00" + canonical_json_bytes(envelope["permit"]))
        ).decode()
    elif damage == "extra":
        envelope["trust_root"] = "caller-selected-root"
    elif damage == "untrusted_key":
        other = Ed25519PrivateKey.generate()
        changed = permit.model_dump(mode="json")
        changed["key_id"] = api().local_operator_key_id(other.public_key())
        args["signed_permit_bytes"] = api().sign_local_permit(
            api().LocalRunPermitV1.model_validate(changed), other
        )
    if damage not in {"whitespace", "duplicate", "untrusted_key"}:
        args["signed_permit_bytes"] = canonical_json_bytes(envelope)
    if damage == "whitespace":
        args["signed_permit_bytes"] += b"\n"
    elif damage == "duplicate":
        args["signed_permit_bytes"] = b'{"schema_version":1,' + args["signed_permit_bytes"][1:]
    with pytest.raises(ValueError):
        verifier.verify(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("authorization_epoch", True),
        ("authorization_epoch", 0),
        ("issued_at_utc", "2026-10-27T12:00:00"),
        ("issued_at_utc", "2026-10-27T20:00:00.000000+08:00"),
        ("expires_at_utc", "2026-10-27T11:00:00.000000Z"),
        ("not_before_utc", "2026-10-27T11:00:00.000000Z"),
        ("permit_id", "00000000-0000-0000-0000-000000000000"),
        ("control_registry_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_permit_schema_refuses_ambiguous_time_identity_and_counter_values(field, value, tmp_path):
    _manifest, permit, _key, _verifier, _args = materials(tmp_path)
    document = permit.model_dump(mode="json")
    document[field] = value
    with pytest.raises(ValueError):
        api().LocalRunPermitV1.model_validate(document)


def test_signing_key_identity_and_trusted_operator_table_are_not_caller_mutable(tmp_path):
    _manifest, permit, key, verifier, args = materials(tmp_path)
    with pytest.raises(ValueError):
        api().sign_local_permit(permit, Ed25519PrivateKey.generate())
    key_id = api().local_operator_key_id(key.public_key())
    table = {key_id: api().LocalOperatorAuthority("competition-operator", key.public_key())}
    frozen = api().LocalPermitAuthenticator(table)
    table.clear()
    assert frozen.verify(**args) == verifier.verify(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", uuid4()),
        ("control_source", "platform"),
        ("permit_digest", "e" * 64),
        ("account_scope", "e" * 64),
        ("control_registry_id", uuid4()),
    ],
)
def test_a_matching_epoch_cannot_hide_a_different_prepared_claim(field, value, tmp_path):
    _manifest, _permit, _key, verifier, args = materials(tmp_path)
    args["checkpoint"] = replace(args["checkpoint"], **{field: value})
    with pytest.raises(ValueError):
        verifier.verify(**args)


def test_verified_authorization_retains_the_same_checkpoint_for_the_same_guard(tmp_path):
    manifest, permit, _key, verifier, args = materials(tmp_path)
    registry = RuntimeAuthorityRegistry.open(tmp_path / "control", identity_probe=probe)
    assert registry.read(account()) is None
    with registry.account_guard(account()) as guard:
        args["checkpoint"] = guard.prepare_claim(
            run_id=manifest.run_id,
            permit_digest=hashlib.sha256(args["signed_permit_bytes"]).hexdigest(),
            expected_authorization_epoch=0,
        )
        verified = verifier.verify(**args)
        assert registry.read(account()) is None
        lease = guard.claim(verified.checkpoint, stores=stores(tmp_path / "run"))
    with lease:
        assert lease.fence.run_id == permit.run_id
        assert lease.fence.authorization_epoch == permit.authorization_epoch
        assert lease.fence.account_scope == permit.account_scope
        lease.assert_current()


def test_the_same_signed_permit_remains_valid_for_a_new_owner_generation(tmp_path):
    manifest, permit, _key, verifier, args = materials(tmp_path)
    registry = RuntimeAuthorityRegistry.open(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    owners = []
    for expected_epoch in (0, 1):
        with registry.account_guard(account()) as guard:
            args["checkpoint"] = guard.prepare_claim(
                run_id=manifest.run_id,
                permit_digest=api().local_permit_digest(args["signed_permit_bytes"]),
                expected_authorization_epoch=expected_epoch,
            )
            verified = verifier.verify(**args)
            assert verified.permit == permit
            lease = guard.claim(verified.checkpoint, stores=bindings)
        with lease:
            lease.assert_current()
            owners.append(lease.fence)
    assert owners[0].authorization_epoch == owners[1].authorization_epoch == 1
    assert owners[1].process_generation == owners[0].process_generation + 1
    assert owners[1].owner_id != owners[0].owner_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorization_epoch", True),
        ("authorization_epoch", "1"),
    ],
)
def test_checkpoint_counter_coercion_cannot_create_verified_authority(field, value, tmp_path):
    _manifest, _permit, _key, verifier, args = materials(tmp_path)
    args["checkpoint"] = replace(args["checkpoint"], **{field: value})
    with pytest.raises(ValueError):
        verifier.verify(**args)


def test_the_permit_digest_is_the_canonical_full_envelope_digest(tmp_path):
    _manifest, _permit, _key, _verifier, args = materials(tmp_path)
    raw = args["signed_permit_bytes"]
    assert api().local_permit_digest(raw) == hashlib.sha256(raw).hexdigest()
    assert api().local_permit_digest(raw) == args["checkpoint"].permit_digest
    with pytest.raises(ValueError):
        api().local_permit_digest(raw + b"\n")

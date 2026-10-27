"""Local run authorization material is strict and never a readiness receipt."""

from __future__ import annotations

import hashlib
import importlib
from copy import deepcopy
from decimal import DefaultContext, Overflow, localcontext
from uuid import uuid4

import pytest
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes

from tests.test_pre_sign_artifact_ref import _golden
from tests.test_runtime_authority import account


def api():
    return importlib.import_module("custos.local.manifest")


def manifest_document():
    return {
        "schema_version": 1,
        "control_source": "local",
        "run_id": str(uuid4()),
        "configuration_revision": 1,
        "account": vars(account()),
        "credential_ref": "competition-session",
        "artifact": deepcopy(_golden()["artifact_ref"]),
        "release_bom_sha256": "b" * 64,
        "toolkit_identity_sha256": "c" * 64,
        "effective_config_digest": "d" * 64,
        "entry_point": "test_strategy.runtime:build_strategy",
        "instrument_ids": ["BTC-USD-PERP.PAPERTRADE"],
        "limits": {
            "max_capital_usd": "1000",
            "max_order_margin_usd": "100",
            "max_order_notional_usd": "500",
            "max_total_notional_usd": "2000",
            "max_loss_usd": "30",
            "leverage": 5,
        },
        "data_policy": {"max_quote_age_seconds": 10, "max_account_age_seconds": 15},
        "stop_policy": {"position_policy": "flatten", "confirmation_timeout_seconds": 10800},
    }


def test_manifest_binds_exact_material_config_account_and_funding_units():
    raw = canonical_json_bytes(manifest_document())
    manifest = api().parse_local_manifest(raw)
    assert manifest.account_identity == account()
    assert manifest.limits.leverage == 5
    assert manifest.limits.max_order_margin_usd == "100"
    assert manifest.limits.max_order_notional_usd == "500"
    assert manifest.canonical_bytes() == raw
    assert manifest.digest == hashlib.sha256(raw).hexdigest()
    assert not hasattr(manifest, "ready")
    assert not hasattr(manifest, "tenant_id")
    assert not hasattr(manifest, "deployment_instance_id")
    with pytest.raises(ValueError):
        manifest.configuration_revision = 2
    assert "schema_version" in manifest.model_json_schema()["required"]


@pytest.mark.parametrize(
    "value", [True, 100, 100.0, "1e2", "100.0", "0100", "NaN", "-100", "0", "100 "]
)
def test_manifest_rejects_noncanonical_or_nonpositive_usd_amounts(value):
    document = manifest_document()
    document["limits"]["max_order_margin_usd"] = value
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


@pytest.mark.parametrize("value", [True, 5.0, "5", 0, -1, 1001])
def test_manifest_leverage_is_a_supported_strict_integer(value):
    document = manifest_document()
    document["limits"]["leverage"] = value
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


@pytest.mark.parametrize(
    "damage", ["notional", "order_capital", "total_capital", "total_order", "loss_capital"]
)
def test_manifest_risk_units_and_account_limits_must_agree(damage):
    document = manifest_document()
    field, value = {
        "notional": ("max_order_notional_usd", "100"),
        "order_capital": ("max_capital_usd", "50"),
        "total_capital": ("max_total_notional_usd", "5001"),
        "total_order": ("max_total_notional_usd", "499"),
        "loss_capital": ("max_loss_usd", "1001"),
    }[damage]
    document["limits"][field] = value
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


def test_manifest_checks_exact_margin_times_leverage_under_a_low_decimal_context():
    document = manifest_document()
    document["limits"].update(
        max_order_margin_usd="100.123456789123456789",
        max_order_notional_usd="500.617283945617283945",
    )
    with localcontext() as context:
        context.prec = 2
        manifest = api().LocalRunManifestV1.model_validate(document)
    assert manifest.limits.max_order_notional_usd == "500.617283945617283945"


def test_default_decimal_context_cannot_turn_a_capital_limit_into_infinity(monkeypatch):
    document = manifest_document()
    document["limits"]["max_total_notional_usd"] = "9999"
    monkeypatch.setattr(DefaultContext, "Emax", 2)
    monkeypatch.setitem(DefaultContext.traps, Overflow, False)
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("configuration_revision",), True),
        (("configuration_revision",), 0),
        (("control_source",), "platform"),
        (("account", "chain_id"), True),
        (("account", "wallet_address"), "0x" + "0" * 40),
        (("account", "exchange"), "0x" + "2" * 40),
        (("credential_ref",), "../key"),
        (("credential_ref",), "/tmp/key"),
        (("artifact", "engine_version"), "any"),
        (("entry_point",), "not-an-entry-point"),
        (("instrument_ids",), []),
        (("instrument_ids",), ["BTC.PAPERTRADE", "BTC.PAPERTRADE"]),
        (("data_policy", "max_quote_age_seconds"), True),
        (("stop_policy", "position_policy"), "ignore"),
        (("stop_policy", "confirmation_timeout_seconds"), 0),
        (("effective_config_digest",), "not-a-digest"),
    ],
)
def test_manifest_rejects_invalid_identity_or_operating_policy(path, value):
    document = manifest_document()
    parent = document
    for field in path[:-1]:
        parent = parent[field]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


@pytest.mark.parametrize(
    "path", [(), ("account",), ("limits",), ("data_policy",), ("stop_policy",)]
)
def test_manifest_forbids_unknown_fields_at_every_local_boundary(path):
    document = manifest_document()
    parent = document
    for field in path:
        parent = parent[field]
    parent["untrusted_override"] = "ignored-is-unsafe"
    with pytest.raises(ValueError):
        api().LocalRunManifestV1.model_validate(document)


@pytest.mark.parametrize("damage", ["whitespace", "duplicate", "nonfinite", "utf8", "missing"])
def test_manifest_parser_accepts_only_exact_canonical_utf8_json(damage):
    raw = canonical_json_bytes(manifest_document())
    if damage == "whitespace":
        raw += b"\n"
    elif damage == "duplicate":
        raw = b'{"schema_version":1,' + raw[1:]
    elif damage == "nonfinite":
        raw = b'{"number":NaN}'
    elif damage == "utf8":
        raw = b"\xff"
    else:
        document = manifest_document()
        del document["limits"]
        raw = canonical_json_bytes(document)
    with pytest.raises(ValueError):
        api().parse_local_manifest(raw)

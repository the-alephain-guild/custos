"""The offline lane's own spec contract, in the shape its consumer renders.

The consumer is `philosophers-stone/deploy/custos`, whose renderer emits exactly
the keys asserted here. This contract is deliberately not the canonical V1
DeploymentSpec: the two have diverged, and reviving the old shape under the
canonical name would be a predecessor parser for V1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.spec import (
    OfflineDeploymentMessage,
    OfflineDeploymentSpec,
    compute_strategy_code_hash,
    offline_subject,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs/gateway-contract/v1/samples"
SCHEMA = ROOT / "docs/gateway-contract/v1/offline_deployment_spec.schema.json"


def _rendered_spec(**overrides: Any) -> dict[str, Any]:
    """The exact key set `deploy/custos/scripts/render_spec.py` emits."""

    spec = {
        "trading_mode": "sandbox",
        "lifecycle_state": "running",
        "code_hash": None,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
        "spec_id": "supertrend-sandbox",
        "generation": 1,
        "strategy_path": "/opt/ps/trend/supertrend/refinement/nautilus",
        "provenance_ref": {"credential_id": "binance-supertrend"},
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "strategy_registry_name": "supertrend",
        "strategy_config": {"timeframe": "1m"},
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def strategy_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "strategy"
    directory.mkdir()
    (directory / "strategy.py").write_text("class S: ...\n", encoding="utf-8")
    (directory / "config.yaml").write_text("timeframe: 1m\n", encoding="utf-8")
    return directory


def test_accepts_the_spec_the_consumer_renders() -> None:
    spec = OfflineDeploymentSpec.model_validate(_rendered_spec())

    assert spec.spec_id == "supertrend-sandbox"
    assert spec.generation == 1
    assert spec.strategy_registry_name == "supertrend"


def test_accepts_a_testnet_spec_without_sandbox_balances() -> None:
    spec = OfflineDeploymentSpec.model_validate(
        _rendered_spec(trading_mode="testnet", spec_id="supertrend-testnet", sandbox=None)
    )

    assert spec.trading_mode.value == "testnet"


def test_refuses_a_live_spec_at_construction() -> None:
    with pytest.raises(OfflineModeRefused, match="live"):
        OfflineDeploymentSpec.model_validate(_rendered_spec(trading_mode="live"))


def test_sandbox_requires_starting_balances() -> None:
    with pytest.raises(ValidationError, match="starting_balances"):
        OfflineDeploymentSpec.model_validate(_rendered_spec(sandbox=None))


@pytest.mark.parametrize("generation", [0, -1, "1", 1.5, None])
def test_rejects_a_malformed_generation(generation: Any) -> None:
    with pytest.raises(ValidationError):
        OfflineDeploymentSpec.model_validate(_rendered_spec(generation=generation))


def test_rejects_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        OfflineDeploymentSpec.model_validate(_rendered_spec(smuggled="payload"))


def test_code_hash_covers_content_and_layout(strategy_dir: Path) -> None:
    original = compute_strategy_code_hash(strategy_dir)

    (strategy_dir / "strategy.py").write_text("class S: pass\n", encoding="utf-8")
    assert compute_strategy_code_hash(strategy_dir) != original

    (strategy_dir / "strategy.py").write_text("class S: ...\n", encoding="utf-8")
    assert compute_strategy_code_hash(strategy_dir) == original

    (strategy_dir / "strategy.py").rename(strategy_dir / "renamed.py")
    assert compute_strategy_code_hash(strategy_dir) != original


def test_code_hash_ignores_build_artefacts(strategy_dir: Path) -> None:
    original = compute_strategy_code_hash(strategy_dir)
    cache = strategy_dir / "__pycache__"
    cache.mkdir()
    (cache / "strategy.cpython-312.pyc").write_bytes(b"\x00\x01")

    assert compute_strategy_code_hash(strategy_dir) == original


def test_code_hash_reports_a_missing_strategy_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="strategy directory"):
        compute_strategy_code_hash(tmp_path / "absent")


def test_subject_binds_tenant_and_strategy() -> None:
    assert offline_subject("local", "deployment_spec", "supertrend") == (
        "arx.local.deployment_spec.supertrend"
    )


@pytest.mark.parametrize(
    ("tenant", "kind", "parts"),
    [("", "deployment_spec", ("s",)), ("local", "", ("s",)), ("local", "deployment_spec", ("",))],
)
def test_subject_refuses_empty_tokens(tenant: str, kind: str, parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="non-empty|required"):
        offline_subject(tenant, kind, *parts)


def test_message_round_trips_through_bytes() -> None:
    spec = OfflineDeploymentSpec.model_validate(_rendered_spec())
    message = OfflineDeploymentMessage.create(
        tenant_id="local", strategy_id="supertrend", spec=spec
    )

    parsed = OfflineDeploymentMessage.parse(message.to_bytes(), expected_tenant_id="local")

    assert parsed.subject == message.subject == "arx.local.deployment_spec.supertrend"
    assert parsed.spec.generation == spec.generation
    assert parsed.spec.spec_id == spec.spec_id


def test_parse_refuses_a_message_addressed_to_another_tenant() -> None:
    spec = OfflineDeploymentSpec.model_validate(_rendered_spec())
    message = OfflineDeploymentMessage.create(
        tenant_id="local", strategy_id="supertrend", spec=spec
    )

    with pytest.raises(ValueError, match="tenant"):
        OfflineDeploymentMessage.parse(message.to_bytes(), expected_tenant_id="someone-else")


def test_parse_refuses_a_live_message_off_the_wire() -> None:
    """A live spec cannot enter through the wire either, only through construction."""

    spec = OfflineDeploymentSpec.model_validate(_rendered_spec())
    message = OfflineDeploymentMessage.create(
        tenant_id="local", strategy_id="supertrend", spec=spec
    )
    document = json.loads(message.to_bytes())
    document["payload"]["spec"]["trading_mode"] = "live"

    with pytest.raises(OfflineModeRefused, match="live"):
        OfflineDeploymentMessage.parse(
            json.dumps(document).encode("utf-8"), expected_tenant_id="local"
        )


def test_published_schema_is_derived_from_the_model() -> None:
    """The asset is generated, not transcribed, so it cannot drift silently.

    When the model changes on purpose, rewrite the asset from it rather than by
    hand: ``OfflineDeploymentSpec.model_json_schema()`` dumped with
    ``indent=2, sort_keys=True`` and a trailing newline.
    """

    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == (
        OfflineDeploymentSpec.model_json_schema()
    )


@pytest.mark.parametrize("mode", ["sandbox", "testnet"])
def test_published_samples_validate(mode: str) -> None:
    sample = SAMPLES / f"offline_deployment_spec_{mode}.json"

    spec = OfflineDeploymentSpec.model_validate_json(sample.read_bytes())

    assert spec.trading_mode.value == mode

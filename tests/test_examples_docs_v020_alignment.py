from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SANDBOX_DIR = EXAMPLES_DIR / "supertrend-sandbox"
TESTNET_DIR = EXAMPLES_DIR / "supertrend-testnet"
LEGACY_TOKENS = (
    "--sops-file",
    "--age-key-file",
    "python -m custos",
    "--use-nt-host",
)
TEXT_FILES = (
    SANDBOX_DIR / "README.md",
    TESTNET_DIR / "README.md",
    TESTNET_DIR / ".env.example",
    TESTNET_DIR / "docker-compose.yaml",
)
ACTIVE_RUNTIME_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "ops" / "05-deployment.md",
    SANDBOX_DIR / "README.md",
    TESTNET_DIR / "README.md",
)
LOCAL_IMAGE = "custos-runner:v0.3.0"
REMOTE_IMAGE = "ghcr.io/the-alephain-guild/custos:v0.3.0"
DEPLOYMENT_RUNTIME_CONTRACT = (
    REPO_ROOT / ".forge" / "plans" / "2026-07" / "14-clean-deployment-runtime-contract.md"
)
VERIFICATION_RULE = REPO_ROOT / ".claude" / "rules" / "verification.md"


def test_project_and_lock_are_versioned_v030() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package for package in lock["package"] if package["name"] == "custos-runner"
    )

    assert project["project"]["version"] == "0.3.0"
    assert locked_project["version"] == "0.3.0"


def test_changelog_documents_v030_clean_break() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    v030 = text[text.index("## [0.3.0]") : text.index("## [0.2.0]")]

    assert "## [0.3.0] - 2026-07-12" in text
    assert LOCAL_IMAGE in v030
    assert "Remote release: deferred" in v030
    assert REMOTE_IMAGE not in v030
    for contract in (
        "--engine",
        "--use-nt-host",
        "generation",
        "lifecycle_state",
        "deployment publish",
        "nats bootstrap",
        "health",
    ):
        assert contract in text


def test_readme_declares_single_authority_topology() -> None:
    """The README must state who decides and who executes, without naming the
    services behind ARX. It is the repository's public front page."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "ARX: authorize the intent, approve and sign the deployment command" in text
    assert "Custos: verify the command, reconcile local runtime, sign execution facts" in text
    assert "ARX: accept the signed facts and advance the deployment lifecycle" in text
    assert "`deployment_instance_id` is the only key" in normalized
    assert "Custos has no path to create or approve a deployment" in normalized


def test_readme_does_not_name_services_behind_arx() -> None:
    """ARX is one commercial product to a reader. Naming what implements it
    discloses a boundary the reader neither needs nor is entitled to."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for name in ("Crucible", "Philosophers-Stone", "Speculum", "Synedrion", "Athanor"):
        assert name not in text, f"README names an internal system: {name}"


def test_readme_forbids_business_fact_relay() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "nothing relays a deployment command or a runner fact" in normalized
    assert "Custos does not count approvals" in normalized
    assert "ARX never holds a credential or places an order" in normalized


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_example_text_has_no_legacy_cli_tokens(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for token in LEGACY_TOKENS:
        assert token not in text


@pytest.mark.parametrize(
    "path", ACTIVE_RUNTIME_DOCS, ids=lambda path: str(path.relative_to(REPO_ROOT))
)
def test_active_runtime_docs_do_not_use_removed_engine_flag(path: Path) -> None:
    assert "--use-nt-host" not in path.read_text(encoding="utf-8")


def test_testnet_compose_uses_inbound_only_runtime_shape() -> None:
    raw = (TESTNET_DIR / "docker-compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    assert set(compose["services"]) == {"runner"}
    runner = compose["services"]["runner"]

    assert "build" not in runner
    assert runner["image"] == LOCAL_IMAGE
    assert runner["pull_policy"] == "never"
    assert runner["command"][0] == "start"
    engine_index = runner["command"].index("--engine")
    assert runner["command"][engine_index : engine_index + 2] == ["--engine", "nautilus"]
    mode_index = runner["command"].index("--enabled-mode")
    assert runner["command"][mode_index : mode_index + 2] == ["--enabled-mode", "testnet"]
    assert "--crucible-domain-public-key" in runner["command"]
    assert "--crucible-domain-key-id" in runner["command"]
    assert runner["healthcheck"]["test"] == ["CMD", "arx-runner", "health"]
    assert "./runtime/.arx:/home/custos/.arx" in runner["volumes"]
    assert all("strateg" not in volume for volume in runner["volumes"])
    assert "deployment publish" not in raw
    assert "nats bootstrap" not in raw


def test_testnet_readme_requires_local_image_gate() -> None:
    text = (TESTNET_DIR / "README.md").read_text(encoding="utf-8")

    assert "make verify-local-v030" in text
    assert LOCAL_IMAGE in text
    assert REMOTE_IMAGE not in text


def test_local_consumer_gate_is_registered_in_deployment_runtime_contract() -> None:
    verification = VERIFICATION_RULE.read_text(encoding="utf-8")
    deployment_runtime_contract = DEPLOYMENT_RUNTIME_CONTRACT.read_text(encoding="utf-8")

    assert "make verify-local-v030" in verification
    assert "Plan 16 verified local image" in deployment_runtime_contract
    assert "PS local-development gate" in deployment_runtime_contract


def test_testnet_example_has_no_derived_dockerfile() -> None:
    assert not (TESTNET_DIR / "Dockerfile").exists()


def test_testnet_env_contains_only_non_secret_runtime_keys() -> None:
    values = {}
    for raw_line in (TESTNET_DIR / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    assert set(values) == {
        "CUSTOS_TENANT_ID",
        "CUSTOS_RUNNER_ID",
        "CRUCIBLE_HTTP_URL",
        "CRUCIBLE_NATS_URL",
        "CRUCIBLE_DOMAIN_EVENT_KEY_ID",
    }
    assert all(
        "API_KEY" not in key and "SECRET" not in key and "TOKEN" not in key for key in values
    )


def test_testnet_vault_fixture_is_one_per_key_payload() -> None:
    fixture = json.loads(
        (TESTNET_DIR / "vault-fixture" / "credentials.example.json").read_text(encoding="utf-8")
    )
    assert list(fixture) == ["binance-testnet"]
    assert set(fixture["binance-testnet"]) == {
        "api_key",
        "api_secret",
        "permission_scope",
    }
    assert fixture["binance-testnet"]["permission_scope"] == "trade_no_withdraw"


def test_examples_do_not_duplicate_crucible_owned_deployment_specs() -> None:
    assert not (SANDBOX_DIR / "spec-example.json").exists()
    assert not (TESTNET_DIR / "spec-example.json").exists()
    assert not (
        REPO_ROOT / "docs" / "gateway-contract" / "v1" / "deployment_spec.schema.json"
    ).exists()
    assert not (
        REPO_ROOT / "docs" / "gateway-contract" / "v1" / "samples" / "deployment_spec_sandbox.json"
    ).exists()


def test_gateway_docs_teach_crucible_owned_deployment_seam() -> None:
    text = " ".join(
        (REPO_ROOT / "docs" / "gateway-contract" / "v1" / "README.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "does not publish a DeploymentSpec schema" in text
    assert "Crucible owns the canonical DeploymentSpec" in text
    assert "DeploymentSpecReadyForRunner" in text
    assert "authenticated Crucible `StrategyRelease` authority" in text
    assert "arx-runner deployment" not in text


@pytest.mark.parametrize("path", (SANDBOX_DIR / "README.md", TESTNET_DIR / "README.md"))
def test_example_readme_teaches_owner_correct_three_step_flow(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "arx-runner enroll" in text
    assert "arx-runner vault put" in text
    assert "runner.toml" in text
    assert "arx-runner start" in text or "docker compose up" in text
    assert "trade_no_withdraw" in text
    assert "Crucible" in text
    assert "deployment publish" not in text

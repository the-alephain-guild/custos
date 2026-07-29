import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/gateway-contract/v1"


def test_custos_does_not_publish_upstream_deployment_spec_assets() -> None:
    assert not (CONTRACT / "deployment_spec.schema.json").exists()
    assert not (CONTRACT / "samples/deployment_spec_sandbox.json").exists()


def test_gateway_readme_teaches_the_single_owner_boundary() -> None:
    text = " ".join((CONTRACT / "README.md").read_text(encoding="utf-8").split())

    assert "does not publish a DeploymentSpec schema" in text
    assert "canonical DeploymentSpec is owned upstream" in text
    assert "authenticated upstream `StrategyRelease` authority" in text
    assert "never substitutes for the signed one" in text, (
        "the offline schema published beside this README must be scoped here, or a "
        "producer could read it as the canonical one"
    )


def test_gateway_readme_names_the_owner_by_boundary_not_by_service() -> None:
    """This file is public, and the reader is an integrator or an auditor.

    Which internal service implements the upstream side is not theirs to
    depend on; the boundary is. The exception is a symbol that genuinely
    exists -- a type name carrying a legacy spelling is reproduced verbatim,
    because renaming it here would point at nothing.
    """
    text = (CONTRACT / "README.md").read_text(encoding="utf-8")

    prose = re.sub(r"`[^`]*`", "", text)
    assert "Crucible" not in prose, (
        "the upstream owner is named by service in prose; name the boundary "
        "instead. Type names and wire literals stay, inside backticks."
    )


def test_the_offline_lane_keeps_its_entry_points_on_the_cli() -> None:
    """`philosophers-stone/deploy/custos` drives the offline lane through these.

    Its compose file runs `nats bootstrap` and `deployment publish`, and its
    Makefile runs `deployment validate`. Removing either command breaks that
    harness. The last removal went unnoticed because the tests covering it were
    deleted in the same commit, so this one is derived from the parser rather
    than from a list that could go stale beside the code it describes.
    """
    import argparse

    from custos.cli.subcommands import _build_parser

    actions = [
        action
        for action in _build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    surface = actions[0].choices

    assert "deployment" in surface, "the offline lane lost its publish/validate surface"
    assert "nats" in surface, "the offline lane lost its transport bootstrap"

    deployment_actions = [
        action
        for action in surface["deployment"]._actions
        if isinstance(action, argparse._SubParsersAction)
    ][0].choices
    assert {"validate", "publish"} <= set(deployment_actions)


def test_the_offline_spec_contract_is_published() -> None:
    assert (CONTRACT / "offline_deployment_spec.schema.json").is_file()
    for mode in ("sandbox", "testnet"):
        assert (CONTRACT / f"samples/offline_deployment_spec_{mode}.json").is_file()


def test_the_published_offline_samples_still_parse() -> None:
    from custos.offline.spec import OfflineDeploymentSpec

    for mode in ("sandbox", "testnet"):
        sample = CONTRACT / f"samples/offline_deployment_spec_{mode}.json"
        assert OfflineDeploymentSpec.model_validate_json(sample.read_bytes())


def test_gateway_readme_explains_the_offline_schema_it_publishes() -> None:
    """An asset sitting in a public contract directory with no prose is a papercut."""
    text = " ".join((CONTRACT / "README.md").read_text(encoding="utf-8").split())

    assert "offline_deployment_spec.schema.json" in text
    assert "sandbox and testnet only" in text

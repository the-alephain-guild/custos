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
    assert "arx-runner deployment" not in text


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

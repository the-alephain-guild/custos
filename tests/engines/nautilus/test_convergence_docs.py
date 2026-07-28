"""Contract tests for ps convergence documentation.

These tests read the real philosophers-stone `shared/README.md` and the
custos-side `docs/authority/nautilus-host-contract.md`, verifying the bilateral
convergence documentation lands: Custos packages are declared as the execution
toolkit authority, ps `shared/` is documented as a non-destructively
preserved research copy, and the crucible Docker preservation hard
constraint is stated with its Dockerfile-line evidence.

The ps-side assertion needs a local philosophers-stone checkout (`PS_ROOT`)
— skipped when unavailable, matching independent-repo self-sufficiency
(mandatory-rules §7): an external auditor cloning custos alone should not
have `make verify` blocked by a repo they do not have.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2].parent
_NAUTILUS_HOST_DOC = _REPO_ROOT / "docs" / "authority" / "nautilus-host-contract.md"


def test_ps_convergence_documentation_no_destructive_delete() -> None:
    """ps `shared/README.md` must point at custos toolkit as authority, state
    the no-destructive-delete guarantee, and reference the crucible Docker
    preservation window."""
    ps_root = os.environ.get("PS_ROOT")
    if not ps_root:
        pytest.skip("PS_ROOT env var not set — needs a local philosophers-stone checkout")

    readme = Path(ps_root) / "shared" / "README.md"
    assert readme.exists(), f"expected ps shared/README.md at {readme}"
    text = readme.read_text(encoding="utf-8")

    assert "custos" in text.lower(), "ps README must reference custos as the authority"
    assert "packages/custos-strategy-toolkit/src/custos_toolkit" in text, (
        "ps README must point at the exact custos authority path"
    )
    assert "no destructive delete" in text.lower(), (
        "ps README must state the no-destructive-delete guarantee"
    )
    assert "crucible" in text.lower(), (
        "ps README must reference the crucible Docker preservation window"
    )


def test_crucible_docker_preservation_window_documented() -> None:
    """custos-side docs/authority/nautilus-host-contract.md must name the crucible Docker
    preservation hard constraint with its Dockerfile-line evidence and point
    at the future crucible-runtime-migration candidate plan."""
    text = _NAUTILUS_HOST_DOC.read_text(encoding="utf-8")

    assert "Toolkit sync discipline" in text
    assert "crucible docker preservation window" in text.lower()
    assert "deploy/nautilus/Dockerfile" in text
    assert "deploy/hummingbot/Dockerfile.image" in text
    assert "crucible-runtime-migration" in text

"""Where the workflow publishes must match where the evidence says it published.

Custos moved organizations in `9b2beec`. That commit changed every place the
release workflow names its target — the job guard, the OCI repository, the
expected Sigstore identity — and changed the one shape assertion that reads them
back. Every piece of recorded evidence stayed at the old organization, and
nothing went red, because the workflow was only ever compared against itself.
The divergence was caught by a person reading two files side by side.

These tests compare the workflow against the receipt the last release produced
and against the authority snapshot that registered it. No coordinate is spelled
out here: an organization rename is allowed, and re-issuing the evidence in the
same change keeps them green. What turns them red is moving one side alone.

`test_renaming_the_organization_in_the_workflow_alone_is_visible_here` injects
exactly that divergence, so these checks cannot pass by extracting nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release-toolkit-rc.yml"
RECEIPT = ROOT / "docs/authority/receipts/custos-toolkit-rc-authority-v1.json"
SNAPSHOT = ROOT / "docs/authority/ecosystem-authority.json"

_NAMESPACE = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9]*/[-A-Za-z0-9_.]+$")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _receipt() -> dict:
    return json.loads(RECEIPT.read_text(encoding="utf-8"))


def _publication() -> dict:
    return _receipt()["publication_receipt"]


def _snapshot_release() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["toolkit_rc_release"]


def _only(pattern: str, text: str, label: str) -> str:
    """Return the single capture, refusing both no match and several.

    Refusing zero matters more than it looks: a pattern that quietly stops
    matching would let every comparison below compare nothing to nothing.
    """

    found = re.findall(pattern, text, flags=re.MULTILINE)
    assert len(found) == 1, f"{label}: expected exactly one declaration, found {found}"
    return found[0]


def _env(name: str, text: str) -> str:
    return _only(rf"^ +{name}: (\S+)$", text, f"env {name}")


def _oci_repository(text: str) -> str:
    declared = _env("CUSTOS_TOOLKIT_OCI_REPOSITORY", text)
    asserted = _only(
        r'^ +test "\$CUSTOS_TOOLKIT_OCI_REPOSITORY" = "([^"]+)"$',
        text,
        "in-run OCI repository assertion",
    )
    assert declared == asserted, "the workflow declares one OCI repository and asserts another"
    return declared


def _source_repository(text: str) -> str:
    guarded = _only(r"^ +if: github\.repository == '([^']+)'", text, "job guard repository")
    asserted = _only(
        r'^ +test "\$GITHUB_REPOSITORY" = "([^"]+)"$',
        text,
        "in-run repository assertion",
    )
    assert guarded == asserted, "the job guard and the in-run assertion name different repositories"
    return guarded


def _sigstore_identity(text: str) -> str:
    return _only(r"^ +EXPECTED_SIGSTORE_IDENTITY: >-\n +(\S+)$", text, "expected Sigstore identity")


def test_the_workflow_publishes_into_the_namespace_the_receipt_recorded() -> None:
    text = _workflow_text()
    registry = _env("CUSTOS_TOOLKIT_OCI_REGISTRY", text)
    repository = _oci_repository(text)
    publication = _publication()

    assert _NAMESPACE.fullmatch(repository), f"not an owner/name pair: {repository}"
    assert registry == publication["registry"]
    assert repository == publication["repository"]
    assert publication["oci_coordinate"].startswith(f"{registry}/{repository}@sha256:")


def test_the_authority_snapshot_registers_that_same_namespace() -> None:
    text = _workflow_text()
    registry = _env("CUSTOS_TOOLKIT_OCI_REGISTRY", text)
    repository = _oci_repository(text)
    release = _snapshot_release()

    assert release["registry_repository"] == f"{registry}/{repository}"
    assert release["oci_coordinate"] == _publication()["oci_coordinate"]


def test_the_job_guard_names_the_repository_the_release_was_signed_from() -> None:
    text = _workflow_text()
    repository = _source_repository(text)
    receipt = _receipt()
    publication = _publication()
    identity = f"https://github.com/{repository}/.github/workflows/{WORKFLOW.name}@refs/heads/main"

    assert _NAMESPACE.fullmatch(repository), f"not an owner/name pair: {repository}"
    assert receipt["source_repository"] == f"https://github.com/{repository}"
    assert publication["source_repository"] == f"https://github.com/{repository}"
    assert publication["workflow_identity"] == identity
    assert publication["workflow_ref"] == identity.removeprefix("https://github.com/")
    assert _sigstore_identity(text) == identity


def test_the_protected_environment_is_the_one_the_release_recorded() -> None:
    text = _workflow_text()
    environment = _only(r"^ +environment: (\S+)$", text, "job environment")

    assert _env("CUSTOS_TOOLKIT_RELEASE_ENVIRONMENT", text) == environment
    assert _publication()["release_environment"] == environment
    assert _snapshot_release()["release_environment"] == environment


def test_the_workflow_and_the_receipt_agree_on_the_signing_issuer() -> None:
    assert _env("EXPECTED_SIGSTORE_ISSUER", _workflow_text()) == _publication()["oidc_issuer"]


def test_renaming_the_organization_in_the_workflow_alone_is_visible_here() -> None:
    """Replay the miss: move the workflow to a new owner, leave the evidence.

    Without this, a pattern that stopped matching would leave the comparisons
    above comparing nothing to nothing, and they would stay green through the
    very change they exist to catch.
    """

    text = _workflow_text()
    owner = _oci_repository(text).split("/", 1)[0]
    renamed = text.replace(f"{owner}/", f"{owner}-under-new-management/")
    publication = _publication()

    assert renamed != text
    assert _oci_repository(renamed) != publication["repository"]
    assert _source_repository(renamed) != publication["source_repository"].removeprefix(
        "https://github.com/"
    )
    assert _sigstore_identity(renamed) != publication["workflow_identity"]

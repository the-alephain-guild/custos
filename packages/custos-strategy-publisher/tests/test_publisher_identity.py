"""Any producer can publish; what it publishes as has to be well formed and consistent.

The publication contract used to accept exactly one producer's repository and
workflow. It now accepts any, and checks the shape of each value and that the
values agree with each other: a workflow identity is its ref on github.com, and
an OIDC subject names a repository at the source ref.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custos_strategy_publisher.ghcr_registry import GhcrRegistryV1
from custos_strategy_publisher.oci_primitives import (
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_ISSUER,
    OciPublicationError,
    PublicationWorkflowIdentityV1,
    require_ghcr_repository,
)
from custos_strategy_publisher.oci_publication import _TAG_RE

# A producer calling the publisher's reusable workflow from its own repository.
REUSABLE_WORKFLOW_REF = (
    "the-alephain-guild/custos/.github/workflows/publish-strategy-release.yml@refs/tags/v0.4.0"
)


def _identity(**overrides: object) -> PublicationWorkflowIdentityV1:
    values: dict[str, object] = {
        "workflow_identity": f"https://github.com/{REUSABLE_WORKFLOW_REF}",
        "workflow_ref": REUSABLE_WORKFLOW_REF,
        "workflow_run_id": 12,
        "workflow_run_attempt": 1,
        "source_ref": "refs/heads/main",
        "oidc_issuer": GITHUB_OIDC_ISSUER,
        "oidc_subject": "repo:example-owner/example-strategies:ref:refs/heads/main",
        "oidc_audience": GITHUB_OIDC_AUDIENCE,
    }
    values.update(overrides)
    return PublicationWorkflowIdentityV1(**values)  # type: ignore[arg-type]


def test_a_reusable_workflow_run_from_another_repository_is_accepted() -> None:
    identity = _identity()

    assert identity.oidc_subject.startswith("repo:example-owner/example-strategies:")


def test_a_workflow_pinned_by_commit_is_accepted() -> None:
    ref = "the-alephain-guild/custos/.github/workflows/publish-strategy-release.yml@" + "a" * 40

    assert _identity(workflow_ref=ref, workflow_identity=f"https://github.com/{ref}")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"workflow_identity": "https://github.com/someone/else/.github/workflows/x.yml@refs/heads/main"},
         "workflow_identity"),
        ({"workflow_ref": "no-workflow-here", "workflow_identity": "https://github.com/no-workflow-here"},
         "workflow_ref"),
        ({"source_ref": "main"}, "source_ref"),
        ({"oidc_subject": "repo:example-owner/example-strategies:ref:refs/heads/other"},
         "oidc_subject"),
        ({"oidc_subject": "example-owner/example-strategies"}, "oidc_subject"),
        ({"oidc_issuer": "https://issuer.example"}, "oidc_issuer"),
        ({"oidc_audience": "other"}, "oidc_audience"),
    ],
)  # fmt: skip
def test_a_malformed_or_inconsistent_identity_is_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _identity(**overrides)


def test_the_subject_must_follow_a_changed_source_ref() -> None:
    identity = _identity()

    with pytest.raises(ValueError, match="oidc_subject"):
        replace(identity, source_ref="refs/tags/v1")


@pytest.mark.parametrize(
    "repository",
    ["ghcr.io/example-owner/example-strategies/strategy-releases", "ghcr.io/owner/name"],
)
def test_ghcr_repositories_are_accepted_by_shape(repository: str) -> None:
    assert require_ghcr_repository(repository) == repository


@pytest.mark.parametrize(
    "repository",
    ["docker.io/owner/name", "ghcr.io/Owner/name", "ghcr.io/owner", "ghcr.io/-owner/name", ""],
)
def test_other_registries_and_malformed_names_are_refused(repository: str) -> None:
    with pytest.raises(ValueError, match="not a GHCR repository"):
        require_ghcr_repository(repository)


def test_a_registry_client_works_on_its_own_repository_only() -> None:
    registry = GhcrRegistryV1(
        actor="actor", token="token", repository="ghcr.io/example-owner/example-strategies/x"
    )

    with pytest.raises(OciPublicationError, match="differs from the one this client is for"):
        registry.get_blob("ghcr.io/example-owner/another", "sha256:" + "0" * 64)


def test_a_registry_client_needs_a_ghcr_repository() -> None:
    with pytest.raises(ValueError, match="not a GHCR repository"):
        GhcrRegistryV1(actor="actor", token="token", repository="docker.io/owner/name")


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [
        ("trend-supertrend-0.1.0rc5", True),
        ("trend-supertrend-2.1.1", True),
        ("momentum-my_idea-0.1.0", True),
        ("latest", False),
        ("trend-supertrend", False),
        ("Trend-SuperTrend-1.0", False),
    ],
)
def test_discovery_tags_name_the_strategy_and_its_version(tag: str, accepted: bool) -> None:
    assert bool(_TAG_RE.fullmatch(tag)) is accepted

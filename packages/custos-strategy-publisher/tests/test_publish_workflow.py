"""The reusable workflow gives each job only the credentials its step needs.

Building runs the producer's code through the type checker, so the jobs that
build hold no write credential and no OIDC token; only the job that signs and
publishes does, and it rebuilds nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/publish-strategy-release.yml"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _jobs() -> dict[str, dict[str, object]]:
    return _workflow()["jobs"]  # type: ignore[return-value]


def test_it_is_called_by_a_producer_and_grants_nothing_by_default() -> None:
    workflow = _workflow()

    assert set(workflow["on"]) == {"workflow_call"}  # type: ignore[arg-type]
    assert workflow["permissions"] == {}


def test_each_job_holds_only_its_own_credentials() -> None:
    jobs = _jobs()

    assert jobs["identity"]["permissions"] == {"id-token": "write"}
    assert jobs["candidate"]["permissions"] == {"contents": "read", "packages": "read"}
    assert jobs["publish"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
        "packages": "write",
    }


def test_only_the_candidate_job_builds_and_only_the_publish_job_signs() -> None:
    raw = {
        name: "\n".join(step.get("run", "") for step in job["steps"])  # type: ignore[union-attr]
        for name, job in _jobs().items()
    }

    assert raw["candidate"].count("custos-strategy-release build") == 1
    assert "custos-strategy-release candidate" in raw["candidate"]
    assert "--extra sign" not in raw["candidate"]
    assert "custos-strategy-release build" not in raw["publish"]
    assert "--extra build" not in raw["publish"]
    publish = raw["publish"]
    assert publish.index("release assemble") < publish.index("release preflight")
    assert publish.index("release preflight") < publish.index("release publish")


def test_registry_write_credentials_reach_only_the_publish_step() -> None:
    publish = _jobs()["publish"]
    steps = publish["steps"]  # type: ignore[index]
    holders = [step["name"] for step in steps if "GHCR_TOKEN" in step.get("env", {})]

    assert holders == ["Publish and read back by digest"]
    assert "GHCR_TOKEN" not in publish.get("env", {})  # type: ignore[union-attr]
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_the_publisher_is_checked_out_at_the_commit_the_token_names() -> None:
    for name in ("candidate", "publish"):
        checkout = next(
            step
            for step in _jobs()[name]["steps"]  # type: ignore[index]
            if step.get("with", {}).get("path") == "publisher"
        )
        assert checkout["with"]["ref"] == "${{ needs.identity.outputs.publisher-sha }}"


def test_every_action_is_pinned_to_a_commit() -> None:
    for job in _jobs().values():
        for step in job["steps"]:  # type: ignore[union-attr]
            uses = step.get("uses")
            if uses:
                assert len(uses.split("@")[1]) == 40, uses

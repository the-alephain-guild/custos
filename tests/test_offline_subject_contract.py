"""The one subject both repositories have to agree on, pinned so they cannot drift.

The offline lane publishes observed state; a probe in another repository waits on
it. Nothing in either repository fails when those two stop matching — the probe
simply waits until it times out, which reads like a slow runner rather than a
wrong address. Both of the last breaks in this lane were invisible until someone
ran it, so the agreement is written down here instead.

The pinned template is the contract. The consumer checkout is cross-checked when
it happens to be present, because this repository is cloned on its own and its
tests must pass without it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custos.offline.spec import offline_subject

CONSUMER_SUBJECT = "arx.{tenant}.deployment_status.{runner_label}.{spec_id}"
CONSUMER_DEFAULT_LABEL = "ps-supertrend"

_CONSUMER_COMPOSE = (
    Path(__file__).resolve().parents[3]
    / "alchymia-labs/philosophers-stone/deploy/custos/docker-compose.yaml"
)


def test_the_lane_publishes_the_subject_the_consumer_waits_on() -> None:
    published = offline_subject("local", "deployment_status", CONSUMER_DEFAULT_LABEL, "spec-1")

    assert published == CONSUMER_SUBJECT.format(
        tenant="local", runner_label=CONSUMER_DEFAULT_LABEL, spec_id="spec-1"
    )


def test_a_uuid_identity_alone_would_not_reach_that_subject() -> None:
    """Why the label exists: a v1 runner_id is a UUID, and the probe names a label."""

    identity = "11111111-1111-4111-8111-111111111111"

    published = offline_subject("local", "deployment_status", identity, "spec-1")

    assert published != CONSUMER_SUBJECT.format(
        tenant="local", runner_label=CONSUMER_DEFAULT_LABEL, spec_id="spec-1"
    )


def test_the_label_reaches_the_subject_from_the_command_line() -> None:
    """`--runner-label` is the flag the consumer sets; nothing else carries it."""

    import argparse

    from custos.cli.subcommands import start

    parser = argparse.ArgumentParser()
    start.register(parser.add_subparsers())

    parsed = parser.parse_args(
        [
            "start",
            "--reconcile-strategy-id",
            "supertrend-sandbox",
            "--runner-label",
            "ps-supertrend",
        ]
    )

    assert parsed.runner_label == CONSUMER_DEFAULT_LABEL
    assert parser.parse_args(["start"]).runner_label is None


@pytest.mark.skipif(
    not _CONSUMER_COMPOSE.is_file(), reason="the consumer checkout is not beside this one"
)
def test_the_consumer_still_subscribes_to_the_pinned_shape() -> None:
    """Reads the other repository when it is here, so drift surfaces as a red test."""

    compose = _CONSUMER_COMPOSE.read_text(encoding="utf-8")
    subjects = re.findall(r"arx\.\$\{TENANT_ID[^}]*\}\.deployment_status\.[^\s]+", compose)

    assert subjects, "the consumer no longer subscribes to any deployment_status subject"
    normalised = [
        re.sub(r"\$\{(\w+)(?::-[^}]*)?\}", r"<\1>", subject.strip()) for subject in subjects
    ]
    assert normalised == [
        CONSUMER_SUBJECT.format(
            tenant="<TENANT_ID>", runner_label="<RUNNER_ID>", spec_id="<SPEC_ID>"
        )
    ], f"the consumer's subject changed shape: {normalised}"

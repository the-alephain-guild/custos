"""Sole production V1 release workflow contract."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from custos.cli.subcommands import _build_parser

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MAKEFILE = ROOT / "Makefile"
DOCKERFILE = ROOT / "Dockerfile"
VERIFICATION_RULE = ROOT / ".claude" / "rules" / "verification.md"
HISTORICAL_LESSONS = ROOT / ".claude" / "rules" / "historical-lessons.md"
EXPECTED_JOBS = ("build-docker", "verify-release", "publish-pypi", "release-notes")


def _read() -> str:
    return WORKFLOW.read_text()


def _top_level_subcommands() -> list[str]:
    parser = _build_parser()
    actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    return sorted(actions[0].choices)


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_workflow_permissions_cover_ghcr_and_keyless_signing() -> None:
    text = _read()
    assert "\npermissions:\n" in text
    assert "contents: read" in text
    assert "packages: write" in text
    assert "id-token: write" in text


def test_workflow_has_only_the_current_release_jobs() -> None:
    text = _read()
    jobs_section = text.split("\njobs:\n", 1)[1]
    jobs = re.findall(r"^  ([a-z][a-z0-9-]+):\n", jobs_section, flags=re.MULTILINE)
    assert tuple(jobs) == EXPECTED_JOBS


def test_stable_tag_and_manual_production_sources_are_explicit() -> None:
    text = _read()
    assert "v[0-9]+.[0-9]+.[0-9]+" in text
    assert "release_id:" in text
    assert "required: true" in text
    assert "manual production publication must use main" in text
    assert "- 'v*'" not in text and '- "v*"' not in text


def test_every_action_is_commit_pinned() -> None:
    uses = re.findall(r"^\s+-?\s*uses:\s*([^\s#]+)", _read(), flags=re.MULTILINE)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", value) for value in uses)


def test_runtime_is_fully_tested_before_wheels_and_image_publication() -> None:
    text = _read()
    install = text.index("--package custos-runner --extra dev --extra nautilus")
    runtime_lock = text.index("make check-runtime-lock", install)
    all_tests = text.index("uv run --extra nautilus pytest tests/", runtime_lock)
    authority = text.index("scripts/check-authority-docs.py", all_tests)
    wheel = text.index("make dist")
    wheel_signature = text.index(".github/workflows/scripts/sign-wheel.sh")
    image = text.index("docker/build-push-action@")
    assert install < runtime_lock < all_tests < authority < wheel < wheel_signature < image


def test_source_date_epoch_is_integer_git_seconds() -> None:
    text = _read()
    assert 'SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"' in text
    assert not re.search(r"SOURCE_DATE_EPOCH:\\s*\\$\\{\\{", text)


def test_image_is_single_platform_with_sbom_and_provenance() -> None:
    text = _read()
    assert "platforms: linux/amd64" in text
    assert "provenance: mode=max" in text
    assert "sbom: true" in text


def test_exact_digest_runtime_gate_precedes_signature() -> None:
    text = _read()
    build = text.index("Build and push exact linux amd64 image")
    digest = text.index("CUSTOS_TEST_IMAGE:", build)
    runtime = text.index("make verify-runtime-existing", digest)
    sign = text.index("cosign sign --yes", runtime)
    assert build < digest < runtime < sign


def test_discovery_tags_are_immutable_but_never_runtime_authority() -> None:
    text = _read()
    assert "release tag already exists and cannot be repointed" in text
    assert ":latest" not in text
    assert "imagetools create" not in text
    assert "IMAGE_REF: ${{ env.IMAGE_NAME }}@${{ needs.build-docker.outputs.digest }}" in text


def test_owner_receipt_is_exact_and_machine_parseable() -> None:
    text = _read()
    for fragment in (
        '"receipt_id": "CUSTOS-V1-TEAM-IMAGE-PUBLICATION-V1"',
        '"status": "IMAGE_PUBLISHED_ATTESTED"',
        '"image_key": "custos"',
        '"platform": "linux/amd64"',
        '"workflow_file": os.environ["WORKFLOW_FILE"]',
        '"cosign_version": os.environ["COSIGN_VERSION"]',
        '"production_ready": False',
        '"provenance"',
        '"sbom"',
        '"sigstore-bundle"',
        '"sigstore-signature-verification"',
    ):
        assert fragment in text


def test_sigstore_verification_binds_workflow_identity() -> None:
    text = _read()
    for fragment in (
        "--certificate-identity",
        "--certificate-oidc-issuer",
        "--certificate-github-workflow-repository",
        "--certificate-github-workflow-ref",
        "--certificate-github-workflow-sha",
        "--certificate-github-workflow-name",
        "--certificate-github-workflow-trigger",
    ):
        assert fragment in text


def test_published_runtime_probes_every_real_cli_command() -> None:
    text = _read()
    for command in _top_level_subcommands():
        assert re.search(rf"for command in .*\b{re.escape(command)}\b", text)


def test_dockerfile_uses_locked_inputs_and_one_production_state_root() -> None:
    text = DOCKERFILE.read_text()
    assert "docker/runtime-requirements.lock" in text
    assert "--require-hashes" in text
    assert "--no-deps" in text
    assert 'CMD ["start", "--production-state-root", "/home/custos/.arx"]' in text


def test_verify_runtime_target_gates_the_image_contract() -> None:
    assert "verify-runtime: test-docker" in MAKEFILE.read_text()


def test_every_makefile_test_path_exists() -> None:
    text = MAKEFILE.read_text()
    referenced = set(re.findall(r"tests/[\w/]+\.py", text))
    missing = sorted(path for path in referenced if not (ROOT / path).exists())
    assert referenced
    assert not missing


def test_release_identity_prevention_is_documented() -> None:
    verification = " ".join(VERIFICATION_RULE.read_text().split())
    lessons = " ".join(HISTORICAL_LESSONS.read_text().split())
    assert "same verified digest" in verification
    assert "must not rebuild" in verification
    assert "C3" in lessons
    assert "SOURCE_DATE_EPOCH" in verification
    assert "C7" in lessons

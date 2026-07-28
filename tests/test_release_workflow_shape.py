"""Release workflow shape: permissions, job DAG, and the post-publish gate.

A release workflow only really runs at tag push, which is the worst possible
moment to discover it is wrong. This locks the shape locally with plain-text
assertions. That is weaker than parsing the YAML, but it keeps `pyyaml` out of
the default test environment, where it exists for nothing else.

What is asserted, and why each one was worth asserting:

- `permissions:` (plural) carrying `id-token: write`, `packages: write` and
  `contents: write` — the singular spelling is silently ignored.
- The full job DAG, by name.
- A stable-only tag pattern, so a pre-release tag cannot trigger a publish.
- `build-docker` needing both the wheel build and its signature, so the image
  is always built on the signed wheel rather than on whatever a later fetch
  returns.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from custos.cli.subcommands import _build_parser


def _top_level_subcommands() -> list[str]:
    parser = _build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return sorted(actions[0].choices)


WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
VERIFY_RELEASE = WORKFLOW.parent / "scripts" / "verify-release.sh"
MAKEFILE = WORKFLOW.parents[2] / "Makefile"
DOCKERFILE = WORKFLOW.parents[2] / "Dockerfile"
VERIFICATION_RULE = WORKFLOW.parents[2] / ".claude" / "rules" / "verification.md"
HISTORICAL_LESSONS = WORKFLOW.parents[2] / ".claude" / "rules" / "historical-lessons.md"

EXPECTED_JOBS = (
    "build-wheel",
    "sign-wheel",
    "build-docker",
    "sign-docker",
    "publish-pypi",
    "publish-ghcr",
    "verify-release",
    "release-notes",
)


def _read() -> str:
    return WORKFLOW.read_text()


def test_workflow_file_exists():
    assert WORKFLOW.exists(), f"missing workflow at {WORKFLOW}"


def test_permissions_plural_with_write_scopes():
    text = _read()
    # H2: plural `permissions:` at top level, exact scopes required by sigstore
    # + GHCR + release-notes.
    assert "\npermissions:\n" in text, "top-level `permissions:` block missing"
    assert "id-token: write" in text, "id-token: write required (sigstore OIDC)"
    assert "packages: write" in text, "packages: write required (GHCR push)"
    assert "contents: write" in text, "contents: write required (release notes)"


def test_workflow_has_eight_documented_jobs():
    text = _read()
    for name in EXPECTED_JOBS:
        assert f"\n  {name}:\n" in text, f"missing job `{name}` in workflow"


def test_stable_tag_pattern_only():
    """M6: stable-only tag pattern; `v*` wildcard would auto-publish RCs to
    the stable PyPI channel and pollute the tag series."""
    text = _read()
    assert "v[0-9]+.[0-9]+.[0-9]+" in text, "stable semver tag pattern missing"
    # Belt-and-braces: guard against the common regression of adding a bare
    # `v*` glob elsewhere in the trigger block.
    assert "- 'v*'" not in text and '- "v*"' not in text, (
        "wildcard tag pattern `v*` re-introduced; would auto-publish rc.* tags"
    )


def test_build_docker_needs_signed_wheel():
    """H1: docker image must build on the artifact from sign-wheel, not a
    PyPI-resolved one. `needs: [build-wheel, sign-wheel]` enforces the DAG."""
    text = _read()
    # Search for the build-docker `needs:` line + verify it references both
    # upstream jobs. Grep is intentionally permissive on formatting so we
    # don't hard-code YAML style.
    lines = text.splitlines()
    build_docker_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "build-docker:"), None
    )
    assert build_docker_idx is not None, "no `build-docker:` job block"
    # Scan the next 15 lines for the `needs:` line.
    window = "\n".join(lines[build_docker_idx : build_docker_idx + 15])
    assert "needs:" in window
    assert "build-wheel" in window
    assert "sign-wheel" in window


def test_release_verifies_clean_base_before_nautilus_runtime() -> None:
    """A preinstalled NT runtime must not mask the dev-only base gate."""
    text = _read()

    base_gate = text.index("make verify-base-clean")
    install_nt = text.index("make install-nt")
    verify_nt = text.index("make verify-nt")

    assert base_gate < install_nt < verify_nt


def test_source_date_epoch_is_an_integer_derivation() -> None:
    """The epoch must be seconds, and hatchling enforces that with int().

    The workflow used to pass the push payload's commit timestamp, which is an
    ISO 8601 string. hatchling raises `ValueError: invalid literal for int()`
    on it, so the very first release job would have failed -- unnoticed,
    because no tag has ever been pushed. A manual dispatch was worse: no push
    payload exists there, so the value was the empty string and the build was
    silently not reproducible at all.
    """
    text = _read()

    assert 'SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"' in text, (
        "the epoch must be derived as integer seconds, by the same command an "
        "auditor runs to reproduce the build"
    )
    # Not a ban on the payload timestamp itself: it is the correct value for
    # the OCI `image.created` label, which wants RFC 3339. What must not come
    # back is binding any workflow expression to the epoch.
    assert not re.search(r"SOURCE_DATE_EPOCH:\s*\$\{\{", text), (
        "SOURCE_DATE_EPOCH must not be bound to a workflow expression; "
        "derive it in the run block so it is integer seconds"
    )


def test_release_gates_complete_runtime_before_stable_tag_promotion() -> None:
    text = _read()

    runtime_gate = text.index("make verify-runtime-existing")
    stable_promotion = text.index("docker buildx imagetools create")

    assert runtime_gate < stable_promotion


def test_signed_wheel_precedes_candidate_build_and_runtime_gate() -> None:
    """The tested candidate must be built from the signed wheel artifact."""
    text = _read()

    signed_wheel = text.index("name: dist-signed", text.index("build-docker:"))
    candidate_build = text.index("id: build", text.index("build-docker:"))
    runtime_gate = text.index("make verify-runtime-existing", text.index("build-docker:"))

    assert signed_wheel < candidate_build < runtime_gate


def test_stable_tags_promote_the_runtime_verified_candidate_digest() -> None:
    """Stable tags must be aliases of the tested digest, never a rebuild."""
    text = _read()
    build_docker = text.index("build-docker:")
    candidate_tag = text.index("candidate-${{ github.sha }}", build_docker)
    runtime_gate = text.index("make verify-runtime-existing", build_docker)
    promotion = text.index("docker buildx imagetools create", build_docker)
    digest_binding = text.index("IMAGE_DIGEST: ${{ steps.build.outputs.digest }}", runtime_gate)

    assert candidate_tag < runtime_gate < digest_binding < promotion
    promotion_block = text[promotion : promotion + 700]
    assert "--prefer-index=false" in promotion_block
    assert '"${IMAGE_NAME}@${IMAGE_DIGEST}"' in promotion_block
    assert '--tag "${IMAGE_NAME}:v${VERSION}"' in promotion_block
    assert '--tag "${IMAGE_NAME}:latest"' in promotion_block


def test_runtime_gate_targets_candidate_digest() -> None:
    text = _read()
    build_docker = text.index("build-docker:")
    runtime_gate = text.index("make verify-runtime-existing", build_docker)
    gate_block = text[runtime_gate - 300 : runtime_gate + 300]

    assert "CUSTOS_TEST_IMAGE" in gate_block
    assert "${{ env.IMAGE_NAME }}@${{ steps.build.outputs.digest }}" in gate_block


def test_runtime_gate_is_not_followed_by_an_image_rebuild() -> None:
    text = _read()
    build_docker = text.index("build-docker:")
    runtime_gate = text.index("make verify-runtime-existing", build_docker)
    build_job_end = text.index("# --- 4/8", runtime_gate)
    after_gate = text[runtime_gate:build_job_end]

    assert "uses: docker/build-push-action" not in after_gate
    assert "docker build " not in after_gate


def test_release_publishes_version_and_latest_image_tags() -> None:
    text = _read()

    assert '--tag "${IMAGE_NAME}:v${VERSION}"' in text
    assert '--tag "${IMAGE_NAME}:latest"' in text


def test_publish_ghcr_declares_every_needs_output_source() -> None:
    text = _read()
    start = text.index("  publish-ghcr:")
    end = text.index("  verify-release:", start)
    block = text[start:end]

    assert "needs: [build-wheel, build-docker, sign-docker]" in block
    assert "${{ needs.build-wheel.outputs.version }}" in block


def test_verify_runtime_target_covers_docker_and_standalone_contracts() -> None:
    text = MAKEFILE.read_text()

    assert "verify-runtime: test-docker" in text
    assert "tests/integration/test_standalone_runtime.py" in text


def test_post_publish_verifies_complete_runtime_contract() -> None:
    text = VERIFY_RELEASE.read_text()

    required_fragments = (
        '"${IMAGE_NAME}:v${VERSION}" --help',
        '"${IMAGE_NAME}:v${VERSION}" vault put --help',
        "import nautilus_trader, yaml",
        'sops "${IMAGE_NAME}:v${VERSION}" --version',
        'age "${IMAGE_NAME}:v${VERSION}" --version',
        'cosign verify "${IMAGE_NAME}:v${VERSION}"',
        "{{.Config.User}}",
    )
    for fragment in required_fragments:
        assert fragment in text, f"post-publish runtime gate missing: {fragment}"


def test_post_publish_command_matrix_matches_the_real_cli() -> None:
    """Derived from the parser, because a hard-coded list goes stale silently.

    It did: the gate probed `deployment validate`, which was removed from the
    CLI, so the published image would have been checked with a command that
    exits 2 — aborting the post-publish verify after the stable tags were
    already public. Three commands that do exist were never probed at all.
    """
    text = VERIFY_RELEASE.read_text()

    for name in _top_level_subcommands():
        fragment = f'"${{IMAGE_NAME}}:v${{VERSION}}" {name} --help'
        assert fragment in text, f"post-publish gate does not probe `{name}`"

    probed = set(re.findall(r'"\$\{IMAGE_NAME\}:v\$\{VERSION\}" ([a-z][a-z-]*) --help', text))
    unknown = sorted(probed - set(_top_level_subcommands()))
    assert not unknown, f"post-publish gate probes commands the CLI does not have: {unknown}"


def test_dockerfile_consumes_the_frozen_runtime_lock() -> None:
    text = DOCKERFILE.read_text()

    assert "docker/runtime-requirements.lock" in text
    assert "--require-hashes" in text
    assert "--no-deps" in text


def test_release_identity_prevention_is_documented() -> None:
    verification = " ".join(VERIFICATION_RULE.read_text().split())
    lessons = " ".join(HISTORICAL_LESSONS.read_text().split())

    assert "same verified digest" in verification
    assert "must not rebuild" in verification
    assert "C3" in lessons
    assert "artifact identity gate" in lessons

    # The gate assertions themselves are derived rather than hard-coded, which
    # is only obvious to someone who knows why. Losing the reason is how the
    # hard-coded matrix comes back.
    assert "Release gate assertions" in verification
    assert "SOURCE_DATE_EPOCH" in verification
    assert "C7" in lessons

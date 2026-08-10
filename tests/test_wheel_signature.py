"""Plan 12 T3 contract: sigstore keyless wheel signing (FM1).

The signing itself needs a GitHub Actions OIDC token, so we cannot exercise
the real signing flow locally. This test is deliberately ``@pytest.mark.ci_only``
so it skips outside of CI. In CI, after ``sign-wheel.sh`` has run, every
wheel under ``dist/`` must have a matching ``.sigstore`` bundle and pass
``sigstore verify identity``.

Two layers cover FM1:

- Layer 2 (this test): ``.sigstore`` bundle exists + ``sigstore verify``
  succeeds against the tag-driven cert-identity.
- Layer 3 (``verify-release.sh``): post-publish, re-download the wheel from
  PyPI and verify against the same cert-identity (independent smoke).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DIST = Path(__file__).resolve().parent.parent / "dist"
SIGN_SCRIPT = Path(__file__).resolve().parent.parent / ".github/workflows/scripts/sign-wheel.sh"


def _is_ci_with_oidc() -> bool:
    """Return True iff we're running in a GitHub Actions job with the OIDC
    token available for sigstore keyless signing.

    ``ACTIONS_ID_TOKEN_REQUEST_URL`` is exported by the runner when a job
    grants ``id-token: write`` — the exact permission Plan 12 T4 sets on
    the release workflow.
    """
    return bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"))


def _release_certificate_ref(event_name: str, ref: str) -> str:
    if event_name == "workflow_dispatch":
        assert ref == "refs/heads/main", f"manual production ref is not main: {ref!r}"
    elif event_name == "push":
        assert ref.startswith("refs/tags/v"), f"stable release ref is not a v tag: {ref!r}"
    else:
        raise AssertionError(f"unsupported release trigger: {event_name!r}")
    return ref


@pytest.mark.parametrize(
    ("event_name", "ref"),
    (("workflow_dispatch", "refs/heads/main"), ("push", "refs/tags/v0.3.0")),
)
def test_release_certificate_ref_is_exact_for_each_supported_trigger(
    event_name: str, ref: str
) -> None:
    assert _release_certificate_ref(event_name, ref) == ref


@pytest.mark.parametrize(
    ("event_name", "ref"),
    (("workflow_dispatch", "refs/heads/feature"), ("push", "refs/heads/main")),
)
def test_release_certificate_ref_rejects_non_release_refs(event_name: str, ref: str) -> None:
    with pytest.raises(AssertionError):
        _release_certificate_ref(event_name, ref)


def test_signing_script_emits_bundle_evidence_not_a_detached_signature() -> None:
    source = SIGN_SCRIPT.read_text(encoding="utf-8")
    assert 'sigstore sign --bundle "${bundle}" "${whl}"' in source
    assert "--output-signature" not in source


@pytest.mark.ci_only
def test_every_wheel_has_sigstore_bundle():
    if not _is_ci_with_oidc():
        pytest.skip("sigstore keyless requires GitHub Actions OIDC (ACTIONS_ID_TOKEN_REQUEST_URL)")
    if not DIST.exists():
        pytest.fail("dist/ directory missing; run `make dist` (or `uv build`) first")
    wheels = sorted(DIST.glob("*.whl"))
    assert wheels, f"no wheels under {DIST}; run `make dist` (or `uv build`) first"
    for whl in wheels:
        bundle = whl.with_suffix(whl.suffix + ".sigstore")
        assert bundle.exists(), (
            f"missing sigstore bundle for {whl.name}; "
            f"run `bash .github/workflows/scripts/sign-wheel.sh`"
        )


@pytest.mark.ci_only
def test_sigstore_verify_passes_for_every_wheel():
    if not _is_ci_with_oidc():
        pytest.skip("sigstore keyless requires GitHub Actions OIDC")
    if shutil.which("sigstore") is None:
        pytest.skip("sigstore CLI not on PATH (install with `uv sync`)")
    wheels = sorted(DIST.glob("*.whl"))
    assert wheels, f"no wheels under {DIST}"
    # sigstore-python 3.x default: bundle written as `<artifact>.sigstore`; the
    # ``verify identity`` subcommand walks the sibling bundle by convention.
    # `cert-identity` and `cert-oidc-issuer` must be pinned so the verify
    # cannot pass on a mis-issued cert (Plan 12 FM8 key-rotation drift).
    ref = _release_certificate_ref(
        os.environ.get("GITHUB_EVENT_NAME", ""),
        os.environ.get("GITHUB_REF", ""),
    )
    repo = os.environ.get("GITHUB_REPOSITORY", "the-alephain-guild/custos")
    cert_identity = f"https://github.com/{repo}/.github/workflows/release.yml@{ref}"
    for whl in wheels:
        bundle = whl.with_suffix(whl.suffix + ".sigstore")
        proc = subprocess.run(
            [
                "sigstore",
                "verify",
                "identity",
                "--bundle",
                str(bundle),
                "--cert-identity",
                cert_identity,
                "--cert-oidc-issuer",
                "https://token.actions.githubusercontent.com",
                str(whl),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"sigstore verify failed for {whl.name}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )

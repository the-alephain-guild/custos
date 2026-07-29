#!/usr/bin/env bash
# Post-publish release verification.
#
# Independent Layer-3 smoke on top of the CI job's own build. Downloads the
# wheel from PyPI, downloads the image from GHCR, verifies both signatures
# against the tag-driven cert identity, then repeats the minimum complete
# runtime contract against the published artifact.
#
# Usage: bash .github/workflows/scripts/verify-release.sh <version>
#   e.g. bash .github/workflows/scripts/verify-release.sh 0.3.0

set -euo pipefail

VERSION="${1:?version required (e.g. 0.3.0)}"

# Allow the repository owner to be overridden via env (useful for
# fork-based verification runs). Defaults to the canonical Alephain Guild
# path so an operator running this locally against a public release doesn't
# need extra flags.
: "${GITHUB_REPOSITORY:=the-alephain-guild/custos}"
: "${IMAGE_NAME:=ghcr.io/${GITHUB_REPOSITORY}}"

CERT_IDENTITY="https://github.com/${GITHUB_REPOSITORY}/.github/workflows/release.yml@refs/tags/v${VERSION}"
CERT_OIDC_ISSUER="https://token.actions.githubusercontent.com"

echo "== Layer 3: pull + verify wheel signature =="
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
pip download "custos-runner==${VERSION}" --no-deps -d "${TMP}"

if ! command -v sigstore >/dev/null 2>&1; then
    echo "sigstore CLI not on PATH; install with: uv sync --extra lts" >&2
    exit 2
fi

# Fetch the bundle from the GitHub Release attachments; PyPI hosts the wheel
# but not the .sigstore bundle.
whl_name="$(basename "${TMP}"/custos_runner-*.whl)"
bundle_url="https://github.com/${GITHUB_REPOSITORY}/releases/download/v${VERSION}/${whl_name}.sigstore"
curl -fsSL "${bundle_url}" -o "${TMP}/${whl_name}.sigstore"

sigstore verify identity \
    --bundle "${TMP}/${whl_name}.sigstore" \
    --cert-identity "${CERT_IDENTITY}" \
    --cert-oidc-issuer "${CERT_OIDC_ISSUER}" \
    "${TMP}/${whl_name}"

echo "== Layer 3: pull + verify image signature =="
docker pull "${IMAGE_NAME}:v${VERSION}"

cosign verify "${IMAGE_NAME}:v${VERSION}" \
    --certificate-identity "${CERT_IDENTITY}" \
    --certificate-oidc-issuer "${CERT_OIDC_ISSUER}"

echo "== Layer 3: published image command matrix =="
docker run --rm "${IMAGE_NAME}:v${VERSION}" --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" credential --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" deployment --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" enroll --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" health --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" nats --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" nats-transport --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" publish-capability --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" start --help >/dev/null
docker run --rm "${IMAGE_NAME}:v${VERSION}" vault --help >/dev/null
# One nested command too: a subparser can resolve while its children do not.
docker run --rm "${IMAGE_NAME}:v${VERSION}" vault put --help >/dev/null

echo "== Layer 3: published image Python runtime =="
docker run --rm --entrypoint python "${IMAGE_NAME}:v${VERSION}" \
    -c 'import nautilus_trader, yaml'

echo "== Layer 3: published image vault toolchain =="
docker run --rm --entrypoint sops "${IMAGE_NAME}:v${VERSION}" --version >/dev/null
docker run --rm --entrypoint age "${IMAGE_NAME}:v${VERSION}" --version >/dev/null

echo "== Layer 3: image runs as non-root =="
user="$(docker inspect --format '{{.Config.User}}' "${IMAGE_NAME}:v${VERSION}")"
if [ "${user}" = "root" ] || [ "${user}" = "0" ] || [ -z "${user}" ]; then
    echo "image published as root user (${user@Q}); refusing release" >&2
    exit 1
fi

echo "OK: v${VERSION} wheel + image signatures verify; image runs as ${user}."

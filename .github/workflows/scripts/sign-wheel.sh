#!/usr/bin/env bash
# Plan 12 T3: sigstore keyless wheel signing.
#
# Signs every wheel under `dist/*.whl` with sigstore-python 3.x, producing a
# sibling `<wheel>.sigstore` bundle. The GitHub Actions OIDC token is picked
# up automatically when this script runs in a workflow that grants
# `id-token: write` (see .github/workflows/release.yml).
#
# Usage: bash .github/workflows/scripts/sign-wheel.sh
#
# The sigstore-python 3.x CLI writes a bundle at `<artifact>.sigstore.json` by
# default. We pin `--bundle` so the release contract keeps its established
# `<artifact>.sigstore` asset name without confusing a bundle with a detached
# signature and certificate pair.

set -euo pipefail

if ! command -v sigstore >/dev/null 2>&1; then
    echo "sigstore CLI not found on PATH" >&2
    echo "install with: uv sync --extra lts   (or: pip install 'sigstore>=3.0,<4.0')" >&2
    exit 2
fi

if [[ ! -d dist ]]; then
    echo "dist/ directory not found; run 'uv build' first" >&2
    exit 2
fi

shopt -s nullglob
wheels=(dist/*.whl)
if (( ${#wheels[@]} == 0 )); then
    echo "no wheels under dist/; run 'uv build' first" >&2
    exit 2
fi

# Plan 12 H6 verification note: this loop assumes the sigstore-python 3.x
# `sign` command still accepts `--bundle <path> <artifact>`. Fail loudly
# rather than silently produce detached or unsigned wheel evidence.
sigstore sign --help >/tmp/sigstore-help.$$ 2>&1 || true
if ! grep -q -- "--bundle" /tmp/sigstore-help.$$; then
    echo "sigstore sign no longer supports --bundle flag" >&2
    echo "-- captured 'sigstore sign --help' output --" >&2
    cat /tmp/sigstore-help.$$ >&2
    rm -f /tmp/sigstore-help.$$
    exit 2
fi
rm -f /tmp/sigstore-help.$$

for whl in "${wheels[@]}"; do
    bundle="${whl}.sigstore"
    echo "signing ${whl} -> ${bundle}"
    sigstore sign --bundle "${bundle}" "${whl}"
done

echo "signed ${#wheels[@]} wheel(s); bundles alongside each .whl"

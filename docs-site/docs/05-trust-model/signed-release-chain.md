---
title: "Signed Release Chain"
sidebar_position: 6
---


# Signed Release Chain

Wheel builds are byte-for-byte reproducible, so an auditor can rebuild from
source and compare hashes against a published artifact.

This is what makes the source being open worth anything. Reading the code tells
you what the runner would do if the binary you are running were built from it —
reproducibility is how you establish that it was. Without it, an audit covers a
source tree and the operator runs something else.

**Remote release is deferred** for 0.3.0. There is nothing published to compare
against yet; the current consumer gate is an image you build and verify
locally.

## The three knobs

1. **`SOURCE_DATE_EPOCH`** — a Unix timestamp (seconds) that hatchling
   uses in place of the host clock when stamping file mtimes into the
   wheel's ZIP metadata. Without this pin, every rebuild embeds "now"
   and the resulting wheels have different byte hashes even though the
   source is identical.
2. **`uv.lock`** — the committed lock file freezes every transitive
   dependency to a specific version + digest. `uv build` reads it via
   `[tool.uv].package = true` so a stale lock is caught immediately.
3. **`hatch_build.py`** — a custom `BuildHookInterface` subclass wired
   through `[tool.hatch.build.hooks.custom]`. hatchling ≥ 1.20 already
   honours `SOURCE_DATE_EPOCH` natively, so the hook body is a no-op —
   its job is to *log* whether the epoch is set (so an operator running
   `uv build` locally can see it engaged) and to be a stable place to
   grow real behaviour if hatchling regresses on native determinism.

## Manual reproduction (auditor workflow)

```bash
# 1. clone the repo at the release tag you want to verify
git clone https://github.com/the-alephain-guild/custos.git
cd custos
git checkout <release-tag>

# 2. pin the epoch to the tagger date at midnight UTC (or copy the value
#    the release workflow used, exposed as the tag's commit timestamp).
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct <release-tag>)"

# 3. build; the resulting wheel MUST hash-match the released wheel.
uv build --out-dir /tmp/verify
sha256sum /tmp/verify/*.whl
```

When a remote release exists, compare against its SHA256SUMS attachment.
A mismatch means either the epoch is wrong (check the release notes for
the exact value the workflow used), or the source has been tampered
with — in which case the sigstore attestation would also fail against
the cert-identity.

## Automated verification

`tests/test_reproducible_build.py` runs two `uv build` cycles with the epoch <!-- disclosure-ok: auditable source location, custos is open for exactly this -->
pinned and asserts the two wheels hash identically. It is marked slow because a
double build takes tens of seconds, but it is not deselected: `make verify`
runs it, so the property is checked on every release gate rather than only when
someone remembers to ask.

To run it alone:

```bash
uv run pytest tests/test_reproducible_build.py
```

A companion test, `test_wheel_bytes_differ_without_epoch`, asserts the
*opposite* — that dropping the epoch changes the bytes. It is
`xfail(strict=True)` because it does not: hatchling ≥ 1.20 is natively
deterministic, so an epoch-less rebuild already produces identical wheels.

That inversion is deliberate, and worth following. If a future hatchling
reintroduces host-clock leakage, this test starts passing — and a strict xfail
that passes is reported as a failure. So the day reproducibility silently
depends on the epoch pin again is the day the suite goes red, rather than a day
nobody notices. The pin is defence-in-depth, and this is how we would find out
it had become load-bearing.

## Docker image reproducibility

Docker image reproducibility is a separate workstream (buildkit
timestamp normalization is not stable across buildkit versions).
For current local 0.3.0 development, the image side of "audit the binary" is
served by:

- `make verify-local-v030` builds `custos-runner:v0.3.0`, injects
  `org.opencontainers.image.revision = <commit sha>`, and runs the Docker plus
  standalone NATS gates.
- The printed image ID and revision label provide local provenance evidence
  for downstream development.
- A remote release additionally re-pulls the published image and verifies the
  command matrix, the Nautilus and PyYAML imports, the `sops` and `age`
  executables, the readiness probe, the non-root identity, and the cosign
  signature against the published digest.

Bit-for-bit image reproducibility — pinning the build to a specific buildkit and
`SOURCE_DATE_EPOCH` combination — is not done. It is worth saying so rather than
implying the image has the same property the wheel does: today the image's
provenance is its revision label and the gate it passed, which is weaker.

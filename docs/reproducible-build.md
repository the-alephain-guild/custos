# Reproducible builds

`custos-runner` supports byte-for-byte reproducible wheel builds so an external
auditor can rebuild from source and compare hashes when a remote artifact is
published. This is the technical foundation for the Non-Custodial red line
"audit-able open source": the distributed wheel must be what the source audit
covers. **Remote release: deferred** for 0.3.0; the current consumer gate is a
locally built Docker image.

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

`tests/test_reproducible_build.py` runs two `uv build` cycles with the
epoch pinned and asserts hash equality. It's `@pytest.mark.slow`
because a double build takes tens of seconds; it is not part of
`make verify` but runs on the nightly CI job.

A companion test (`test_wheel_bytes_differ_without_epoch`) is
`xfail(strict=True)` today because hatchling ≥ 1.20 is natively
deterministic — an epoch-less rebuild already produces identical wheel
bytes on the currently pinned hatchling. This is why the epoch pin
here is *defence-in-depth* rather than the sole knob: it defends
against a future hatchling regression that reintroduces host-clock
leakage. If such a regression lands, the epoch-less test would then
correctly differ, the xfail would fire, and we'd notice.

## Docker image reproducibility

The production image consumes the sole runtime dependency set exported from
`uv.lock` into `docker/runtime-requirements.lock`. Every third-party
requirement is hash-pinned and installed with `pip --require-hashes --no-deps`.
The Python base is pinned by tag and multi-platform manifest digest.

`make dist` builds the runner, base toolkit, and Nautilus toolkit wheels from
one source checkout. The image installs exactly one of each local wheel with
dependency resolution disabled. Strategy artifacts are not baked into the
runner image; Crucible's signed `StrategyRelease` remains their runtime
authority.

Run `make runtime-lock` only when `uv.lock` intentionally changes. Both
`make dist` and the release workflow reject lock drift. The workflow signs all
three wheels for image construction while publishing only the `custos-runner`
distribution to PyPI.

BuildKit does not promise byte-identical image archives across versions, so the
release identity is the candidate OCI digest. CI validates that digest, attaches
SBOM and provenance, signs it with cosign, and promotes the same digest to
stable tags without rebuilding. Local development keeps
`make verify-local-v030` as the fast downstream consumer gate and records the
source revision in the image labels.

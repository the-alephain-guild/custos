---
title: "签名发布链"
sidebar_position: 6
---


# 签名发布链

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

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

`tests/test_reproducible_build.py` runs two `uv build` cycles with the <!-- disclosure-ok: auditable source location, custos is open for exactly this -->
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

Docker image reproducibility is a separate workstream (buildkit
timestamp normalization is not stable across buildkit versions).
For current local 0.3.0 development, the image side of "audit the binary" is
served by:

- `make verify-local-v030` builds `custos-runner:v0.3.0`, injects
  `org.opencontainers.image.revision = <commit sha>`, and runs the Docker plus
  standalone NATS gates.
- The printed image ID and revision label provide local provenance evidence
  for downstream development.
- A future remote release uses cosign and `verify-release.sh` to re-pull the image and verify the CLI command matrix,
  Nautilus/PyYAML imports, sops/age executables, readiness probe, non-root
  identity, and cosign signature against the published digest.

A follow-up plan (tracked in
[upgrade paths](/release-governance/upgrade-paths)) will pin the image
build to a specific buildkit + `SOURCE_DATE_EPOCH` combination for
bit-for-bit image reproducibility as well.

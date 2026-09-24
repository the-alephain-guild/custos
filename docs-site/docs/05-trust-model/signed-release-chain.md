---
title: "Signed release verification"
sidebar_position: 6
---

Verify the exact artifact you intend to run. Source review, wheel reproducibility, image signatures and deployed acceptance establish different properties.

Choose an artifact using the official release instructions and check [supported use](/release-governance/release-status). Do not assume a locally built image is approved for production.

## Wheel reproduction

Use the release's exact source revision, build tools and recorded epoch. The runner release workflow derives `SOURCE_DATE_EPOCH` from the source commit timestamp; use the epoch specified for the artifact being verified.

```bash
git checkout "$RELEASE_REF"
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
uv build --out-dir /tmp/custos-wheel-verify
sha256sum /tmp/custos-wheel-verify/*.whl
```

Compare with the published wheel's checksums using the matching build inputs. A mismatch needs investigation of revision, epoch, tool versions and build environment before attributing it to tampering. A source lock file alone does not prove the build backend environment is identical.

`hatch_build.py` records epoch use; hatchling provides the underlying timestamp behavior. Reproducibility tests build twice and compare bytes:

```bash
uv run pytest tests/test_reproducible_build.py
```

The suite also records expected behavior without an explicit epoch. Interpret its xfail/skip results in the tested toolchain rather than assuming all build environments have the same determinism.

## Image verification

A stable release tag names the same image digest that passed the full runtime gate. The release workflow builds one candidate, runs the gate against that digest, then promotes and signs the same digest; it does not rebuild between the gate and the stable tag.

```bash
make verify-local-v030
```

The local target checks the image runtime contract and revision label. It does not run a full signed deployment or standalone broker acceptance. Bit-for-bit Docker image reproducibility is not established by this target.

For a published image, verify the digest, signature identity, source revision and recorded runtime checks against that exact image. Candidate verification does not close deployed production acceptance. Keep artifact evidence attached to its recorded revision.

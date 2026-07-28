---
title: "SEMVER & LTS Commitment"
sidebar_position: 1
---


# SEMVER & LTS Commitment

This document is the authoritative statement of the Long-Term Support (LTS)
window, security patch SLA, release cadence, and key-rotation protocol for
`custos-runner`. It is deliberately hand-maintained (rather than generated
from an LTS-status page) at the 0.x stage — an automated status page is a
follow-up plan tracked in [upgrade paths](/release-governance/upgrade-paths).

The concrete numbers below are contractual — a change to any row must go
through a MINOR bump (loosening) or a MAJOR bump (tightening) plus a
matching `CHANGELOG.md` entry. See [`../CHANGELOG.md`](https://github.com/the-alephain-guild/custos/blob/main/CHANGELOG.md) and
the SEMVER contract table below for the full envelope.

## The SEMVER contract

What counts as which bump is fixed, so that a version number tells you whether
you have to do anything before upgrading.

| Bump | Covers | Explicitly **not** covered |
|---|---|---|
| **MAJOR** | Cutting the gateway contract to a new version directory; renaming or removing a console-script entry point; tightening `requires-python`; renaming or removing an `ExecutionEngineProtocol` Tier-1 field or method; a breaking change to the `~/.arx/` state layout; **adding a required gateway-contract field** | Anything that leaves existing callers working |
| **MINOR** | Additive only: a new entry point, a new **optional** gateway-contract field, a new optional-dependency extra, a new subcommand, a new CI job that does not replace an old one; a dependency **major** upgrade | Making an existing field required, removing a field, renaming an entry point, tightening `requires-python` |
| **PATCH** | Fixes, security patches, documentation corrections, internal refactors with no externally observable change; dependency **patch/minor** upgrades with `uv.lock` updated in the same commit | Any change to a field, entry point, schema or documented semantic; a dependency major upgrade |

Two rows deserve a note because integrators get them wrong.

**Adding an optional field is MINOR, adding a required one is MAJOR.** The
schemas are strict (`additionalProperties: false`), so a new field is rejected by
a consumer that has not been updated — an optional addition still needs both
sides deployed. A *required* addition is worse: an old producer that does not
send it fails validation outright, which is a break rather than a coordination
problem.

**A dependency major upgrade is MINOR, not PATCH.** It can pull a transitive
breaking change into your environment even when none of our own surfaces moved,
so it does not belong in a bump you are meant to be able to take blindly.

## EOL Window

Each minor release line (`0.Y.x`) is supported for **at least 12 months**
from the first `0.Y.0` tag. During that window the line receives security
patches (see next section) and — best-effort — bug-fix patches. EOL is
announced at least 30 days in advance in the GitHub release notes and
copied to the `CHANGELOG.md` `### Deprecated` section (audit-non-silence).

| Minor line | First release | EOL |
| ---------- | ------------- | --- |
| 0.3.x      | 2026-07-12    | 2027-07-12 (best-effort ≥ 12 months) |
| 0.2.x      | 2026-07-11    | 2027-07-11 (best-effort ≥ 12 months) |

Additional lines will be appended as they cut. Each row is a hard commitment
— a line is not dropped before its published end-of-life date.

## Security Patch SLA

Security fixes ship as a patch release (`0.Y.z+1`) within **30 days** of
public CVE disclosure (best-effort; a note in this doc's Deviations log
covers any miss).

- Report via [GitHub Security Advisories](https://github.com/the-alephain-guild/custos/security/advisories)
  — see [`SECURITY.md`](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md) for the disclosure protocol.
- Public advisories go live within 24 hours of the patch release.
- Backport policy: security fixes land on every active LTS line. Critical functional-bug
  backports are assessed case by case and announced with the release.

## Release Cadence

Best-effort **quarterly** minor releases. The cadence is not a hard
contract — a missed quarter is annotated in the Deviations log below,
and the LTS window is measured from actual release dates, not from the
target cadence.

## Deprecation Grace

Any field, entry point, or observable behaviour marked `deprecated` in
one minor release stays available for at least the following minor
release (≥ 3 months in practice) before it can be removed. Every minor
release notes emit a reminder for still-deprecated items so nothing
falls off quietly (audit-non-silence).

## Key Rotation Protocol

Sigstore + cosign are keyless (OIDC-backed), so there is no "custos
signing key" to rotate. The rotation surface is the CI workflow's
`cert-identity` template — if the workflow file moves or the tag
naming scheme changes, existing bundles will no longer verify. Handle
that by:

1. Announce the identity change in the next release notes and in the
   `## [Unreleased]` section of `CHANGELOG.md`.
2. Ship a follow-up patch release whose bundles use the new identity.
3. Add a Deviations-log row here linking to the affected tag.

An identity break that only affects re-verification of prior tags
does *not* affect the artifact contents — auditors can still verify
via the tag-time cert-identity that was in effect when the tag was
cut. Verification instructions live in
[`../.github/workflows/scripts/verify-release.sh`](https://github.com/the-alephain-guild/custos/blob/main/.github/workflows/scripts/verify-release.sh).

## Upgrade Path

Concrete upgrade steps for each minor bump live in
[upgrade paths](/release-governance/upgrade-paths), including the 0.x → 1.0 promote
checklist (arx-side gateway wire ready + 3 consecutive minor releases
without breaking changes + gateway-contract v1 covered 100%).

## Follow-up

- Automated LTS status page (0.x → 1.0 timeline) — separate follow-up
  plan; not scoped into 0.3.0.
- Machine-readable EOL feed (`docs/lts-commitment.json`) — same follow-up.

## Deviations log

| Date | Line | Deviation | Notes |
| ---- | ---- | --------- | ----- |
| — | — | — | first entry appears here when a deviation ships |

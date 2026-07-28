---
title: "Security Policy"
sidebar_position: 5
---

# Security Policy

**[SECURITY.md](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md)** is the authoritative policy. This page
summarises it.

## Reporting

Report privately through GitHub Security Advisories:

**[Report a vulnerability](https://github.com/the-alephain-guild/custos/security/advisories/new)**

Please do not open a public issue for a suspected vulnerability. A public report
tells every operator running the affected version at the same time it tells us,
and they cannot act on it until a patch exists.

## What we commit to

| Commitment | Window |
|---|---|
| Acknowledge receipt | 72 hours |
| Ship a fix after confirmation | 30 days, best effort |
| Publish the advisory | within 24 hours of the patch release |

Security fixes land on **every** active support line, not only the newest. See
[SemVer and LTS](/release-governance/semver-lts) for which lines are active.

## What to include

The version you observed it on, what you did, what happened, and what you
expected. A reproduction is ideal but not required — a precise description of
the boundary you think is crossed is more useful than a partial exploit.

## Where the boundaries are

The trust model states four guarantees, and a report is most actionable when it
names which one it breaks:

| Guarantee | A finding here would show |
|---|---|
| [Keys never leave the host](/trust-model/keys-never-leave-the-host) | Credential or key material reaching a log, message or HTTP body |
| [Live execution is gated](/trust-model/live-execution-is-gated) | A path to real-venue execution that skips admission |
| [Safety survives a disconnect](/trust-model/safety-survives-disconnect) | Local protection that stops when upstream is unreachable |
| [Money arithmetic is exact](/trust-model/exact-money-arithmetic) | A float entering a money path |

## Auditing rather than reporting

If you want to check these claims rather than report a break in one, the
[audit checklist](/trust-model/audit-checklist) walks the same boundaries with
the commands and tests that cover each.

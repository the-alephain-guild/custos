---
title: "Contributing"
sidebar_position: 3
---

# Contributing

The full guide lives in the repository, next to the code and the hooks it
describes:

**[CONTRIBUTING.md](https://github.com/the-alephain-guild/custos/blob/main/CONTRIBUTING.md)**

## Before you start

Three things are worth knowing before you write anything, because they shape
what a reviewable change looks like here.

**The four guarantees are not negotiable.** Keys never leave the host, live
execution is always gated, safety survives a disconnect, and money arithmetic is
exact. A change that even appears to weaken one needs a design discussion before
code review, not during it. See [the red lines](/trust-model/red-lines).

**Source artifacts are English.** Comments, log strings, exception messages,
identifiers and commit messages. A pre-commit hook rejects newly added lines
containing CJK characters. Deployment hosts do not reliably render them, and log
output has to stay greppable.

**Tests come first.** Every behavioural change lands with a failing test, then
the minimal implementation. `make verify` is the gate a pull request has to
pass, and it is the same target CI runs.

## Some files cannot be edited casually

Parts of the source tree are pinned byte-for-byte by the authority asset
indexes. Editing one — even to fix a comment or reformat it — breaks the
evidence chain, and the failure surfaces as a size mismatch rather than as
anything that reads like a permissions problem. Check first:

```bash
grep -rho '"src/custos/[^"]*\.py"' docs/authority/ | sort -u
```

## Security-sensitive changes

If your change touches vault handling, network egress or key derivation, open a
private advisory **before** the pull request. See
[the security policy](/release-governance/security-policy).

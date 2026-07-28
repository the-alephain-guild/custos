---
title: "Contract Versioning"
sidebar_position: 4
---

# Contract Versioning

There is one version. V1 changes in place.

That is the whole policy, and it is narrower than most versioning schemes on
purpose. What follows is what it means in practice and what would have to be
true before a second version existed.

## Why not run two versions

A compatibility path means two parsers for the same message. Two parsers means
two behaviours, and the difference between them is only discovered by the
message that exercises the one you did not test. For a contract that authorises
trading with real money, that is a poor trade for the convenience of a slower
rollout.

So there are no predecessor parsers, no compatibility aliases, and no
negotiation step. A runner accepts exactly one shape, and if it does not
recognise what arrived it refuses rather than guessing.

## What "changes in place" costs

Every schema is `additionalProperties: false`, which makes both kinds of change
coordinated:

| Change | Effect |
|---|---|
| Add an optional field | The producer must not send it until every consumer is updated — an un-updated consumer rejects the unknown key rather than ignoring it |
| Add a required field | An un-updated producer stops validating immediately |
| Remove or rename a field | Both sides move together, or neither does |

Notice that "optional" buys less than it usually does. Under
`additionalProperties: true` an optional field is a free unilateral change;
under `false` it still requires both sides deployed. The strictness is what
makes an unknown field a detected error instead of a silently dropped one, and
that property is worth more here than the easy rollout.

## Why the change is a re-issue, not an edit

Several contract assets are recorded in the repository's authority index by
path, size and commit, and the fact batch schema also carries a digest sidecar.
Changing one of those files invalidates the record that says what was signed.

So a contract change is: update the schema, update the golden, update the
digest, re-issue the receipt. Any subset of that is a broken evidence chain,
which the test suite reports as a failure rather than a warning. This is
deliberate friction — it makes the cost of a contract change visible at the
moment someone proposes one.

## What V2 would require

Two things, and neither is a decision we can simply take:

1. **A real external production consumer** that has pinned the exact V1 bytes.
2. **An explicit migration window**, declared in advance.

Today no consumer receipt is pinned. Every receipt in the repository is in an
open or pending state — the producer side is complete, the independent consumer
acceptances are not. So V2 is not currently available, and that is a statement
about evidence rather than about preference: without a consumer pinned to V1,
there is nothing for a V2 to migrate *from*.

The empty `v2`, `v3` and `v4` directories are placeholders. Their existence is
not a plan, and reading them as a roadmap is a mistake worth avoiding.

## Two different clocks

The package version and the contract version move independently:

- **Package SemVer** — `custos-runner` releases, EOL windows, security patches.
  See [SemVer and LTS](/release-governance/semver-lts).
- **Contract version** — `v1`, and the rules on this page.

A MINOR package release can carry a V1 change, and a MAJOR package release does
not imply a V2. Reading a version bump as a contract event, or the reverse, is
the most likely way to misread a release note.

## Deprecation

There is no deprecation mechanism inside V1, because there is nothing to
deprecate *to* — a field is either in the contract or it is not. A field being
removed is a contract change, handled as above.

If that sounds inflexible, it is: the flexible version of this design is the
one where a producer and a consumer disagree about whether a field still means
what it used to, and neither of them finds out until a fill is wrong.

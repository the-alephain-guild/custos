---
title: "Artifact Signing & Verification"
sidebar_position: 2
---

# Artifact Signing & Verification

Before a single line of strategy code is imported, the runner has to be
convinced the artifact is the one that was released. This page is what it checks
and what it refuses.

## The claim being checked

A published artifact carries a detached Sigstore bundle: a signed in-toto
statement whose subjects are the exact digests of the artifact's members.

Verification answers one question — *were these exact bytes signed by the
workflow we expect, and is that signature in the transparency log?* Not "is
there a signature", which any attacker can also satisfy.

## What must hold

Verification is offline against the bundle and fails closed at every step:

| Step | Rejected if |
|---|---|
| Bundle read | Not a stable regular file |
| Bundle parse | Malformed, or the injected trust root is invalid |
| Subjects | The required digests are not all present, or a subject is duplicated |
| Identity | The certificate does not match an accepted workflow identity and issuer |
| Repository | The identity's source repository coordinate does not match |
| DSSE | The payload signature does not verify |
| Transparency | The bundled log proof does not verify |

Duplicate JSON keys are rejected rather than last-one-wins. A payload that
parses differently in two implementations is a payload two parties can disagree
about while both believing they verified it.

## Identity, not just signature

An accepted identity names the **workflow**, its **issuer** and its **source
repository** together. All three must match.

That combination is what makes the check meaningful. A valid signature from
some other workflow, or from the expected workflow in a fork, is still a
signature — it just is not the one that authorises this artifact.

## Trust roots cannot be chosen by the artifact

The trusted root and the accepted identities come from signed, immutable local
release configuration:

| Flag | Supplies |
|---|---|
| `--artifact-sigstore-trusted-root` | The trust root used for verification |
| `--artifact-release-policy-envelope` | The signed policy naming accepted identities |
| `--artifact-release-policy-key-id` | Expected policy signing key id |
| `--artifact-release-policy-public-key` | Key the policy is verified against |

Artifact metadata may *reference* a trust root; it can never *select* one. An
artifact that chose the authority verifying it would be verifying itself, and
the whole chain would prove nothing.

The policy itself is signed and verified before it is used, so "which identities
are acceptable" is not something a local file edit can change.

## What is not a verification path

None of the following is accepted in production, and each is excluded
deliberately rather than merely unimplemented:

- a skip or override flag;
- shelling out to `cosign`, or to a Python subprocess;
- a sidecar or HTTP verifier;
- a bundle that is merely structurally plausible.

The last one is the subtle one. A bundle that parses, has the right shape and
contains a signature is not a verified bundle — it is an unverified bundle that
looks reassuring.

## Ordering

```text
verify → safe extraction → activate → import
```

Verification and safe extraction both complete before any import. The loader
then proves the module it imported came from the activation root, and rejects a
module cached from a different activation.

That second check matters more than it looks: verifying bytes on disk proves
nothing if the import system serves a module it cached earlier from somewhere
else. See [artifact materialization](/toolkit/artifact-materialization).

## Current status

The verifier is implemented and the contract assets are pinned, but the
end-to-end artifact capability is **not** enabled: the daemon that would consume
it stays disabled while no verified artifact capability is present, and live
readiness is false.

That distinction is worth keeping straight when reading this page — the checks
described here exist and are tested; what is not yet true is that a production
runner is executing artifacts through them. See
[the strategy toolkit](/toolkit/overview) for the receipt state.

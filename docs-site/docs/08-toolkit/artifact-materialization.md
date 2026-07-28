---
title: "Artifact Materialization"
sidebar_position: 3
---

# Artifact Materialization

How a strategy artifact gets from a registry onto the runner, and what has to
hold before any of it is imported.

:::note This replaced "registry-mode loading"
Strategy loading by registry name is gone. It was a mode in which the runner
resolved a strategy by a mutable coordinate — which meant "which code ran" could
not be answered from the artifact alone. What exists now pulls by **digest**, so
the coordinate cannot select different bytes later.
:::

## Pull by digest, never by tag

Blobs are fetched at an exact `sha256` digest:

```text
https://{registry}/v2/{repository}/blobs/sha256:{digest}
```

A tag is a name that can be repointed. A digest is the content. Nothing in the
materialization path accepts a tag, so there is no window in which the artifact
you approved and the artifact you ran can differ.

## The registry must be on the allow-list

The runner holds an explicit set of permitted registry hostnames, normalised to
lower case and validated against a hostname pattern. A pull from anywhere else is
refused before a request is made.

Credentials are keyed by registry and must be a subset of that allow-list — you
cannot hold a credential for a registry you are not allowed to pull from. That
ordering matters: it makes "which registries can this runner reach" answerable
from configuration rather than from whatever credentials happen to be present.

Configure it with:

| Flag | Default |
|---|---|
| `--artifact-registry` | `ghcr.io` |
| `--artifact-registry-username` | — |
| `CUSTOS_ARTIFACT_REGISTRY_TOKEN` (environment) | — |

The token is read from the environment rather than taken as a flag, so it does
not appear in `ps` output.

## Pull-only transport

The OCI client is deliberately minimal and **pull-only**, with scoped bearer
auth. There is no push path, so a compromised runner cannot publish an artifact —
it can only fail to run one.

Every response is bounded by a maximum size, and the digest is verified against
the bytes received. A blob that does not hash to what was requested is discarded
rather than being cached and retried.

## Where the bytes go

```text
pull → quarantine → verify → activate (immutable root) → import
```

Quarantine comes first and activation is atomic. An artifact is never imported
from the location it was downloaded into, and a partially materialised artifact
cannot be activated because activation is the last step rather than a
side-effect of the first.

Directories are configurable:

| Flag | Purpose |
|---|---|
| `--artifact-cache-dir` | Downloaded blobs |
| `--artifact-quarantine-dir` | Staging before verification |
| `--artifact-activation-dir` | Immutable activation roots |

## Verification precedes import

Signature and attestation verification complete before any Python module is
imported. The loader then proves the module it imported originated under the
activation root, and rejects a module cached from a different activation.

That second check is the one people skip. Verifying the bytes on disk proves
nothing if the import system serves a module it cached earlier from somewhere
else.

For what is verified and how it fails closed, see
[the strategy toolkit](/toolkit/overview) and
[artifact signing](/toolkit/artifact-signing).

## Development source

There is a sandbox-only path for iterating on a strategy without a published
artifact, selected with `--development-artifact-root`. It is an explicit,
non-promotable union member — it cannot be used in `testnet` or `live`, and it
cannot be promoted into one.

It exists so that "I need to test a change quickly" never becomes an argument
for weakening the real path.

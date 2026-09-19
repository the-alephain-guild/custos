---
title: "Artifact materialization"
sidebar_position: 3
---

The signed release path downloads exact artifact bytes, verifies them and activates a local immutable root before import.

## Registry access

OCI blobs are pulled by digest:

```text
https://{registry}/v2/{repository}/blobs/sha256:{digest}
```

The registry must be allowed by local configuration. Credentials are scoped to allowed registries. The client is pull-only, uses scoped bearer authentication, bounds response sizes and checks received bytes against the requested digest.

| Input | Purpose |
|---|---|
| `--artifact-registry` | Permitted registry; default `ghcr.io` |
| `--artifact-registry-username` | Private registry username |
| `CUSTOS_ARTIFACT_REGISTRY_TOKEN` | Registry token, kept out of command arguments |

## Local stages

```text
pull -> quarantine -> verify and extract -> activate -> import
```

Cache, quarantine and activation directories have separate CLI flags. Verification uses the independently configured release policy and Sigstore root. Safe extraction and atomic activation complete before import. The loader checks module origin and rejects a cached module from a different activation.

Persist the activation and runtime state needed for restart recovery. A cache is not a replacement for authority or an accepted desired-state record.

## Development inputs

Signed-lane `DevelopmentSourceRefV1` is an explicit, content-addressed sandbox-only input selected through `--development-artifact-root`. It cannot be used in testnet/live or promoted into a production release.

The offline lane separately loads an operator-mounted directory in sandbox/testnet. Its directory hash records local content; it provides no signed-release assurance. See [offline testnet](/operator-guide/offline-testnet) and [artifact signing](/toolkit/artifact-signing).

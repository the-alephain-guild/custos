# Custos runner contract V1

This directory contains contracts owned and published by Custos. It does not
publish a DeploymentSpec schema: the canonical DeploymentSpec is owned
upstream, along with its exact-byte command schema, its golden and the producer
receipt. Publishing a schema for the runner's local view would invite a
producer to build against that view instead of the signed original.

Custos consumes only the two signed, runner-scoped command events:

- `DeploymentSpecReadyForRunner.<runner_id>.<deployment_instance_id>`;
- `DeploymentInstanceDesiredStateChanged.<runner_id>.<deployment_instance_id>`.

`CrucibleRunnerDeploymentCommandV1` verifies the exact subject, signed event
bytes, canonical digest, tenant, runner, instance, spec and generation. The
canonical payload contains one typed `execution_config`; it contains no source
path, artifact path, `code_hash` or generic `parameters` fallback.

That type name keeps a legacy spelling. It is the symbol in the source and in
the pinned authority assets, so it is reproduced verbatim here — a tidier name
in this file would point at something that does not exist.

Strategy code material is resolved from the authenticated upstream
`StrategyRelease` authority, verified locally against the signed release,
snapshot, artifact and manifest digests, activated atomically, then passed to
the engine as an `ActivatedEngineArtifactV1`. The engine never imports a path
from a command.

The explicit local-development alternative is `DevelopmentSourceRefV1`. Its
`source_sha256` uses the Custos-owned `sha256-canonical-directory-v1` profile:
start SHA-256 with the ASCII domain `CUSTOS-DEVELOPMENT-SOURCE-DIRECTORY-V1\0`,
then, for each non-empty safe POSIX relative path sorted by UTF-8 bytes, append
the 8-byte big-endian path length, path bytes, 8-byte big-endian content length
and exact content bytes. The directory must be
`<configured-root>/sha256/<source_sha256>`, contain only stable regular files and
no symlinks, and is accepted only for sandbox with `promotable=false`.

The reference is returned by the authenticated upstream preview-material
resolver; it is never carried in the signed command and never represented as a
`StrategyRelease`. Development evidence cannot satisfy testnet, live, promotion,
runtime-RC or production-readiness gates.

Runner lifecycle observations use the signed RunnerFact V1 outbox. The outbox
allocates `facts[].seq`; typed fact builders never supply it.

`deployment_instance_id` is the sole runtime primary key. The spec ID, spec
digest and generation are immutable fencing/provenance fields and never create
a second stream.

Readiness is fail closed. Until the authenticated StrategyRelease resolver and
its exact producer receipt are composed, `arx-runner start --reconcile` refuses
to start rather than selecting a legacy or unsigned path.

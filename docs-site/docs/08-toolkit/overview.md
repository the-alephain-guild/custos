---
title: "Strategy toolkit"
sidebar_position: 1
---

The toolkit defines the strategy execution ABI and the artifact metadata consumed by Custos. ARX supplies release selection and deployment intent; Custos verifies material before importing it.

## Packages

<!-- generated:packages -->

| Package | Source version | Python |
|---|---|---|
| `custos-strategy-toolkit` | `0.1.0` | `>=3.11` |
| `custos-strategy-toolkit-nautilus` | `0.1.0` | `>=3.12,<3.13` |

<!-- /generated:packages -->

The Nautilus package requires an exactly matching base toolkit version. Use [installation](/getting-started/installation) for the locked fork wheels and platform requirements.

## Execution ABI

The entry-point group is `alephain.strategy_runtime.v1`. An adapter receives a verified `StrategyExecutionContext`; runtime addressing uses `deployment_instance_id`. Spec id, digest and generation retain provenance and ordering.

Effective configuration is parsed with finite `Decimal` numbers, duplicate keys rejected, containers recursively frozen, and `effective_config_digest` recomputed. Adapters must use that supplied configuration without loading different defaults or mutating it.

`sha256-canonical-json-v1` uses compact UTF-8, recursively sorted object keys, preserved array order and finite Decimal values. Use the contract vectors when implementing another encoder.

## Artifact boundary

`StrategyArtifactRefV1` describes pre-sign executable and manifest bytes, runtime artifacts, SBOM and contract schema. Detached attestation material, approvals, release selection and deployment state are outside that reference.

The runner resolves the complete release BOM through the authenticated release resolver. It verifies all members and detached evidence, quarantines downloads, activates an immutable root and imports only after verification. Artifact metadata cannot choose its own trust root. See [signing](/toolkit/artifact-signing) and [materialization](/toolkit/artifact-materialization).

## Typing and extraction evidence

The historical extraction inventory records 241 files: 36 platform-neutral, 55 Nautilus-specific and 150 private vendor files. It describes that extraction revision, not the current source-file count.

The historical 75/289 type-error baseline was closed. The current `make toolkit-typecheck` target runs whole-package strict checks for the base and Nautilus packages and verifies the typing-closure evidence. Private third-party vendor code is outside the mypy scope and has separate parity/extraction checks.

```bash
make check-toolkit-extraction
make toolkit-typecheck
make check-authority
```

## Handoff and runtime status

The registered Nautilus 2 contract handoff is complete and toolkit RC7 is recorded. These establish contract/candidate evidence, not production readiness. Live execution remains disabled and deployed runtime acceptance is open. See the revision-scoped [release status](/release-governance/release-status).

V1 is the active first-production contract. Evolving source and internal contracts use Git review and CI. Historical receipts remain evidence for their recorded revision; update schemas and fixtures for intentional changes without rewriting old acceptance evidence.

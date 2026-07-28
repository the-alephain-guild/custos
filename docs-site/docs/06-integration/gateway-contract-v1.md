---
title: "Gateway Contract v1"
sidebar_position: 1
---

# Gateway Contract v1

The machine-readable contract lives in the repository under
`docs/gateway-contract/v1/`. This page says what is in it, and — just as
usefully — what is deliberately not.

## What Custos publishes a schema for

| Schema | Covers |
|---|---|
| `enrollment.schema.json` | Locally provisioned machine enrollment material |
| `runner_fact_batch_v1.schema.json` | The signed fact batch Custos emits |
| `strategy_artifact_ref_v1.schema.json` | The pre-sign artifact reference |
| `strategy_manifest_v1.schema.json` | Artifact-local compatibility metadata |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` | The local verification receipt |
| `strategy_execution_context_v1.schema.json` | The frozen context handed to an adapter |
| `development_source_ref_v1.schema.json` | Sandbox-only development source reference |

The pattern: Custos publishes schemas for **what it owns** — its own enrollment
material, its own facts, and the execution boundary it defines.

## There is no DeploymentSpec schema here

Custos does not publish one, and its absence is asserted by a test rather than
left to memory.

The canonical DeploymentSpec is owned upstream. What Custos has is a narrow
local execution view, derived only *after* signature and digest verification
have passed. Publishing a schema for it would invite a producer to treat the
runner's local projection as the contract, when the authority is the signed
canonical payload.

Deployment publication is likewise not a Custos operation. There is no CLI
command to create, sign or publish a DeploymentSpec — see
[the CLI reference](/reference/cli).

## Where the command contract actually lives

The signed command is defined in code as a strict consumer, not as a published
schema you could produce against loosely:

- exact subject and exact event bytes are verified before any field is parsed;
- the field set is exact — unknown keys are rejected, not ignored;
- tenant, mode, runner, instance, generation and digest must agree across
  subject, envelope and payload.

For the subject shape, the two event types and the agreement matrix, see
[reference implementations](/integration/reference-implementations).

## Versioning

`v1` is the only version with content. The sibling directories are placeholders
and carry nothing.

Adding an **optional** field is a MINOR change but still needs both sides
deployed, because the schemas are `additionalProperties: false` and an
un-updated consumer rejects the new field. Adding a **required** field is MAJOR:
an old producer that does not send it fails validation outright. See
[SemVer and LTS](/release-governance/semver-lts).

Cutting `v2` would be a MAJOR change, and is not a way to keep two contracts
alive at once — the first-production rule is that V1 changes in place, with no
predecessor parsers and no compatibility aliases.

## Verifying against it

Schemas are consumed by the authority gate as well as by code:

```bash
make check-authority
```

That gate also asserts the absence of the things listed above as deliberately
missing, so a future change that quietly reintroduces a DeploymentSpec schema
fails rather than passing unnoticed.

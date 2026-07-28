---
title: "JSON Schema Reference"
sidebar_position: 3
---

# JSON Schema Reference

Eleven schemas, all under `docs/gateway-contract/v1/` in the repository. This <!-- disclosure-ok: auditable machine-asset location in the public repository -->
page says what each one is for and what the set does not cover.

Every schema is `additionalProperties: false`. An unknown field is a rejection,
not something ignored — see [contract versioning](/integration/contract-versioning)
for what that costs when you add one.

## The schemas

**Enrollment**

| File | Title |
|---|---|
| `enrollment.schema.json` | `EnrollmentPayload v1` |

**What the runner emits**

| File | Title |
|---|---|
| `runner_fact_batch_v1.schema.json` | `Custos RunnerFactBatchV1` |

All twenty fields are required. This is the only schema in the set an external
consumer subscribes to; for the subject, the signing preimage and the verifier
checklist, see [consuming RunnerFact](/integration/consuming-runner-fact).

**The strategy artifact boundary**

| File | Title |
|---|---|
| `strategy_artifact_ref_v1.schema.json` | `StrategyArtifactRefV1` |
| `strategy_manifest_v1.schema.json` | `StrategyManifestV1` |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` | `StrategyArtifactPreImportVerificationReceiptV1` |
| `strategy_execution_context_v1.schema.json` | `StrategyExecutionContextV1` |
| `development_source_ref_v1.schema.json` | `DevelopmentSourceRefV1` |

The receipt is written *before* the import, which is the point of it: it
records what was verified while refusing is still possible. The execution
context is what a strategy actually receives, frozen. `DevelopmentSourceRefV1`
is sandbox-only and explicitly non-promotable — it exists so that local
development has a path that cannot be mistaken for a released artifact.

**Toolkit release-candidate receipts**

`toolkit_rc_authority_receipt_v1`, `toolkit_rc_pending_receipt_v1`,
`toolkit_rc_receipt_manifest_v1` and `toolkit_rc_t6d_pending_receipt_v1`.

These are evidence assets, not an integration surface. They record what a
toolkit release candidate was pinned to. Nothing consumes them at runtime and
you do not produce one.

## `$id` is deliberately not a URL

```text
custos://gateway-contract/v1/runner_fact_batch_v1.schema.json
```

Validators will not fetch this, which is intentional. A schema retrieved over
the network at validation time is a schema somebody else can change between
your test run and your production run. Use the copy in your checkout.

## What JSON Schema cannot say

`runner_fact_batch_v1.schema.json` carries an `x-custos-invariants` block
because the properties that matter most are not expressible as a shape:

| Key | What it fixes |
|---|---|
| `subject` | the exact subject template |
| `stream_identity_fields` | the four fields that identify a stream |
| `signed_fencing_fields` | spec id, spec digest and generation — provenance, not identity |
| `sequence_rule` | `facts[i].seq == source_seq_start + i` |
| `generation_resets_sequence` | `false` |
| `signing_domain_base64` | the domain bytes, including the trailing NUL |
| `signing_header_fields` | the eighteen header fields **in order** |
| `payload_digest_formula` | `sha256(canonical_json(facts))` |
| `signing_preimage_formula` | `DOMAIN \|\| canonical_json(header)` |
| `canonicalization` | encoding, key ordering, number form |

The field *order* is the one to be careful with. Schema validation will happily
accept a header you serialised in a different order, and the signature will
then fail — with nothing to indicate that ordering was the reason. If you are
implementing a verifier, treat `signing_header_fields` as the authority and the
schema as a shape check.

## Verifying you have the right bytes

The fact batch schema ships a digest sidecar:

```bash
cd docs/gateway-contract/v1 && shasum -a 256 -c runner_fact_batch_v1.schema.json.sha256
```

Several of these schemas are also recorded in the repository's authority index
by path, size and commit. That is what makes them evidence rather than
documentation, and it is why a schema change is a coordinated re-issue rather
than an edit.

## What is not here

There is **no DeploymentSpec schema**, and its absence is asserted by a test
rather than left to memory. The canonical spec is owned upstream; what the
runner holds is a local view derived only after verification passes. Publishing
a schema for that view would invite a producer to build against the runner's
projection instead of the signed original.

The `v2`, `v3` and `v4` directories exist and are empty. They are placeholders,
not a roadmap — see [contract versioning](/integration/contract-versioning).

## Validating a document

The schemas have no remote references, so validation is offline:

```bash
uv run python -c "
import json, jsonschema
schema = json.load(open('docs/gateway-contract/v1/runner_fact_batch_v1.schema.json'))
jsonschema.validate(json.load(open('batch.json')), schema)
"
```

`jsonschema` comes with the `dev` extra — `make install` has it; a base install
of the runner does not, because validating someone else's document is not
something the runner does at runtime.

Three of the eleven declare `$schema` as draft 2020-12: the enrollment payload,
the fact batch and the pre-import verification receipt. The other eight omit it,
which leaves the draft to the validator's default. Pin the draft explicitly in
your own tooling rather than inheriting whatever your library happens to choose
— two validators disagreeing about the draft will disagree about the document.

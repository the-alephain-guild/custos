---
title: "JSON Schema reference"
sidebar_position: 3
---

The following schemas describe supported operator and integration inputs and outputs. They are available under `docs/gateway-contract/v1/`.

<!-- generated:schemas -->

| Schema file |
|---|
| `development_source_ref_v1.schema.json` |
| `enrollment.schema.json` |
| `offline_deployment_spec.schema.json` |
| `runner_fact_batch_v1.schema.json` |
| `strategy_artifact_pre_import_verification_receipt_v1.schema.json` |
| `strategy_artifact_ref_v1.schema.json` |
| `strategy_execution_context_v1.schema.json` |
| `strategy_manifest_v1.schema.json` |

<!-- /generated:schemas -->

## Contract groups

| Group | Use |
|---|---|
| Enrollment | Local enrollment material |
| RunnerFact batch | Signed observation batch consumed by integrations |
| Strategy artifact, manifest, context and pre-import receipt | Execution ABI and local verification boundary |
| Development source | Explicit sandbox-only signed-lane development material |
| Offline deployment spec | Operator-owned unsigned sandbox/testnet input |

The offline schema does not replace the canonical signed DeploymentSpec. Its validate/publish CLI cannot create signed ARX commands.

## Validation

Use the schema copy that matches the producer/consumer revision. `custos://...` identifiers name repository assets; they are not endpoints to fetch at validation time. Strict object field sets reject unknown properties where specified. A schema pass checks shape, not signatures, authority, deployment eligibility or production readiness.

For an offline spec:

```bash
uv run arx-runner deployment validate --spec-file "$SPEC_FILE" --mode sandbox
```

For a generic JSON Schema check, use a validator that supports the schema's declared dialect. Additional invariants are covered by contract tests and the authority gate:

```bash
make check-authority
```

The separately signed strategy signal envelope is defined by its contract fields and tests; it is not an extra kind inside `runner_fact_batch_v1`. See [consumer reference](/integration/consuming-runner-fact).

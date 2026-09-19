---
title: "Contributing"
sidebar_position: 3
---

See [CONTRIBUTING.md](https://github.com/the-alephain-guild/custos/blob/main/CONTRIBUTING.md) for the repository workflow.

Changes must preserve local credential handling, execution admission, disconnect-resilient safety and decimal money boundaries. Source identifiers, comments, runtime messages and commit messages use English; public documentation supports English and Chinese.

## Verification

Run the checks relevant to your change and report their scope. Behavioral changes need tests covering the affected success and failure paths. `make verify` is the repository baseline; engine, Docker and external-service tests have additional requirements.

For site changes, install the base runner dependencies and run:

```bash
cd docs-site
npm ci
npm run verify
```

The site checks disclosure, Chinese wrapping/terminology, generated source references, both locale builds and TypeScript. Update English and Chinese together, using task-oriented prose and exact command names. Source-derived tables are regenerated with `uv run python scripts/check-docs-site.py --write` from the repository root.

## Contract and evidence changes

Evolving code, schemas and internal contracts are governed by Git review and CI. A historical receipt records its own revision; do not rewrite it merely because source bytes changed. Coordinate intentional contract changes with consumers and record new acceptance evidence when required.

Report suspected vulnerabilities privately through the [security policy](/release-governance/security-policy). Routine improvements can follow the normal pull-request process without publishing sensitive exploit details.

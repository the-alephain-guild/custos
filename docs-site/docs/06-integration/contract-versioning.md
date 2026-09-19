---
title: "Contract versioning"
sidebar_position: 4
---

V1 is the active first-production contract. Coordinated changes update that contract in place; predecessor parsers and compatibility aliases are not maintained.

## Coordinating a change

| Change | Required coordination |
|---|---|
| Add an optional field | Update consumers before producers send it to strict schemas |
| Add a required field | Update producer and consumer together |
| Remove or rename a field | Update parsing, generation, fixtures and consumers together |

Use Git commits/tags, review and CI for evolving source, schemas, golden files and internal contracts. Update related tests and fixtures when behavior intentionally changes. Existing acceptance receipts describe their recorded revision; source edits do not authorize rewriting those historical records. New acceptance should identify the new revision and verification scope.

Immutable published artifacts remain content-addressed and require verification of the exact published bytes. This is separate from treating every source edit as a reissue of historical evidence.

## A future V2

A new wire version requires a published migration window and coordination with affected production consumers. See [release status](/release-governance/release-status) for current support.

Package SemVer and wire version are separate: a package version bump does not imply a new wire version. The published compatibility policy is in [SemVer and LTS](/release-governance/semver-lts).

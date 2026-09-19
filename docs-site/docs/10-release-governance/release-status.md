---
title: "Release status"
sidebar_position: 1
---

Custos supports development and sandbox/testnet workflows. Live execution is disabled by default and requires verified runtime approval for the installed image, signed deployment authority, permitted credentials and current risk policy. Source support does not establish production acceptance of a released image.

| Workflow | Current support |
|---|---|
| Standalone sandbox | Local lifecycle exercise using `sandbox-sim` |
| Nautilus sandbox | Compatible strategies with market data and locally simulated fills |
| Offline testnet | Supported venue connectors with testnet credentials; see the venue-specific restrictions |
| Signed deployments | Require enrollment, issued authority and verified release inputs |
| Live trading | Requires verified runtime and signed deployment approval |

## Installation and updates

Follow [installation](/getting-started/installation) for source and local-container setup. Read the [upgrade guide](/release-governance/upgrade-paths) before changing the runtime. For distributed artifacts, use the version and verification instructions supplied with the official release.

A successful build, health probe or sandbox run does not establish production readiness. Confirm the selected connector's support and limitations before using a testnet account. The [SoDEX guide](/engines/sodex) describes its account configuration requirements.

Support windows are listed in [SemVer and LTS](/release-governance/semver-lts). This page describes product availability; it does not announce a new stable release.

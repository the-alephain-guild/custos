---
title: "Release status"
sidebar_position: 1
---

Custos currently supports development and sandbox/testnet workflows. Live execution is disabled in the runner; no operator flag enables it.

| Workflow | Current support |
|---|---|
| Standalone sandbox | Local lifecycle exercise using `sandbox-sim` |
| Nautilus sandbox | Compatible strategies with market data and locally simulated fills |
| Offline testnet | Supported venue connectors with testnet credentials; see the venue-specific restrictions |
| Signed deployments | Require enrollment, issued authority and verified release inputs |
| Live trading | Not enabled |

## Installation and updates

Follow [installation](/getting-started/installation) for source and local-container setup. Read the [upgrade guide](/release-governance/upgrade-paths) before changing the runtime. For distributed artifacts, use the version and verification instructions supplied with the official release.

A successful build, health probe or sandbox run does not establish production readiness. Confirm the selected connector's support and limitations before using a testnet account. The [SoDEX guide](/engines/sodex) describes its current testnet input limitation.

Support windows are listed in [SemVer and LTS](/release-governance/semver-lts). This page describes product availability; it does not announce a new stable release.

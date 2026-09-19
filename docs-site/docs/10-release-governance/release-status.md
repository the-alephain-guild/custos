---
title: "Release status"
sidebar_position: 1
---

This page separates source support, candidate publication and production acceptance. The snapshot below was checked against repository records on 2026-09-19 at source revision `c1d27024312210590bc0716fe1ddff592e5f8012`.

| Surface | Recorded state | What it establishes |
|---|---|---|
| Runner source package | `0.3.0` | Checkout package version; not a stable-release announcement |
| Nautilus 2 contract handoff | Consumer handoff complete | Registered consumers accepted the coordinated contract |
| Toolkit candidate | `0.1.0rc7`, source `8bf45ac6b0f42018aae2a74ac9e743e41f9ca789` | Candidate authority recorded; not production execution |
| Runtime candidate image | Published and attested, source `4afffb96b1a768fb34f66692d4bb7f96652aeccf` | Publication and exact-image verification recorded for that revision |
| Local runtime composition | Implemented with recorded local activation evidence | Local composition and test evidence |
| Deployed runtime / production | Acceptance open | `runtime_ready=false`, `production_ready=false` |
| Live execution | Disabled in current daemon composition | No operator flag enables live trading |

The recorded runtime candidate image is:

```text
ghcr.io/the-alephain-guild/custos@sha256:2e9081c14df31cac15112ba0a38100da94cb271a6bbaf7f9ad3c1096548c6753
```

This is a historical candidate coordinate, not a recommended current deployment. Registry availability was not rechecked for this documentation update. The image predates the current Nautilus 2 checkout; its publication record does not establish current-HEAD acceptance.

## Selecting an artifact

For current-source development, follow [installation](/getting-started/installation) and record the Git revision and local image id. For a remotely distributed candidate, verify its digest, signature, source revision and acceptance scope before use. A candidate publication does not start a stable support window or close the production gates.

Keep historical acceptance records attached to their original revisions. Record new evidence for a new revision rather than refreshing an old receipt to match changed source files.

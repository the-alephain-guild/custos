---
title: "Architecture at a glance"
sidebar_position: 3
---

Custos has a shared engine and credential layer, with separate compositions for signed and offline input.

```text
ARX signed command -> verify -> durable desired state -> artifact activation
                                                        |
                                                        v
local vault -------------------------------------> engine host -> venue
                                                        |
signed outbox <---------------------------------- observations
     |
     +-----> ARX

operator -> local NATS -> offline reconciler -> mounted strategy -> engine host
                              |
                              +-----> local deployment status
```

ARX is presented here as one product. Its identity authorization and command/fact interfaces have different availability requirements; an enrollment outage does not itself describe the health of the message transport.

## Components

| Component | Responsibility |
|---|---|
| Identity and vault | Encrypt local keys, bind machine metadata, resolve exchange credentials |
| Signed intake | Authenticate exact bytes/subject and persist desired state |
| Artifact runtime | Resolve, verify, quarantine and activate signed-lane release material |
| Offline reconciler | Apply operator-owned sandbox/testnet specs using a strategy directory |
| Engine host | Construct and supervise a node; report connectivity and portfolio state |
| Local safety | Evaluate exposure, drawdown and containment independently of transport |
| Observation delivery | Durable signed outbox on the signed lane; local status on the offline lane |

## Runtime boundaries

Signed runtime operations use `deployment_instance_id`; spec id, digest and generation carry configuration provenance and ordering. Offline runtime identifiers are deterministically derived from `spec_id` and are not canonical ARX identities.

The supervisor accepts repeated `--enabled-mode` flags for signed transport sessions. The Nautilus 2 host permits only one active node on an event loop. Use separate runner processes and state directories for concurrent nodes; declaring multiple modes does not remove this limit.

Exchange credentials remain on the host. Money calculations use `Decimal` and wire values use integers or canonical decimal strings. Live admission remains disabled in the current daemon composition.

## Reading paths

- [Standalone sandbox](/getting-started/standalone-sandbox): local lifecycle exercise.
- [Signed sandbox](/getting-started/first-sandbox-run): connect an enrolled runner.
- [NautilusTrader](/engines/nautilus-trader): engine and connector limits.
- [Safety during disconnects](/trust-model/safety-survives-disconnect): lane-specific enforcement.

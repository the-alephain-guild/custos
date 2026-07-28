---
title: "The Trust Model"
sidebar_position: 2
---

# The Trust Model

Custos (Latin: *guardian*) is a non-custodial, self-hosted execution runner. You
run the daemon on your own infrastructure, it holds your exchange credentials
locally, and it executes strategies against a venue on your behalf.

That arrangement asks a lot. This page is about why the arrangement is
defensible, and what specifically you are trusting.

## Why it is open source

The runner holds your exchange credentials and places real orders. There is
exactly one condition under which trusting it is rational: you can read the code
and check what it does with those credentials.

Open source is therefore not a licensing preference here. It is the mechanism
that turns "credentials stay on your machine" from a claim in a document into a
property an external auditor can verify line by line. A closed runner asking for
the same trust would be asking you to take its word.

Apache-2.0, public from the first release. See [`LICENSE`](https://github.com/the-alephain-guild/custos/blob/main/LICENSE).

## What crosses the boundary

Three properties define the boundary Custos defends.

**Credentials and strategy code stay local.** The only things that leave the
runner are signed observations — statements about what the engine did. No
credential, no key material, and no plaintext of either is ever published,
logged, or sent upstream.

**Control is declarative, not imperative.** Custos receives a signed statement of
what *should* be running and reconciles local reality toward it. Nothing reaches
into your machine to start a process. This is the difference between a runner you
host and an agent someone else drives, and it is why a lost message degrades into
"converge on restart" rather than "instruction lost".

**A disconnect degrades gracefully.** If the upstream authority becomes
unreachable, running deployments keep running from durable local state, and local
protection keeps enforcing — the aggregate notional cap, the drawdown breaker and
the zombie watchdog all evaluate locally. An outage never stops local trading and
never removes local protection.

The four guarantees these produce are stated, with the code and tests that hold
each one, in the [trust model chapters](/trust-model/red-lines).

## Who decides, who executes

ARX decides. Custos executes. Neither can do the other's job.

| | ARX | Custos |
|---|---|---|
| Authenticates the actor | ✅ | ✗ |
| Owns the deployment record | ✅ | ✗ |
| Approves live promotion | ✅ | ✗ |
| Holds venue credentials | ✗ | ✅ |
| Places orders | ✗ | ✅ |
| Signs execution facts | ✗ | ✅ |

Two signature checks, opposite directions, no shared secret. Compromising the
runner does not let you manufacture an approval; compromising the approval path
does not reach a venue without a runner willing to execute.

Custos exposes no API to end users, dashboards or API clients. It subscribes and
it publishes; there is no inbound control surface to attack.

## The six modules

| Module | Responsibility | Guarantee it anchors |
|---|---|---|
| **enrollment** | Nonce-bound proof of possession; encrypted machine credential; rotation and revocation | Private key never transmitted |
| **reconcile** | Verify signed desired state, converge local runtime, record outcomes durably | A disconnect is not a stop |
| **engine host** | Supervise the trading engine, configure venue clients, enforce admission | Live execution is always gated |
| **runner_fact** | Typed signed statements through a durable local queue | No unsigned fallback path |
| **credential_vault** | `sops`+`age` local vault; `trade_no_withdraw` scope enforcement | Credentials never leave the process |
| **transport** | Signed desired-state subscription; signed fact publication | Versioned wire contract |

Each has its own chapter — start from
[architecture at a glance](/introduction/architecture-at-a-glance) for the map.

## Independence

The repository is self-sufficient on purpose. An external auditor clones one
repository and can read everything: the code, the rules it is held to, the
contract assets, and the tests that prove the claims.

Nothing in that audit requires access to anything closed. If it did, the
verifiability argument above would collapse — an auditor who has to take one part
on faith has to take all of it on faith.

Releases follow SemVer, and each minor line is supported for at least 12 months.
See [SemVer and LTS](/release-governance/semver-lts).

## Next

- The four guarantees in depth: [red lines](/trust-model/red-lines)
- How the pieces fit: [architecture at a glance](/introduction/architecture-at-a-glance)
- Check the claims yourself: [audit checklist](/trust-model/audit-checklist)

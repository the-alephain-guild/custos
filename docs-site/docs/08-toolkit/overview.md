---
title: "Strategy Toolkit Overview"
sidebar_position: 1
---

# Strategy Toolkit Overview

The toolkit is the boundary between a strategy artifact and the runner that
executes it. It defines the execution ABI, the shape of the artifact reference
Custos signs before release, and the verifier that decides whether an artifact
may be imported at all.

## What Custos owns here

Custos owns four things and deliberately not the rest:

- the strategy **execution ABI**;
- the **toolkit implementation**;
- the pre-sign **`StrategyArtifactRefV1`**;
- the local **fail-closed verifier**.

Everything upstream of that belongs to ARX: strategy source, the release bill of
materials, the signed release statement, the detached attestation reference,
artifact evidence, acceptance receipts, artifact selection, the DeploymentSpec,
effective configuration, and business risk policy.

The split matters because it decides what a compromised runner can do. It can
refuse to execute. It cannot select a different artifact, approve one, or
declare one released.

## The execution ABI

The entry-point group is fixed:

```text
alephain.strategy_runtime.v1
```

`deployment_instance_id` is the only runtime address. Spec id, spec digest and
generation are provenance and ordering inputs — they say what was configured and
in what order, never which running thing to talk to. Catalog aliases never
authorize or address execution.

An adapter receives its final effective configuration from the verified signed
command. It cannot merge defaults, read a config file, or mutate what it was
given. Custos parses JSON numbers as `Decimal`, rejects duplicate keys and
non-finite values, recursively freezes the containers, and recomputes
`effective_config_digest`.

Freezing is what makes the digest meaningful. A configuration an adapter could
mutate after the digest was computed would be a digest of something that no
longer ran.

### Canonical JSON

`sha256-canonical-json-v1` is UTF-8, with object keys recursively sorted, array
order preserved, finite `Decimal` numbers, and no insignificant whitespace.

Implement against those rules rather than against a language's default encoder —
most differ in at least one of them.

## The artifact boundary

`StrategyArtifactRefV1` (`schema_version: 1`) describes **only** what exists
before signing: exact executable and manifest bytes, runtime artifacts, SBOM,
and contract schema.

It deliberately carries no bundle coordinate or digest, no certificate or
transparency proof, no trust-policy identity, and no release, deployment,
approval or selection state. Those come into existence later, and a reference
that claimed them would be asserting facts that had not happened yet.

`StrategyManifestV1` is artifact-local compatibility metadata, nothing more.

Custos does not define the canonical release BOM. It consumes the strict BOM
object and requires a lossless in-memory projection of every member: the base,
contracts, Nautilus and strategy wheels, plus manifest, SBOM, contract schema,
normalized source tree and every runtime artifact. An attestation bundle is
detached — it is never a BOM or ArtifactRef member.

The signed command binds runtime identity, spec provenance, generation, release
id, the full BOM object and digest, the pre-sign ArtifactRef, accepted evidence,
and the effective config digest. No separately serialized member table is
allowed to become a second authority on what the release contains.

## Verification is fail closed

Verification covers the certificate chain, Fulcio identity and validity, the SCT,
the DSSE PAE and signature, the Rekor entry, body and SET, the inclusion proof
and the checkpoint. Verification and safe extraction both complete before import.

None of the following is a production verification path: a skip flag, a Python or
`cosign` subprocess, a sidecar, an HTTP verifier, or a bundle that is merely
structurally plausible.

Trust roots and the expected issuer, workflow and policy come from signed
immutable local release configuration. Artifact metadata may *reference* a trust
root; it can never *select* one. An artifact that could choose the authority
verifying it would be verifying itself.

## Two distributions

| Distribution | Python | Notes |
|---|---|---|
| `custos-strategy-toolkit` | `>=3.11` | Base and contracts |
| `custos-strategy-toolkit-nautilus` | `>=3.12,<3.13` | Exact matching base version, `nautilus-trader==1.230.0` |

On Python 3.11, resolving the Nautilus distribution must **fail** rather than
quietly install without NautilusTrader. A silent omission would produce an
environment that imports cleanly and cannot trade.

## Inventory and typing debt

The published inventory classifies every deterministic input: **241** files —
36 platform-neutral, 55 Nautilus-specific, 150 private-vendor. Extraction maps
those one-to-one into `custos_toolkit`, `custos_toolkit_nautilus.adapter`, and
the private `custos_toolkit_nautilus._vendor.pandas_ta` namespace.

Extraction may not publish a top-level `shared` or `pandas_ta`, mutate
`sys.path`, fake a distribution, or leave two writable canonical copies.

Typing is reported honestly rather than as a single pass/fail:

| Scope | Standard |
|---|---|
| Custos-owned contracts and package shell | strict mypy, must pass |
| Inventory-extracted implementation | checked against an exact recorded baseline |
| Private third-party vendor code | outside mypy; guarded by exact digests and fixed-input parity |

The baseline currently records **75** platform-neutral and **289**
Nautilus-adapter errors. That is acknowledged debt, not a strict pass, and it is
published rather than rounded off — a baseline you cannot see is one you cannot
hold anyone to.

## Reproducing the assets

```bash
make strategy-contract-assets   # regenerate schema, golden, receipt assets, digest index
make check-toolkit-extraction   # reconstruct every extraction target from the pinned source
make toolkit-typecheck          # strict where required, baseline-checked elsewhere
```

`strategy-contract-assets` rejects predecessor tracks rather than preserving
them. A separate parity golden independently freezes fixed-input signal and
order-intent behaviour, plus private-vendor indicator behaviour, from before the
extraction — so the move can be shown not to have changed results.

## Current status

The producer receipt is `CANONICAL_V1_PENDING_CONSUMER_RECEIPTS`. Handoff,
runtime and production readiness are all **false**.

Custos publishes the execution ABI; the consuming owners must pin the same exact
V1 bytes before the coordinated handoff closes. Custos does not author receipts
on their behalf, and a receipt Custos wrote for a counterparty would prove
nothing about whether that counterparty can actually read the bytes.

This is a first-production contract: one active V1 parser, dataclass, schema,
golden set, asset index and authority entry. Superseded shapes are deleted, not
kept as aliases or fallbacks. Git history and immutable digests retain the audit
evidence; runtime code does not carry it.

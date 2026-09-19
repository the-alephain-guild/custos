---
title: "Artifact signing and verification"
sidebar_position: 2
---

The signed artifact path verifies exact release bytes before importing strategy code. It checks a detached Sigstore bundle containing a signed in-toto statement about the artifact members.

## Verification conditions

| Boundary | Required check |
|---|---|
| Input | Stable regular file, valid bundle, no duplicate JSON keys |
| Subjects | Required member digests present, no duplicate subject |
| Identity | Accepted issuer, workflow identity and source repository |
| Signature | Certificate chain/validity, SCT, DSSE PAE and signature |
| Transparency | Rekor entry/body/SET, inclusion proof and checkpoint |
| Activation | Safe extraction, atomic activation, module origin under the activated root |

Trust comes from an independently signed local release policy and trusted root. Artifact metadata cannot select either. The policy is verified before use.

| CLI flag | Input |
|---|---|
| `--artifact-release-policy-envelope` | Signed policy naming accepted identities and limits |
| `--artifact-release-policy-key-id` | Expected authority key id |
| `--artifact-release-policy-public-key` | Authority verification key |
| `--artifact-sigstore-trusted-root` | Sigstore trust root |

Provisioning with `release-policy issue` is described in [deployment](/operator-guide/deployment). A locally generated development authority does not constitute production approval.

## Failure handling

Verification and extraction finish before any import. The loader also rejects a module cached from another activation. There is no production skip flag, external shell verifier or acceptance based only on a plausible bundle shape.

The artifact runtime is composed and has recorded local execution evidence. Candidate publication and consumer handoff are separate from deployed production acceptance, which remains open. See [release status](/release-governance/release-status).

The offline mounted-strategy path is a separate sandbox/testnet workflow without these signed-release claims. It cannot produce promotion evidence.

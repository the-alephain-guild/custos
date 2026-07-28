# Strategy toolkit and execution contract

**Status:** authoritative for Custos execution ABI and artifact verification.

## Ownership

Custos owns the strategy execution ABI, toolkit implementation, pre-sign
`StrategyArtifactRefV1`, and local fail-closed verifier. Philosophers-Stone owns
strategy source, canonical `StrategyReleaseBomV1`, the signed
`StrategyReleaseStatementV1`, and detached `ArtifactAttestationRefV1`. Crucible
owns `ArtifactEvidenceV1`, acceptance receipts, StrategyRelease, artifact
selection, DeploymentSpec, effective configuration, and business risk policy.

The legacy Philosophers-Stone `build-image.sh` to Crucible Python publication
and deployment path remains an independent compatibility lane. It cannot
produce a v1.team receipt or act as a fallback for this contract.

## Runtime and configuration

`deployment_instance_id` is the only runtime address. `deployment_spec_id`,
`deployment_spec_digest`, and `generation` are provenance and ordering inputs.
Catalog aliases never authorize or address execution. The fixed entry-point
group is `alephain.strategy_runtime.v1`.

The adapter receives final effective config from a verified signed Crucible
command. Custos parses JSON numbers as `Decimal`, rejects duplicates and
non-finite numbers, recursively freezes containers, and recomputes
`effective_config_digest`. Adapters cannot merge defaults or mutate config.

`sha256-canonical-json-v1` uses UTF-8, recursively sorted object keys, preserved
array order, finite Decimal numbers, and no insignificant whitespace.

## Artifact boundary

`StrategyManifestV1` is artifact-local compatibility metadata.
`StrategyArtifactRefV1` (`schema_version: 1`) describes only exact executable
and manifest bytes, runtime artifacts, SBOM, and contract schema available
before signing. It has no bundle coordinate/digest,
certificate/transparency proof, trust-policy identity, release, deployment,
approval, or selection state.

Custos does not define canonical `StrategyReleaseBomV1`. It consumes the strict
PS BOM object and requires a lossless in-memory member projection with
base/contracts, Nautilus, and strategy wheels plus manifest, SBOM, contract
schema, normalized source tree, and every runtime artifact. An attestation
bundle is detached and is never a BOM or ArtifactRef member.

The signed command binds runtime identity, spec provenance, generation,
StrategyRelease id, full BOM object/digest, pre-sign ArtifactRef, accepted
ArtifactEvidence, and effective config digest. No separately serialized member
table may become a second authority.

The PS in-toto/DSSE statement signs producer claims over fixed BOM, artifact and
manifest subjects. After the bundle is immutable, `ArtifactAttestationRefV1`
binds its coordinate/digest. Crucible then verifies the bundle with local policy
and produces post-bundle evidence; that composite evidence digest is not and
cannot be claimed as a subject of the same bundle.

Trust roots and expected issuer/workflow/policy come from signed immutable local
Custos release configuration. Artifact metadata may reference, but cannot
select, trust roots. Verification and safe extraction precede import.

Verification is fail closed across the certificate chain, Fulcio identity and
validity, SCT, DSSE PAE/signature, Rekor entry/body/SET, inclusion proof and
checkpoint. No skip flag, Python or `cosign` subprocess, sidecar, HTTP verifier,
or structurally plausible bundle fallback is a production verification path.

This is the first production contract. There is one active V1 parser, dataclass,
schema, golden set, asset index, and authority entry. Superseded pre-production
shapes are deleted rather than accepted as aliases or fallbacks. Git history
and immutable OCI digests retain audit evidence; runtime code does not.

## Python and inventory

The `custos-strategy-toolkit` base/contracts distribution supports Python
>=3.11. The separate `custos-strategy-toolkit-nautilus` distribution requires
Python >=3.12,<3.13, exact matching base version, and `nautilus-trader==1.230.0`;
Python 3.11 resolution must fail rather than omit NT.

`docs/authority/strategy-toolkit-inventory-v1.json` classifies every current
deterministic input below legacy `shared/` and `vendor/`. There are 241 inputs:
36 platform-neutral, 55 Nautilus-specific, and 150 private-vendor files. Earlier
Plan prose counted 459 general filesystem entries; that is not the deterministic
extraction set.

Plan 18 T4 maps those inputs one-to-one into `custos_toolkit`,
`custos_toolkit_nautilus.adapter`, and the private
`custos_toolkit_nautilus._vendor.pandas_ta` namespace. The legacy implementation
tree is removed; its package marker is implementation-free. Extraction may not
publish top-level `shared`/`pandas_ta`, mutate `sys.path`, fake a distribution,
or leave two writable canonical copies. Runtime verifier activation remains T5.

Plan 18 T3 moves the exact reviewed execution-contract source bytes to
`packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py`.
The canonical V1 receipt declares that source path and pins the coordinated
Custos, Philosophers-Stone, and Crucible handoff bytes. No second source or
re-export shim is retained.

Run `make strategy-contract-assets` to regenerate the sole canonical V1
ArtifactRef schema, pre-sign golden, verification receipt assets, contract
receipt, and immutable digest index. The digest index contains only producer
assets and never consumer receipt/readiness state; that mutable handoff state
lives exclusively in the separate contract receipt, avoiding circular relocks.
The command rejects predecessor tracks rather than preserving them.
`make check-toolkit-extraction` reconstructs every T4 target
from the pinned T3 Git blob, and
`strategy-toolkit-parity-golden-v1.json` independently freezes pre-move fixed-input
signal/order-intent and private-vendor indicator behavior.

`make toolkit-typecheck` reports two distinct results: Custos-owned contracts and
package shell must pass strict mypy, while inventory-extracted implementation is
checked against the machine-readable exact debt baseline in
`strategy-toolkit-typing-baseline-v1.json`. The current 75 platform-neutral and
289 Nautilus-adapter errors are acknowledged debt, not a strict PASS. Plan 18
Task 4b must reduce that baseline to zero before the distributions or 18b may be
called strict or production-ready. Private third-party vendor code stays outside
mypy and remains guarded by exact digests plus fixed-input parity.

The current authority record is
`custos-strategy-contract-v1-producer-receipt.json`. It remains
`CANONICAL_V1_PENDING_CONSUMER_RECEIPTS`, with handoff/runtime/production false.
Crucible's owner-produced Plan 88 contract receipt is pinned by exact commit and
vendored-byte hash. The PS consumer receipt remains absent; Custos must not fabricate
it, and the coordinated handoff stays closed until PS consumes the same exact V1
bytes and the final relock is complete.

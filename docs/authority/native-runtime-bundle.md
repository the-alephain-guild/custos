# Local native candidate measurement

`arx-runner start --native-runtime-bundle <bundle-root>` supplies the archive and
source profile of an explicitly built local candidate. It does not select an
engine version, change a capability, or authorize a complete inventory model.
Released installations have no candidate descriptor and reject this input.

The existing candidate builder reads the versioned recipe and generates
`custos/core/_native_build_descriptor_v1.json` inside the runner wheel. The
descriptor binds the exported runner source commit and exact engine record.
The engine record adds a required, closed `native_build` object:

| Field | Meaning |
|---|---|
| `source_revision` | Nonzero, 40-character lowercase native Git revision. |
| `crate_version` | Canonical three-part numeric Cargo workspace version. |
| `profile` | The exact development build profile, `dev`. |
| `features` | Nonempty unique explicit maturin feature arguments, in source order, including `extension-module`. This is not a transitive feature closure. |

The recipe is reviewed against the fixed native source, build command and actual
archive. These fields are not caller overrides. Archive hashes distinguish
unpublished local candidates even when their distribution versions match.

Before creating an outbox, tasks or a native host, the daemon compares the
protected bundle profile and its source-inventory descriptor with the installed
build resource and static toolkit engine profile. It rejects missing files,
symlinks, path escape, writable inputs from another trust scope, source mismatch
and metadata drift. The existing archive measurement then checks installed import
origins, ABI, metadata and package payload against the archive. The host receives
the measured archive digest and the descriptor's Cargo version; the collector
still compares that version with the actual capture source.

The current recipe describes the retained local archive from revision
`50e5bd4dd12bc6a8e9a866bbeae55f7e3d89670b`, with measured archive SHA-256
`e7cbea544961fdc23d51486d5fad954bd5974cbc8d543628323d9a3c7dbd195e`.
This measurement plumbing does not qualify native dispatch or opening protocols.
A new native build requires new measured archive evidence and a reviewed recipe
update, followed by a normal candidate rebuild and independent observations.

Default opening flags, complete-model declarations and readiness remain
unchanged. Raw native observations precede model registration; registration,
successful opening, restart continuity and end-to-end delivery require their
own acceptance. Historical archives and acceptance receipts are retained.

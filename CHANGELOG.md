# Changelog

All notable changes to `custos-runner` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The complete versioning contract — what is allowed / forbidden per MAJOR /
MINOR / PATCH bump, LTS window, security patch SLA, key-rotation protocol —
lives in [`docs/lts-commitment.md`](docs/lts-commitment.md) and
[`docs/upgrade-path.md`](docs/upgrade-path.md).

## [Unreleased]

## [0.3.0] - 2026-07-12

Custos 0.3.0 closes the standalone deployment loop. The verified local image
`custos-runner:v0.3.0` is the complete NautilusTrader runtime, and the
desired-state producer and consumer share one strict public contract. This is
a clean break: downstream development must start from 0.3.0 rather than
carrying compatibility code for an older runner. **Remote release: deferred**;
this entry records the code and local Docker contract, not a GitHub, PyPI, or
GHCR publication event.

### Added

- `custos.contracts.DeploymentSpec` and `DeploymentMessage` as the normative
  consumer and transport interfaces. The contract requires `generation >= 1`,
  separates `trading_mode` from `lifecycle_state`, rejects unknown fields, and
  passes opaque `strategy_config` to the selected strategy factory unchanged.
- `arx-runner deployment validate` and `arx-runner deployment publish`,
  including canonical subject and JetStream acknowledgement handling;
  producers no longer assemble envelopes or import engine-private hash helpers.
- `arx-runner nats bootstrap --profile standalone`, which idempotently owns the
  FILE-backed deployment and observed-state stream topology without letting
  the production runner mutate infrastructure implicitly.
- `arx-runner health` and atomic ready-state files for Compose, systemd, and
  Kubernetes probes. Readiness is asserted only after the deployment
  subscription succeeds and is cleared on shutdown or retry.
- A hermetic standalone acceptance test covering real NATS bootstrap,
  sops+age vault decrypt, running-to-stopped-to-running generation
  reconciliation, status publication, and readiness through the official image.

### Changed

- The verified local `custos-runner:v0.3.0` image includes
  NautilusTrader, PyYAML, sops, and age. `ENTRYPOINT ["arx-runner"]` plus an
  explicit `CMD ["start"]` gives both daemon-by-default and subcommand-safe
  Docker behavior.
- Engine selection is the closed enum `--engine nautilus|noop`; `nautilus` is
  the default and `noop` is an explicit non-live contract-test host.
- Deployment payloads are validated at the runtime boundary before vault, gate
  or host code runs. Successful `stopped` and `archived` desired state now
  reports `phase=stopped` rather than claiming the deployment is running.
- Subscription failure uses bounded exponential retry while local guard ticks
  continue; deployment readiness reflects the real subscription state.
- Release CI shape is prepared to verify the lightweight base install, the
  Nautilus extra, the complete candidate image contract, and the signed
  published artifact when remote publication is authorized.

### Removed

- `--use-nt-host`; no compatibility alias is retained. Use
  `--engine nautilus` or `--engine noop` explicitly.
- The testnet example's derived Custos Dockerfile. Downstream development
  consumes the verified local image directly and owns only strategy material
  and `strategy_config` assembly.

### Fixed

- Per-key vault decryption now pins JSON input and output types so sops 3.13.x
  does not infer the `.enc` payload as binary.
- Live checks use `trading_mode == "live"` instead of conflating execution mode
  with lifecycle state.
- A higher-generation active spec after `stopped` now creates a fresh engine
  deployment and re-runs vault and gate checks instead of reconfiguring a removed one.

### Security

- The local runtime remains non-root and the local gate checks the CLI,
  Nautilus/YAML imports, sops/age executables, readiness probe, package
  version, and source-revision label. A future remote release additionally
  requires cosign verification before acceptance.

## [0.2.0] - 2026-07-11

The 0.2.0 release combines a clean-break CLI redesign with the
distribution-and-contract-versioning work. Existing 0.1.x operators
must run through [`docs/upgrade-path.md`](docs/upgrade-path.md) — the state
namespace has moved from `~/.custos/` to `~/.arx/` and the legacy
`SopsAgeVault` multi-credential-in-one-JSON sops file has been replaced by
per-key `.enc` files under `~/.arx/vault/`.

### Added

- `[project.scripts].arx-runner` — single console-script entry
  (`arx-runner enroll` / `arx-runner vault put | verify | list` /
  `arx-runner start`) dispatching through `custos.cli.subcommands:main`.
- Multi-stage `Dockerfile` — Python 3.12-slim `builder` + slim `runtime`,
  non-root `USER 1000:1000`, `VOLUME ["/home/custos/.arx"]` for persistent
  state, OCI provenance labels (`org.opencontainers.image.*`). Published as
  `ghcr.io/the-alephain-guild/custos:v0.2.0`.
- Sigstore keyless wheel signing — `.github/workflows/scripts/sign-wheel.sh`
  emits `<wheel>.sigstore` bundles verifiable against the tag-driven
  cert-identity via `sigstore verify identity`.
- Cosign keyless docker-image signing — `.github/workflows/release.yml`
  `sign-docker` job attaches an OIDC signature to the pushed image.
- 8-job release workflow — build-wheel → sign-wheel → build-docker →
  sign-docker → publish-pypi → publish-ghcr → verify-release →
  release-notes. Triggered by `v[0-9]+.[0-9]+.[0-9]+` stable tags only;
  RC tags run on a separate pre-release workflow.
- `docs/lts-commitment.md` — LTS window (EOL ≥ 12 months per minor line),
  security patch SLA (30 days), release cadence (quarterly best-effort),
  key-rotation protocol, deprecation grace window.
- `docs/upgrade-path.md` — 0.x → 1.0 promote checklist and minor-line
  upgrade template.
- `docs/reproducible-build.md` — `SOURCE_DATE_EPOCH` + `uv.lock` freeze,
  double-build bytes-identical verification.
- `docs/gateway-contract/v1/` — JSON Schemas for the four CustosGateway
  payloads (`enrollment`, `deployment_status`, `telemetry_snapshot`,
  `heartbeat`) with a golden-snapshot backward-compat gate.
- `docs/ops/05-deployment.md` §Docker Runtime Volume Mount — append-only
  section documenting `docker run -v ~/.arx:/home/custos/.arx …` and the
  fail-loud message when the volume is missing.
- `CONTRIBUTING.md` + `SECURITY.md` — public-repo façade (test runner,
  PR flow, vulnerability disclosure, Apache-2.0 as-is disclaimer).
- `[project.optional-dependencies].lts` — release-engineering toolchain
  (`sigstore>=3.0,<4.0` + `pytest-docker>=3`).
- `[tool.hatch.build.hooks.custom]` + `hatch_build.py` — reproducible-
  build defence-in-depth hook honouring `SOURCE_DATE_EPOCH`.
- Pytest markers `docker` / `ci_only` / `slow` — registered for the
  distribution-level gates so unregistered marks no longer emit warnings.

### Changed (BREAKING)

- State namespace `~/.custos/` → `~/.arx/`. `~/.custos/enrollment.json`
  and `~/.custos/state/` must be moved before the first `arx-runner
  start` on 0.2.0; the daemon does NOT auto-migrate.
- Vault storage model: the multi-credential-in-one-JSON `SopsAgeVault`
  file is replaced by per-key `.enc` files under `~/.arx/vault/`.
  Existing operators must `sops --decrypt` their old vault manually and
  re-add each key via `arx-runner vault put`.

### Removed (BREAKING)

- Legacy `python -m custos` entry point — now `sys.exit(2)` with a
  pointer to `arx-runner start`.
- Legacy `custos` console script — removed to avoid long-term dual-CLI
  drift; `arx-runner` is the single entry.
- `SopsAgeVault` class and its supporting code paths (the shared vault
  base and audit events are preserved; only the sops-file model is retired).
- `--sops-file` and `--age-key-file` CLI flags.

### Fixed

- No externally reported bugs — 0.2.0 is the first tagged release since
  the initial extraction; the `Fixed` section will populate from 0.2.1
  onwards.

### Security

- No CVEs published against 0.1.x. Vulnerability disclosure now goes
  through GitHub Security Advisories (see `SECURITY.md`) with a 30-day
  best-effort patch SLA (see `docs/lts-commitment.md`).

[Unreleased]: https://github.com/the-alephain-guild/custos/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/the-alephain-guild/custos/releases/tag/v0.3.0
[0.2.0]: https://github.com/the-alephain-guild/custos/releases/tag/v0.2.0

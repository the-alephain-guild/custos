# Changelog

All notable changes to `custos-runner` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The complete versioning contract — what is allowed and forbidden per MAJOR /
MINOR / PATCH bump, the LTS window, the security patch SLA and the key-rotation
protocol — is published at
[SemVer and LTS](https://custos.alephain.com/release-governance/semver-lts) and
[upgrade paths](https://custos.alephain.com/release-governance/upgrade-paths).

## [Unreleased]

### Changed

- **Breaking:** `StrategyManifestV1` requires `trading_scope` --
  `{"connector": ..., "pairs": [...], "leverage": ...}` -- the connector, pairs
  and leverage the release was validated to trade. A release whose manifest has
  no `trading_scope` is refused; rebuild and republish it.
- A signed deployment runs only on the trading scope its strategy declares. The
  runner compares the strategy's connector, claimed instruments and leverage
  with the deployment's before the engine starts, and quarantines a mismatch at
  once with `strategy_trading_scope_mismatch`. A signed strategy config may not
  carry its own `trading` section.

## [0.3.0] - 2026-09-25

The first published release. It ships as the signed container image
`ghcr.io/the-alephain-guild/custos:v0.3.0`, verified by digest against the full
runtime gate before the stable tag was applied, and as source. There is no PyPI
package. The entries below this one describe source revisions that were never
published as artifacts; an earlier, unpublished 0.3.0 entry dated 2026-07-12 is
replaced by this one.

### Added

- A NautilusTrader 2.0 runtime (`nautilus-trader==2.0.0rc5+sodex.1`) hosted on
  `LiveNode`, with the connectors `binance`, `binance_perpetual`, `okx`,
  `okx_perpetual`, `sodex` and `sodex_perpetual`. Live trading additionally
  requires signed promotion evidence; see [release status](https://custos.alephain.com/release-governance/release-status).
- The signed lane: desired state is accepted only as signed commands verified
  byte for byte, and the desired and applied state, leases and outcomes are kept
  in the local runner database so a restart resumes where it stopped.
- Signed RunnerFact V1 reporting through a durable outbox: fills, equity and
  position snapshots, heartbeats, lifecycle, runtime logs, and reconciliation
  evidence (venue ledger snapshots, valuation checkpoints, period close).
- Local safety that keeps working while the control plane is unreachable: the
  fallback breaker and the signed runner safety policy's `max_total_notional`
  ceiling, enforced where orders leave the strategy.
- The offline lane for sandbox and testnet strategy verification:
  `arx-runner deployment validate`, `publish` and `schema`,
  `arx-runner nats bootstrap --profile standalone` and
  `arx-runner identity standalone`. Live is refused on this lane.
- `arx-runner credential`, `nats-transport`, `publish-capability` and
  `release-policy` for machine identity, transport and capability authority.
- `CUSTOS_VENUE_PROXY_URL` sends Binance market data, execution and ledger
  traffic through an `http://` or `https://` forward proxy. The address is read
  from the environment and logged only as scheme, host and port.

### Changed

- **Breaking:** an offline deployment spec must declare `"spec_version": 2`. A
  runner refuses any other version and names the version it accepts;
  `arx-runner deployment schema` prints the schema it validates with.
- **Breaking:** the offline spec no longer carries `connector`, `pairs`,
  `leverage` or `strategy_config`. They come from the `trading` section of the
  strategy's `config.yaml`, which must set `trading.leverage` explicitly.
- **Breaking:** the simulation engine is `--engine sandbox-sim`; the earlier
  `noop` spelling never shipped.
- After a restart the runner becomes ready, reports and accepts commands first,
  and recovers each deployment in the background. A newer command, including a
  stop, cancels an in-flight recovery; a deployment that exhausts its restart
  budget is quarantined without ending the runner.
- A heartbeat reports `online` only once the deployment's engine is ready and
  its state reliable; a deployment still starting reports `degraded`.
- The base install supports Python 3.11; the NautilusTrader runtime requires
  Python 3.12.

### Fixed

- Sandbox Binance spot uses the public JSON market data feed, which needs no
  credentials.
- Warmup history requests start on a whole microsecond instead of emitting a
  precision warning.
- Many execution, risk and reconciliation corrections made while hardening the
  runtime, including preserved breaker and restart state across restarts,
  confirmed containment on stop, reservation release by unfilled quantity, and
  independent cash and perpetual valuation for reconciliation.

### Security

- The image runs as a non-root user. Its stable tag names the same digest that
  passed the runtime gate, signed keyless with cosign; see
  [signed release verification](https://custos.alephain.com/trust-model/signed-release-chain).

### Known limitations

- OKX and SoDEX do not route through the venue proxy yet; a deployment on them is
  refused while a proxy is configured. SOCKS proxies are not supported.
- Docker image bytes are not reproducible bit for bit.
- A passing build, health probe or sandbox run does not establish production
  readiness.

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

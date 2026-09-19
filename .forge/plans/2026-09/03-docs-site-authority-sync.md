# Docs-site authority and operator-flow synchronization

Status: Completed; documentation published and public content verified.

Baseline: `c1d27024312210590bc0716fe1ddff592e5f8012` (2026-09-19).

Audit: `../../reviews/2026-09/19-docs-site-code-drift-audit.md`.

## Objective

Bring the existing English and Chinese documentation in line with the current
CLI, signed/offline delivery lanes, Nautilus 2 host, integration surfaces and
revision-scoped readiness evidence. Preserve the site structure and public
product boundary. Do not change runtime behavior or historical receipts.

## Resolution authority

The user authorized the proposed document corrections and editorial cleanup, then
authorized commit, push and publication. Current code is the factual baseline;
runtime behavior is unchanged.
Existing root AGENTS.md governs historical receipts and evolving source contracts.
The disclosure gate continues to govern public naming. Historical image evidence
must not be represented as current-HEAD or stable-release acceptance.

## Page tasks

Each row is one document task, including its Chinese counterpart. Paths are under
`docs-site/docs/`. New pages must receive sidebar and translation entries.

| Order | Document | Correction / completion criterion |
|---|---|---|
| 01 | `02-getting-started/first-sandbox-run.md` | Correct `--reconcile`; complete signed prerequisites; prove subscription and instance readiness separately from health (F01). |
| 02 | `02-getting-started/enrollment.md` | Scope mandatory enrollment to signed lane; point to standalone identity and its restrictions (F02). |
| 03 | `01-introduction/what-is-custos.md` | Introduce both delivery lanes and their support/evidence boundaries (F02). |
| 04 | `01-introduction/architecture-at-a-glance.md` | Add offline data flow without implying canonical authority (F02/F09). |
| 05 | `02-getting-started/standalone-sandbox.md` (new) | Executable identity/bootstrap/validate/publish/start/status/stop tutorial with non-secret fixtures (F02). |
| 06 | `02-getting-started/first-deployment-spec.md` | Distinguish signed DeploymentSpec from OfflineDeploymentSpec; fix health-versus-apply claim (F01/F02). |
| 07 | `03-concepts/trading-modes.md` | Explain repeated enabled-mode and actual host limits; lane × mode admission (F03). |
| 08 | `09-reference/cli.md` | Cover all 11 parser commands, missing start flags and correct requirements/defaults (F04). |
| 09 | `09-reference/configuration.md` | Document standalone metadata, offline state and production-state-root; lane-specific requirements (F02/F04). |
| 10 | `02-getting-started/installation.md` | Correct NT Python/platform requirements and image-contract verification scope (F05/F10). |
| 11 | `07-engines/nautilus-trader.md` | Pin current fork version; include SoDEX and one-node-per-loop boundary (F03/F05). |
| 12 | `07-engines/sodex.md` (new) | Sandbox/testnet setup, connectors, credential format, valuation failures and unsupported live boundary (F05/F11). |
| 13 | `03-concepts/live-execution-gate.md` | Current per-mode venue matrix, lane scope and existing test pointers (F02/F05/F11). |
| 14 | `05-trust-model/live-execution-is-gated.md` | Same admission truth; fix deleted test reference without weakening guarantees (F05/F11). |
| 15 | `08-toolkit/overview.md` | Current version, strict typing closure, historical inventory scope and current handoff summary (F05/F06/F07). |
| 16 | `08-toolkit/artifact-signing.md` | Distinguish composed verification path from blocked production acceptance; reference release-policy provisioning (F06). |
| 17 | `10-release-governance/release-status.md` (new) | One revision-scoped status table: stable releases / toolkit RC / runtime candidates / deployed acceptance (F06). |
| 18 | `10-release-governance/changelog.md` | Correct current release characterization and link status authority (F06). |
| 19 | `10-release-governance/semver-lts.md` | Separate support commitments from RC/historical image publication (F06). |
| 20 | `10-release-governance/upgrade-paths.md` | Remove obsolete command/publication denials; add NT 2/operator upgrade boundaries (F02/F05/F06). |
| 21 | `06-integration/contract-versioning.md` | Consumer receipts now exist; preserve V1 policy; evolving source uses Git/CI, historical receipts remain historical (F06/F10). |
| 22 | `09-reference/json-schema.md` | Cover 14 current schemas and categorize local/offline/candidate contracts (F08). |
| 23 | `06-integration/gateway-contract-v1.md` | Canonical owner schema remains distinct from local offline spec; fix CLI denials (F02/F08). |
| 24 | `09-reference/nats-subjects.md` | Resolve ARX product/service contradiction; add offline and signal subjects without exposing backend topology (F08/F09). |
| 25 | `06-integration/consuming-runner-fact.md` | Explain separate strategy signal envelope, signature, sequence and idempotency; do not change 13-kind union (F08). |
| 26 | `03-concepts/runner-fact.md` | Bound canonical stream description and link separate signal/offline observations (F08). |
| 27 | `04-operator-guide/deployment.md` | Three runnable lanes/recipes, release-policy provision, actual infrastructure ownership (F01/F02/F04). |
| 28 | `04-operator-guide/credential-vault.md` | Scope enrollment statements; explain offline testnet credential-scope binding (F02/F11). |
| 29 | `04-operator-guide/readiness-health.md` | Distinguish daemon health, command subscription, engine readiness and reliable valuation (F01/F11). |
| 30 | `04-operator-guide/troubleshooting.md` | Add exact offline/NT 2 errors and diagnostic steps, including absent prices and loop occupancy (F03/F11). |
| 31 | `04-operator-guide/emergency-playbook.md` | Offline stop/trip/recovery; inspect venue positions and orders before restart (F11). |
| 32 | `05-trust-model/safety-survives-disconnect.md` | Signed aggregate policy versus offline per-deployment ceiling; trip latch and recovery (F11). |

## Drift prevention task

Add an independent PR verification job, preserving main-only deployment. Generate
or compare command inventory from argparse, dependency versions from package metadata,
schema inventory from files, and connector support from the venue registry. Check
literal source/test references and English/Chinese page parity. Trigger this check
when relevant source or authority metadata changes, not only when docs-site changes.
Do not add current-byte source hashes to historical receipt gates.

## Acceptance

- Run `npm run verify` in docs-site for both locales.
- Validate command examples against the actual parser and use current non-secret
  fixtures; demonstrate the signed example actually enables reconciliation.
- Exercise standalone sandbox with an isolated local broker and separate state root.
  Testnet verification requires provisioned credentials and must be reported separately.
- Check mode and connector tables against registry and host capabilities.
- Re-run `make check-authority` if authority/ownership/protocol descriptions change;
  verify no historical receipt was rewritten.
- Review all affected English/Chinese pages for semantic parity, not just terminology.
- Report source/parser, local sandbox, Docker, testnet and production evidence as
  separate results. No green documentation gate can close production readiness.

## Current execution status

- Updated 44 existing pages in both locales and added four paired pages:
  standalone sandbox, offline testnet, SoDEX, and release status (49 per locale).
- Rewrote explanatory prose around operator tasks; retained the public naming and
  disclosure boundary. Corrected the home page's stale translation notice.
- Generated CLI, package/version, connector and schema tables from current source.
- Added reference drift tests and a read-only PR/main verification workflow; kept
  publishing on the existing main -> gh-pages path.
- Fixed the existing example checker to discover nested subcommands from argparse
  instead of a hard-coded list. It now detects invalid nested command names too.
- `npm run verify`: passed for both locales, disclosure, CJK, terminology, source
  references and TypeScript. Final post-edit check is recorded in the close-out.
- Documentation/reference/LTS/example tests: 28 passed.
- `make check-authority`: passed. Runtime source and historical receipts unchanged.
- Real local sandbox exercise passed with a disposable Docker NATS JetStream broker,
  actual sops/age, standalone identity, vault put/verify, validate/publish, daemon
  readiness, generation 1 running and generation 2 stopped. The simulation host
  did not connect to a venue or run trading strategy code. Test resources stopped.
- SoDEX testnet documentation is deliberately limited: the adapter needs account
  fields absent from the strict offline spec. No runnable CLI recipe or end-to-end
  acceptance is claimed for that incomplete input path.
- Signed deployment prerequisites were checked against argparse; actual ARX-backed
  execution and real venue/testnet acceptance were not run.
- GitHub Pages configuration confirmed by GitHub API: branch `gh-pages`, path `/`,
  CNAME `custos.alephain.com`. Existing docs-deploy workflow publishes this branch.
- Main was already six commits ahead of origin/main at task start; those commits
  are the local offline scope and Nautilus readiness fixes documented by this change.

## Publication close-out

- Content commit: `35c304c81b467fc00c379a43e736e040ba9e622d`, pushed to main.
- Read-only documentation CI passed: https://github.com/the-alephain-guild/custos/actions/runs/35444139923
- Documentation deploy passed: https://github.com/the-alephain-guild/custos/actions/runs/35444139993
- Pages source commit: `0a77d80d2d2b0c821aaa37cc83a93ae264b8b4d8`; GitHub Pages build status `built`.
- HTTPS content checks passed for 10 public routes across English and Chinese:
  both home pages, both standalone sandbox pages, both CLI pages, English Nautilus,
  Chinese SoDEX, English release status and Chinese offline testnet.
- Public site: https://custos.alephain.com/ and https://custos.alephain.com/zh-Hans/
- These checks verified HTTP delivery and rendered HTML text. Browser automation
  timed out, so no interactive/visual browser acceptance is claimed.
- No runtime source, historical receipt, runner package release or container
  publication was changed by the documentation commit. The six pre-existing local
  source commits were pushed with the existing main history as announced.
- This close-out changes only the execution record; it does not republish site content.


## Public-content boundary correction (2026-09-19)

The user clarified that dependency origins and custom build information must not
appear in public documentation. Updated both locales and the home page to describe
installation, API compatibility and operating limits only. Removed internal release
coordination, candidate inventories and historical publication coordinates from the
public status/reference pages. Source dependency pins and historical receipts remain
unchanged.

The reference generator now publishes the engine API series and an explicitly
classified public schema set. The disclosure gate also checks decoded translation
JSON and rejects internal build/record details even when a generic exemption is
present. Validation: 30 related pytest tests, 46 disclosure regression tests, both
locale builds/typecheck, source-reference/CJK/terminology checks passed; targeted
scan of 224 generated HTML/JS/JSON assets found no removed details.

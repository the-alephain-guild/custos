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

### Added

- **Conformance vectors for consumers.** `docs/authority/conformance/` publishes
  `runner-fact-batch-v1.vectors.json` and `strategy-canonical-json-v1.vectors.json`,
  each with a `.sha256` sidecar. Every vector carries the exact bytes of one
  message as a JSON string and the outcome a consumer must reach: accepted with
  the payload digest, envelope digest and signing preimage, or refused with an
  error class. They cover whole-second and fractional instants, former
  six- and nine-digit instant spellings, a NETTING slot reopened by another
  order, a reused `position_id`, exponent, trailing-zero and binary-float
  `realized_pnl` values, a null required member, an unknown member, string
  escapes and the largest signed 64-bit sequence. The vectors are rendered by
  the runner's own fact paths and `scripts/generate_contract_conformance.py
  --check` regenerates them byte for byte; they are signed with a published
  test-only key.
- **`scripts/contract_vendor.py`** vendors another repository's contract assets
  at a contract revision and checks the vendored bytes offline, without naming
  any commit.

### Changed

- **Contract schemas carry a contract revision.** The RunnerFact batch,
  `StrategyArtifactRefV1`, `StrategyArtifactPreImportVerificationReceiptV1` and
  `RuntimeCandidateAcceptanceV1` schemas gain two top-level annotation keywords,
  `x-contract-id` and `x-contract-revision` (all at revision 1). The revision is
  raised only when the wire shape or the meaning of a field changes; a change to
  descriptive text keeps it. JSON Schema validators ignore both keywords, so no
  document's validity changes. The schemas, the RunnerFact and strategy asset
  indexes and their sidecars change; historical receipts are unchanged.
- **The RunnerFact batch schema refuses a signed zero (contract revision 2).**
  Every signed decimal pattern accepted `"-0"`, which the runner never writes
  (it renders zero as `"0"`) and which a consumer reading a decimal type cannot
  keep. The pattern now excludes it, so the RunnerFact batch contract moves to
  revision 2, and the conformance vectors gain `decimal-negative-zero`, refused
  as a non-canonical decimal. A consumer syncs revision 2; no receipt is written.
- **The RunnerFact batch schema states the consumer's acceptance domain
  (contract revision 3).** Each identifier the consumer bounds now carries its
  `maxLength` (64, 128, 256 or 512) and a pattern that refuses control
  characters, and every decimal pattern stops at 28 fractional digits and 29
  digits. JSON Schema counts characters and cannot compare a mantissa with
  2**96 - 1, so the exact bounds stay in the fact builders; the two vectors the
  schema admits but the consumer refuses say so with `"schema_outcome":
  "accept"`. The terminal valuation's open-position instrument keeps its
  non-empty rule. The vectors gain the identifier byte and character bounds and
  three control characters, six decimal spellings the runner never writes
  (trailing zero, exponent, signed zero, leading plus, 29 fractional digits and
  a JSON number) in an execution fill price, an equity amount and a position
  snapshot mark price, the edges of the decimal type, and the two required
  nullable members (`client_order_id`, `causation_id`) left out; the error
  classes gain `decimal_out_of_range`. Consumers sync revision 3; no receipt is
  written.
- **The runner refuses to sign identifiers and decimals its consumer refuses.**
  The consumer rejects an identifier (venue, venue trade, order and position
  ids, client order id, instrument, order type and category, balance asset, fee
  id and kind, watermark, reconciliation period) that is blank, longer than its
  bound in UTF-8 bytes (64, 128, 256 or 512 by field), or carries a control
  character, and it holds decimals in a type with a 96-bit mantissa and at most
  28 fractional digits. A fact carrying such a value was signed and then
  refused at ingest together with its whole batch. Every wire identifier and
  every wire decimal is now checked against those bounds before signing; until
  now only cash flows were. A value derived from a conversion rate with more
  than 28 fractional digits (1 / 60000 has 32) is now refused when it reaches a
  fact instead of being signed; arithmetic off the wire is unchanged.
- **Consumers pin contract revisions instead of exchanging receipts.**
  `docs/authority/contract-revisions-v1.json` lists each contract other
  repositories vendor with its revision, wire fingerprint, assets and vectors.
  The strategy contract generator no longer requires the two consumers'
  acceptance receipts or names their commits, and no longer writes the strategy
  handoff receipt; that receipt and the two vendored consumer receipts stay as
  historical evidence and are checked only for shape. A wire change now raises
  the contract revision and ships updated vectors, with no new receipt.
- **The contracts Custos consumes are pinned by revision too.** The deployment
  service's strategy release resolution, runner safety policy, runner machine
  request, runner NATS transport authority and runner deployment command assets
  are synced by `scripts/contract_vendor.py` at revision 1 and recorded in
  `docs/authority/vendor/contract-pins-v1.json`; `make check-authority` and the
  release workflow check them offline. This vendors the producer's current
  bytes: the safety policy schema now lists the `VUSDC`, `VBTC` and `VETH`
  settlement currencies, and the NATS transport authority golden grants the
  strategy-signal publish subject. The runner command and machine-request
  consumer indexes and receipts, and the receipts vendored from the deployment
  service, stay as historical evidence checked only for shape; they no longer
  pin current source or vendored bytes, so editing a consumer source file no
  longer fails the authority gate. `scripts/generate_runner_machine_request_consumer_assets.py`
  is removed.

### Fixed

- **The RunnerFact batch schema names the subject a runner publishes on.** Its
  `x-custos-invariants.subject` read
  `crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}`,
  a superseded shape the runner never publishes on and the deployment service
  refuses. It now reads `crucible.runner.fact.v1.{tenant_id}.{runner_id}.{trading_mode}`,
  the subject `RunnerFactAuthority.subject` builds. The schema, its sidecar and
  the RunnerFact asset index change; no wire field changes.
- **The documentation site names the same RunnerFact subject.** The consumer
  guide and the NATS subject reference, in English and Simplified Chinese,
  still showed the superseded subject; they now show
  `crucible.runner.fact.v1.{tenant_id}.{runner_id}.{trading_mode}` and say that
  the deployment instance travels in the signed batch header. The disclosure
  gate's exemption for the subject literal follows the new spelling, and its
  self-test now refuses the superseded one.
- **Independent venue ledger reads name the runner in their User-Agent.** The
  SoDEX and OKX ledger transport sent no User-Agent, so urllib's default
  `Python-urllib/<version>` went out and the SoDEX gateway answered 403, even on
  its public endpoints: start-up reconciliation failed with
  `venue ledger request failed` on testnet and would on live. Every read now
  sends `custos/<installed custos-runner version>`, kept when a caller adds
  headers and replaced only when a caller names its own User-Agent. The
  Binance ledger, which hard-coded `custos-runner/0.3`, sends the same value.

## [0.7.0] - 2026-10-05

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.7.0`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.7.0`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. The engine (`2.0.0rc5+sodex.2`) and
the matching strategy toolkit (`0.1.0rc9`) are those of 0.6.2; strategy
repositories do not need to move. The runner state database is unchanged.

Minor, not patch, with two **Breaking** changes: the capability manifest gains a
required `runtime` object, with new contract assets, and a capability receipt
published by an earlier release no longer starts a runner. A runner run from the
container image needs `CUSTOS_RUNTIME_IMAGE_DIGEST` to declare that image. A
deployment service that consumes the capability manifest must take this
release's contract assets; once it requires the `runtime` object, a runner on an
earlier release can no longer publish its capability. Move runners to 0.7.0,
publish the capability again, then restart.

### Changed

- **Breaking:** a runner capability declares the runtime it runs, as a required
  `runtime` object in the capability manifest: `distribution`, `image_digest`,
  `source_revision`, `engine` and `engine_version`. `publish-capability` writes
  it from what the publishing process observes and refuses a manifest that
  already carries a different one. The signed lane starts only when its capability
  receipt declares the runtime running now, so a receipt published by an earlier
  release no longer starts a runner: publish the capability again, then restart.
  After changing the image or the engine, publish again before restarting.
- **Breaking:** a runner running from the container image must be given the
  image's multi-platform index digest in `CUSTOS_RUNTIME_IMAGE_DIGEST`
  (`sha256:` and 64 lowercase hexadecimal digits). The runner reads the image's
  source revision and the installed NautilusTrader version itself, and refuses to
  publish or start when the variable is set but the image was not built from a
  commit. Without the variable, as when running from source, the runtime is
  declared `development` with no digest and no revision; its facts never qualify
  a runtime. The variable is not the live admission digest: live still requires
  `--runtime-image-digest` with its promotion receipt, and the capability must
  declare that same image.
- Publishing a capability and starting the signed lane need the NautilusTrader
  engine installed, since the runtime names the engine version.

### Fixed

- A strategy that closes and reopens a position on the same instrument under a
  netting account now signs each closed lifecycle under its own `position_id`.
  NautilusTrader keeps one position id per instrument and strategy under netting
  and reuses it on every reopen, and the runner derived `position_id` from that
  id alone, so every later close of the instrument repeated the first close's
  identity and the deployment service refused to settle it. The identity now
  also covers the order that opened the lifecycle and the instant it opened. The
  wire shape of `position_closed` is unchanged; a replayed close still derives
  the same fact. The RunnerFact batch schema now states this contract in the
  description of `position_id`: it identifies exactly one position lifecycle.
- **Every signed time is written in one RFC 3339 form.** Times in runtime log
  facts, capital-basis observations and deployment lifecycle facts were written
  with a fixed nine-digit fraction (`.632000000Z`) or with six digits
  (`.632000Z`), while the deployment service writes the same instant as
  `.632Z`: no fraction for a whole second, otherwise the shortest of three, six
  or nine digits that keeps every non-zero digit. They now use that form, as
  every other signed fact already did. The same rendering applies when the
  runner checks the digest of a NATS transport credential; a credential whose
  times fell on a whole millisecond was refused with
  `runner NATS authority digest mismatch` and is now accepted. Wire values stay
  valid RFC 3339 and parse to the same instant.
- **A strategy signal's `input_digest` commits to the `occurred_at` it is signed
  with.** The digest input used the event time with a fixed nine-digit fraction,
  while the signed `occurred_at` carries the form above at microsecond
  precision. Both are now the same string. As a result, **the same signal has a
  different `input_digest` than under 0.6**; a consumer that compares digests
  across the upgrade must not treat that as a changed signal. Signal identity
  (`fact_id`, `trace_id`) and every other field are unchanged.

### Known limitations

- The description of `position_closed.position_id` in the RunnerFact batch
  schema says the derivation applies under a netting account. The runner derives
  `position_id` the same way under every account type, and the derivation also
  covers the deployment's fact stream scope. The description will be corrected
  in a later release.
- `position_id` is unique only while the engine position id, the order that
  opened the lifecycle and the instant it opened, at microsecond precision, are
  unique together. If a partial fill of one order opens a lifecycle, another order
  closes it, and the rest of the first order opens the next lifecycle within the
  same microsecond, both lifecycles carry the same `position_id`. The deployment
  service then refuses the second close as a reused identity: nothing is settled
  twice, but settlement of that instance stops. Many venues stamp fills only to
  the millisecond, so the window is narrow but not zero. A later release adds the
  opening fill's trade id to the derivation, which changes `position_id` values
  again.
- A passing build, health probe or sandbox run does not establish production
  readiness.

## [0.6.2] - 2026-10-03

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.6.2`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.6.2`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. The engine (`2.0.0rc5+sodex.2`), the
matching strategy toolkit (`0.1.0rc9`) and the contract assets are those of
0.6.1; deployment services and strategy repositories do not need to move. The
runner state database is unchanged.

A stop that holds a position now confirms its terminal valuation when the read
is otherwise reliable. What a confirmed valuation promises is the type of what
the read takes in: no input arrives as a binary float. On a margin account the
equity includes unrealized PnL that NautilusTrader computes in binary floating
point and rounds to the settlement currency, and in a simulated account the
balance carries realized PnL computed the same way; no bound on that rounding
is promised. See [Decimal money arithmetic](https://custos.alephain.com/trust-model/exact-money-arithmetic).

### Fixed

- **A stop that still holds a position can confirm its terminal valuation.**
  The stop-boundary read refuses inputs that arrive as binary floats, and it also
  fetched each position's opening average, which NautilusTrader reports only as a
  float. Every stop that held a position, on a margin or a cash account, was
  therefore signed `valuation_unconfirmed` with `valuation_unreliable`. The
  terminal valuation carries equity and, per position, quantity, mark price and
  currency; the boundary read now reads only those and leaves the cost basis out.
  Periodic reads, including the valuation checkpoint, still carry the average.
  An input the boundary does read that arrives as a float is still refused.

## [0.6.1] - 2026-10-03

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.6.1`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.6.1`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. The engine (`2.0.0rc5+sodex.2`), the
matching strategy toolkit (`0.1.0rc9`) and the contract assets are those of
0.6.0; deployment services and strategy repositories do not need to move.

The runner state database gains one table, created at the first start. Its
schema version is unchanged, and a 0.6.0 runner still opens a database that has
it.

### Changed

- **An unbound kept stop is logged once, not at every restart.** A stop, pause
  or archive kept for an instance the capability does not bind can stay kept
  for good, for example when the instance was stopped before it was ever bound.
  `durable_command_recovery_skipped` is now logged for it at the first restart
  that finds it unbound, and again only for a newer generation or a changed
  reason; a small table in the runner state database remembers which. A kept
  start that is unbound is still logged at every restart. The command is kept
  and reported as before.

### Fixed

- **A restart recovery that meets an unavailable dependency is retried.** A
  kept command recovered after a restart has no delivery that could be
  redelivered, so a start that failed for a reason that may pass (an artifact
  registry dropping one blob request, a release service not answering yet) was
  logged once and left until the next restart. It is now retried on the
  schedule a delivered command gets, logging
  `durable_command_recovery_retry_scheduled` with the reason code, the error
  message of the runner's own dependency error and the types of its causes. A
  start still failing after the last attempt is refused and reported as
  `retry_exhausted`; a stop, pause or archive still failing stays kept. A newer
  command for the instance cancels a recovery waiting to retry.

## [0.6.0] - 2026-10-02

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.6.0`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.6.0`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. The engine (`2.0.0rc5+sodex.2`) and
the matching strategy toolkit (`0.1.0rc9`) are unchanged; strategy repositories
do not need to move.

Minor, not patch: RunnerFact V1 gains a fact kind, and lifecycle outcomes for a
start that could not be carried out change their reported state. A deployment
service that consumes RunnerFact must take this release's contract assets
before it issues a runner a capability that declares terminal valuation, and
must accept `stopped` on the outcomes described under Changed.

### Added

- **A signed stop carries the instance's terminal valuation.** RunnerFact V1
  gains `RunnerInstanceTerminalValuationFact.v1`, a settlement fact committed in
  the same batch as, and directly after, the lifecycle fact of an applied stop.
  It is either `confirmed`, with equity, open positions and the oldest price time
  read at the stop boundary (after the node's run ended and before it was
  disposed), or `valuation_unconfirmed`, with the valuation nulled and one
  `reason_code` chosen by a fixed precedence. A month close the stream still owed
  is committed first, and the instance's fact stream is sealed afterwards: only
  its archive may follow. Before this, an instance stopped mid-month received no
  settlement close, and the last periodic equity snapshot missed whatever changed
  before the stop.
- **The fact is opt-in through the capability.** The runner produces it only
  when its capability receipt declares the settlement flag
  `terminal_valuation: "v1"` together with the new kind; a capability without it
  keeps committing the lifecycle fact alone, and a receipt issued before the flag
  still loads. A manifest that declares the kind without the flag, or the flag
  without the kind, is refused.
- **Contract assets.** The RunnerFact batch schema, the capability manifest and
  its receipt golden, the parity matrix, the asset index and a new
  `runner-fact-terminal-boundary-golden-v1.json` change or are added. The
  single-batch golden keeps its shape and fact content, but its header digest
  and signature and the signing preimage change with the capability manifest; a
  consumer that pins these assets takes them together with this release.
  Historical receipts are unchanged.
- **The strategy toolkit gains `strategy_registration_scope()`**, a registry
  private to the code it wraps. The runner loads every verified artifact inside
  its own scope, so two artifacts that register the same strategy name from
  different sources, such as a new version of a strategy, load one after the
  other in one process instead of the second being quarantined. Within one
  artifact, one name registered from two sources is still refused. Outside a
  scope (backtesting, the offline lane) nothing changes.
- **A local image can be built on a reviewed engine wheel.**
  `make docker-build-local-v030` accepts `LOCAL_NAUTILUS_WHEEL`, with its
  SHA-256 and engine source revision, checks the wheel's digest, name and version
  before installing it over the locked runtime, and labels the image with both.
  It is for local development only; the release lock and the published image are
  unchanged.

### Changed

- **A start the runner cannot carry out is refused and reported, not
  quarantined.** A runner process runs one instance at a time. A start for a
  second instance is now refused before its artifact is loaded, with
  `runtime_capacity_rejected:runner_engine_occupied`, and signed as a
  `retry_exhausted` lifecycle outcome; the running instance is untouched. A start
  that arrives while the running instance is being stopped, including while its
  node is still being reaped, is retried and goes ahead once the node is
  released.
- **A command the runner cannot yet sign for is kept and acknowledged.** The
  capability receipt is read once at startup; a command for an instance it does
  not cover used to be loaded anyway, and its refusal, which could not be signed,
  was negatively acknowledged again and again, holding every later command
  (including a stop) behind it. The command is now acknowledged, kept as the
  desired state and logged as `runner_command_awaiting_capability_binding`;
  nothing is loaded or started. Acknowledgement now has three outcomes: applied,
  refused, and kept awaiting a binding.
- **Terminal lifecycle outcomes report `lifecycle_state: "stopped"`.** Every
  terminal outcome the lifecycle supervisor commits, including a refused or
  failed start, reports the state the runner observes after it stopped the
  engine or never started it, rather than the state the command asked for.
- **Stop timeouts change meaning.** A run that ignores the graceful stop is
  cancelled; one that also outlasts the cancellation may still be running, so
  nothing is committed: the stop is recorded as awaiting its reap and the
  delivery is acknowledged rather than retried into exhaustion. When the node is
  reaped the stop commits with `stop_timeout`. An applied stop therefore always
  means the node is no longer running, and one stop command ends once.

### Fixed

- **A restart reports what it cannot start.** A runner restarting with several
  instances to recover used to recover them concurrently, racing for the one
  engine node, and a final failure was only logged locally while the control
  plane kept seeing the instance as running. Recovery now runs one instance at a
  time, the instance that was running first; a final refusal (an artifact or
  authority rejection, a quarantined activation, an occupied runner) is
  committed as `retry_exhausted` and signed.
- **A kept stop, pause or archive is reported once a restart covers its
  instance.** After a restart whose capability covers the instance, the kept
  command is applied and its signed lifecycle outcome reported exactly once. A
  kept command that a newer command for the same instance has replaced is not
  reported, and recovering it never stops the newer generation.
- **A stop whose node outlived the process is signed as a process exit.** A
  stop still awaiting its reap when the process ended is settled after the
  restart with `process_exit_before_confirmation`. The same reason, with
  `stop_effective_at` null, now applies to a stop that finds no node for an
  instance last applied running in an earlier process and never started in this
  one: that node ended with the earlier process, at a moment nobody observed. A
  stop after a pause, or after a node that ended inside this process, keeps
  `engine_not_running_at_stop`.

## [0.5.2] - 2026-09-29

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.5.2`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.5.2`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity.

### Fixed

- **A spot deployment is no longer stopped on its first fill.** A spot venue
  publishes no mark price, and SoDEX spot publishes no order book either, so
  the runner had no price for a spot position or balance: on 0.5.1 a SoDEX spot
  deployment's first fill tripped the fallback breaker within seconds, and the
  runner flattened the position and stopped the deployment. The portfolio
  snapshot, the cash account's balance conversion and the order valuation now
  take the mark, then the mid, then the last trade; with none of the three the
  snapshot still fails closed and the order is still refused.
- **The strategy toolkit subscribes a spot pair's trades rather than its mark
  price.** A perpetual keeps its mark price. SoDEX spot answered the mark
  subscription with an error every five seconds, and the last trade the runner
  now values at is only there when the trades are subscribed.

## [0.5.1] - 2026-09-29

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.5.1`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.5.1`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. It is the first published release on
the 0.5 line: the `v0.5.0` tag exists, but its release run stopped at the runtime
contract gate and published no image, no signature and no release notes. The
engine, the toolkit match (`0.1.0rc9`) and the contracts are those described
under 0.5.0.

### Fixed

- The support policy lists the 0.5 line (first release 2026-09-29, EOL
  2027-09-29). The release gate refuses a released line without a support
  window, and the table had no row for it.

## [0.5.0] - 2026-09-29

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.5.0`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.5.0`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity. The strategy toolkit that matches it is
`0.1.0rc9`, built from the same engine; a strategy repository moves to both
together, since a release built on `0.1.0rc8` names the previous engine and is
refused.

### Changed

- **The engine is NautilusTrader `2.0.0rc5+sodex.2`.** The runner image, the
  `custos-strategy-toolkit-nautilus` distribution and the publisher's build
  environment install the fork release `guild-v2.0.0rc5+sodex.2`, pinned by the
  sha256 of each platform wheel. The V1 strategy contracts change the
  `engine_version` constant in place; a strategy release, a consumer receipt
  or a golden that still names `2.0.0rc5+sodex.1` is refused. What changed in
  the engine: SoDEX historical bars carry the venue's real timestamps and the
  forming bar is dropped, so a strategy's warmup request on SoDEX no longer
  comes back empty; amendments go through the venue's replace route; the
  execution client proves at startup that the wallet lists the key it signs
  with; commission is charged at the account's own rates.
- **RunnerFact V1 venue ledger snapshots carry cash flows and wallet scope.**
  The contract changes in place; a consumer must take the new schema and golden
  together with this release.
  - A chunk has a fifth section, `cash_flows`: internal and sub-account
    transfers, transfers to and from another user of the venue, deposits and
    withdrawals. Amounts and fees are never negative; `kind` gives the
    direction. A withdrawal carries its receiving `destination` when the venue
    reports one.
  - Every balance names its `wallet_type` and `sub_account`; the manifest
    carries the snapshot's `sub_account` and a `cash_flows_count`.
  - `completeness` gains `cash_flows_complete`, reported separately from the
    other four flags. Collectors report cash flows as empty and incomplete
    until transfer history is collected.
  - Valuation reads one declared wallet; evidence that supplies valuation
    balances without naming their wallet is refused.
  - A `cash_flow_id` is at most 256 UTF-8 bytes with no control characters,
    and a cash-flow `amount` or `fee` must fit a 96-bit mantissa with at most
    28 fractional digits. Zero is always written as `0`, never `-0`.

## [0.4.2] - 2026-09-27

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.4.2`
and as source; there is no PyPI package. Call the reusable publishing workflow at
`@v0.4.2`; a runner or deployment service that trusts releases published through
it lists that tag as the workflow identity.

### Fixed

- The publisher accepts GitHub's immutable OIDC subjects, in which each name
  carries its numeric id (`repo:owner@1/name@2:ref:refs/heads/main`). A
  repository with immutable subjects enabled could not publish: its release
  stopped at assembly. A refused subject is now quoted in the error.

## [0.4.1] - 2026-09-27

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.4.1`
and as source; there is no PyPI package. It fixes the strategy release publisher,
which the first release built from a strategy template could not get through.
Call the reusable workflow at `@v0.4.1`; a runner or deployment service that
trusts releases published through it lists that tag as the workflow identity.

### Fixed

- The publisher no longer takes a template strategy's section titles, such as
  `parameters._section`, for parameters; a config that declares no parameters
  is refused.
- The publisher's build environment installs `msgspec`, which every release
  declares as a dependency and template strategies import; strict typing of a
  template strategy failed without it.

## [0.4.0] - 2026-09-27

It ships as the signed container image `ghcr.io/the-alephain-guild/custos:v0.4.0`
and as source; there is no PyPI package. Its headline is that strategy releases
no longer come from one producer: any strategy repository can publish signed
releases through Custos's reusable publishing workflow, and runners trust the
producers their operator lists. Two changes need action from a consumer, each
marked **Breaking** below: list the development producers a runner accepts, and
rebuild releases whose manifest lacks `trading_scope`.

### Added

- **Strategy release publisher.** The package `custos-strategy-publisher`
  (`packages/custos-strategy-publisher`, command `custos-strategy-release`)
  builds a strategy's release twice and refuses a difference, type-checks it
  strictly against the pinned toolkit and engine, signs it keylessly through
  Sigstore's public instance and publishes it to the producer's own GitHub
  Container Registry package. It derives every name a release carries from the
  strategy template's layout, `strategies/<category>/<name>`.
- **Reusable publishing workflow.** `.github/workflows/publish-strategy-release.yml`
  (`workflow_call`) publishes for the repository that calls it. Its jobs hold
  only what each needs: reading the signing identity, building without write
  access, and signing and publishing. The signing certificate names this
  workflow at the tag it was called with and the calling repository; a runner
  trusts a producer by that pair.
- `release-policy issue` accepts more than one producer: repeat `--issuer`,
  `--workflow-identity` and `--source-repository` together, once per producer.
- `start --development-producer-repository OWNER/REPOSITORY` (repeatable, or
  `CUSTOS_DEVELOPMENT_PRODUCER_REPOSITORIES`) lists the repositories whose
  sandbox-only development sources the runner accepts.

- The offline lane publishes local telemetry for the operator's own tools:
  a snapshot of each running deployment's status, open positions and open
  orders every 10 seconds on
  `arx.<tenant>.telemetry.<runner-label>.<spec-id>.snapshot`, and each fill and
  closed position on `.fill` and `.position_closed`. It is unsigned and best
  effort, it is not RunnerFact evidence, and nothing in the lane reads it.
- The offline lane's observed stream keeps at most 10,000 messages per subject;
  `nats bootstrap --profile standalone` brings an existing stream up to the bound.

### Fixed

- The offline lane stops on SIGTERM and SIGINT. It used to ignore SIGTERM until
  its supervisor killed it, and a stop it did notice left every deployment
  running until the process died, so a strategy's own stop handler -- which
  cancels the orders it left resting -- never ran. Each running deployment is now
  stopped through the engine within 75 seconds, its status is published as
  `stopped`, and the process exits non-zero when a deployment could not be
  confirmed stopped. Give the container a stop grace period above 75 seconds.

### Changed

- Releases ship on GitHub only. The release workflow no longer has a PyPI
  job, and the post-publish check `make verify-release` verifies the signed
  image alone.
- **Breaking:** a signed-lane runner no longer accepts development sources from
  a producer named in its code. List each accepted repository with
  `--development-producer-repository`; with none listed, development
  deployments are refused. The release contract (`StrategyReleaseBomV1`, the
  signed statement and the detached attestation) is now owned by Custos, with
  every producer publishing under it; schema identifiers are unchanged.
- **Breaking:** `StrategyManifestV1` requires `trading_scope` --
  `{"connector": ..., "pairs": [...], "leverage": ...}` -- the connector, pairs
  and leverage the release was validated to trade. A release whose manifest has
  no `trading_scope` is refused; rebuild and republish it.
- A signed deployment runs only on the trading scope its strategy declares. The
  runner compares the strategy's connector, claimed instruments and leverage
  with the deployment's before the engine starts, and quarantines a mismatch at
  once with `strategy_trading_scope_mismatch`. A signed strategy config may not
  carry its own `trading` section.

### Known limitations

- The make targets `docker-build-local-v030` and `verify-local-v030` keep their
  names; they build and check `custos-runner:v0.4.0`.
- OKX and SoDEX do not route through the venue proxy yet; a deployment on them is
  refused while a proxy is configured. SOCKS proxies are not supported.
- Docker image bytes are not reproducible bit for bit.
- A passing build, health probe or sandbox run does not establish production
  readiness.

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

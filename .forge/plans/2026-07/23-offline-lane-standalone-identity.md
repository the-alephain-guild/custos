# 23 — Standalone identity for the offline lane

> **Status**: ⏳ In Progress
> **Created**: 2026-07-29
> **Project**: custos (`tesseract-trading/custos/`)
> **Wave**: independent — completes Plan 21's lane; does not touch the Crucible-signed path
> **For Claude**: `/forge:execute`; single session (4 Tasks)
> **Depends on**: Plan 21 ✅ Completed (offline lane), Plan 13 ✅ Completed (`deploy/custos` support)
> **Blocks**: `philosophers-stone/deploy/custos` — the lane cannot start without this
> **multi_session_scope**: false

## Context

Plan 21 restored the offline delivery path and closed with the consumer-side end-to-end
run unexecuted. That run was performed on 2026-07-29 against an image built at `b75e3bc`.
Delivery is genuinely restored — `deployment validate` accepts the spec PS actually
renders, digest `c46e88e146f37595b2adddf61bf1399e65d283c0980e229dac6958693a4bf833` — but
the runner never reaches it:

```
Runner startup authority check failed: /home/custos/.arx/runner.toml is not a
v1 runner authority document (missing credential_id, credential_valid_until,
credential_version, enrolled_at, machine_key_id, machine_vault_path;
unexpected enrolled_at_ns, long_term_credential)
```

The lane was restored; its precondition was not. Plan 21's five-break table counted the
delivery surface and never listed startup identity — the one mention of it
(`21-…:112`) cites PS's `init_runtime.py:147` only to argue where the live boundary sits.

### What v1 requires and what exists

`src/custos/core/runner_toml.py:25` defines the contract:

| field | constraint |
|---|---|
| `tenant_id` | non-empty, no whitespace |
| `runner_id` | **UUID**, not nil |
| `backend_url` | absolute URL (scheme + hostname) |
| `credential_id` | **UUID**, not nil |
| `credential_version` | positive int |
| `credential_valid_until` | timestamp |
| `machine_key_id` | must start with `ed25519-` |
| `machine_vault_path` | absolute path |
| `enrolled_at` | timestamp |

PS writes five fields, two of which v1 rejects outright
(`deploy/custos/scripts/init_runtime.py:156-157` emits `long_term_credential` and
`enrolled_at_ns`). Re-running `make init-runtime` cannot fix this: the producer itself
predates the contract.

Custos has no standalone path either. `enroll` is the only producer of a v1 document, and
it requires `--backend` (`enroll.py:46`), a reachable service (`_require_secure_backend`,
`:80`) and a server-issued nonce (`:86`) — precisely the dependency this lane exists to
avoid. `runner_toml.py` exposes `RunnerToml` and `write` but no standalone constructor.

The material is already local, though. `generate_machine_identity()`
(`core/machine_credential_vault.py:106`) generates an Ed25519 keypair in-process and
derives the `ed25519-` key id with no network involved. Enrollment's remote half exists to
have an authority *attest* the identity, not to create it. A lane that answers to no
authority does not need that half.

### The runner_id shape change breaks the consumer's subject

`start.py:199` passes `metadata.runner_id` — read from `runner.toml`, therefore a UUID —
into the offline reconciler, which builds its status subject from it
(`offline/reconciler.py:332`). The consumer subscribes to
`arx.${TENANT_ID}.deployment_status.${RUNNER_ID:-ps-supertrend}.${SPEC_ID}`
(`philosophers-stone/deploy/custos/docker-compose.yaml:87`).

So a v1 identity publishes to `arx.local.deployment_status.<uuid>.supertrend-sandbox`
while the probe waits on `…ps-supertrend…`. `wait-status` would hang even after startup
succeeds. This plan must state which side moves, and Task 3 exists to prove the two agree
rather than to assume it.

## Goal

An operator with no backend, no network and no enrollment can produce a v1 runner
authority document that the runner accepts, restricted to the offline lane's modes, and
the consumer's status probe observes the runner under the identity it subscribes to.

## Non-goals

- No weakening of `RunnerToml.__post_init__`. Standalone identities satisfy the same v1
  contract as enrolled ones; they are not a laxer shape.
- No standalone path to `live`. The generated identity is refused for live by the same
  `custos.offline.mode_guard` Plan 21 established.
- No change to `enroll`. The authority-attested path keeps its current behaviour and
  remains the only way to obtain an identity a backend will recognise.
- No claim that a standalone identity is enrollment. It is deliberately not attested and
  must be distinguishable from an enrolled one by inspection.

## Tasks

### Task 1: Mark the identity as unattested, and refuse it where attestation is required

**RED**: a test asserts that a standalone document is distinguishable from an enrolled one
without consulting the backend, and that the signed lane refuses it.

**Implementation**: decide the marker before generating anything, so an unattested
identity can never be mistaken for an enrolled one. Whatever carries it — a reserved
`credential_id` namespace, an explicit field, or the `backend_url` sentinel PS already
uses (`http://standalone.invalid`) — must survive `RunnerToml` round-tripping and be
checkable offline. Route the signed lane's identity load through that check so an
unattested document cannot start it.

**Verify**:

```bash
uv run pytest tests/test_runner_toml.py -v -k "standalone or unattested"
```

**Commit**: `feat(custos): make an unattested runner identity self-evident`

### Task 2: Generate a standalone v1 identity locally

**RED**: tests assert the generated document satisfies `RunnerToml.__post_init__`, that the
Ed25519 private key is written with the same permissions the vault uses, that regenerating
over an existing document refuses rather than silently replacing an identity the vault
material is bound to, and that no network call is attempted.

**Implementation**: add the generator behind the Task 1 marker, reusing
`generate_machine_identity()` rather than re-deriving key handling. `runner_id` and
`credential_id` are locally generated UUIDs; `credential_valid_until` and `enrolled_at`
come from the local clock; `machine_vault_path` points at the operator's vault root.

Expose it where the operator already is. `enroll` requires `--backend` and belongs to the
attested path, so this is a separate entry point rather than a flag on that one — a lane
that answers to no authority should not enter through the command whose purpose is
answering to one.

**Verify**:

```bash
uv run pytest tests/test_runner_toml.py tests/cli/ -v -k "identity"
uv run --package custos-runner arx-runner <entry point> --help
```

**Commit**: `feat(custos): generate a standalone runner identity without a backend`

### Task 3: Agree with the consumer on the status subject

**RED**: a test asserts the subject the offline reconciler publishes equals the subject the
consumer subscribes to, built from the same identity — currently false, since one is a
UUID and the other is `ps-supertrend`.

**Implementation**: pick the direction deliberately and record it. Two defensible answers:
carry a separate lane-facing runner label distinct from the UUID `runner_id`, or have the
consumer subscribe by the generated UUID. Prefer whichever leaves the consumer's
`docker-compose.yaml` able to name the runner without reading generated state, and say why
in the deviation entry.

Pin the agreed subject in a contract test so the two repositories cannot drift apart
silently — the last two breaks in this lane were both invisible until something was run.

**Verify** (`tests/test_offline_subject_contract.py` is created by this Task; no such file
exists today):

```bash
uv run pytest tests/test_offline_subject_contract.py -v
```

**Commit**: `test(custos): pin the offline status subject the consumer observes`

### Task 4: Prove it from the consumer, not from here

**RED**: none — this Task is the end-to-end proof Plan 21 deferred and this plan exists
because deferring it hid a break.

**Implementation**: from a clean `runtime/.arx`, generate a standalone identity, bootstrap
the vault, and run the consumer's own entry point. Record the observed generation and
phase, not merely that the command exited zero.

**Verify**:

```bash
cd philosophers-stone/deploy/custos
make init-runtime STRATEGY=supertrend MODE=sandbox TENANT_ID=local   # updated producer
make bootstrap-vault STRATEGY=supertrend TENANT_ID=local
make start STRATEGY=supertrend MODE=sandbox TENANT_ID=local
```

reaching `wait-status` with the target generation. If PS's `init_runtime.py` must change to
call the new entry point, that change belongs to a PS-side plan; name it here and do not
edit PS from this repository.

**Commit**: `docs(custos): record the offline lane's first end-to-end run`

## Verification

- [ ] `make lint` clean
- [ ] `make check-authority` green, including `verify_offline_lane`
- [ ] `make test` — no new failures against the current baseline (833 passed / 0 failed at
      `b75e3bc`)
- [ ] `make verify-local-v030` builds an image carrying the generator
- [ ] The consumer reaches `wait-status` with the target generation — the only evidence
      that this lane works
- [ ] A standalone identity is refused by the signed lane, proven by test, not by argument
- [ ] `MODE=live` remains refused with a standalone identity present

## Red-line gate satisfaction

Filled at close-out, one row per red line, per lesson #40.

| red line | code_coverage | runtime_wire | defer_status | follow_up |
|---|---|---|---|---|
| 0.1 Key/KEK never leaves the process | | | | |
| 0.2 G6 host gate not bypassed | | | | |
| 0.3 Reconcile outage ≠ stop | | | | |
| 0.4 Decimal money math | | | | |

Note for 0.1: this plan generates a private key. State where it is written, with what
permissions, and that it never enters a log, an argument vector or a NATS payload.

## Deviations and improvements

### Decisions taken before implementation (CEO, 2026-07-29)

**Task 1 marker — `backend_url` sentinel, not a new field.** A new key cannot exist without
changing the contract: `runner_toml.py:89` compares `set(document) != set(_FIELDS)`, an exact
set. Adding to `_FIELDS` invalidates every enrolled document until `enroll` also emits the
field — which contradicts this plan's own non-goal "No change to `enroll`". The sentinel needs
neither. It is also not merely a convention: `_require_secure_backend` (`enroll.py:230-236`)
admits only `https://` or `http://` on loopback, so **`enroll` cannot produce
`http://standalone.invalid` today**, and `.invalid` is reserved by RFC 2606 so no real backend
can ever hold that name. The two sets are disjoint by an existing mechanical check rather than
by agreement. PS already writes and asserts this value (`Makefile:224`,
`init_runtime.py:114`). The check reads the URL's host, not the literal string, so
`https://x.invalid` and a trailing slash are unattested too — near-misses fail closed.

**Task 3 direction — a lane-facing label distinct from the UUID.** `start` gains
`--runner-label`, used only for the offline lane's status subject; `runner_id` stays the v1
UUID. The consumer keeps naming the runner literally (`RUNNER_ID ?= ps-$(STRATEGY)`,
`Makefile:23`) and adds one line to the `custos-runner` service. The rejected option — having
the consumer subscribe by the generated UUID — requires PS to read generated state in three
places (Makefile, compose, `assert_identity`), which is what this plan's own criterion says to
avoid.

### Corrections to this plan's context, found by Foundation Scan

1. **`enroll`'s nonce is client-generated, not server-issued.** `enroll.py:86` is
   `challenge_nonce = uuid4()`. The real backend dependency is `_require_secure_backend` plus
   `_post_enrollment`, which is what issues the credential and `enrolled_at`. The conclusion
   stands; the stated reason did not.
2. **The machine vault is missing too, not just `runner.toml`.** `start.py:246` loads
   `MachineCredentialVault(...).load()` and calls `assert_binding` **before** the offline
   branch at `:251`. So the generator must produce a machine credential as well, and PS's
   `bootstrap-vault` does not cover it — that target creates the *venue* credential.
3. **The machine credential must be minted locally.** `MachineCredential` requires
   `machine_credential` to start with `rkc1.` (`machine_credential_vault.py:209`); enrolled
   runners receive it from the backend. A standalone identity has to generate one, from
   `secrets`, within the charset `runtime_log_fact.py:81` redacts.
4. **Two anchors drifted.** `runner_id` reaches the offline lane at `start.py:215`, not `:199`
   (`:199` passes it to `_build_host`); the subject is built at `reconciler.py:331`.

### To record at close-out

- If PS's `init_runtime.py` needs to change, name the PS-side plan here. Custos does not
  edit that repository.

## Close-out Report

（执行完成后填写）

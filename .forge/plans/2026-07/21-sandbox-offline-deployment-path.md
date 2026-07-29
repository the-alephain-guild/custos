# 21 — Restore a sandbox-only offline deployment path for strategy-logic verification

> **Status**: 🔲 Not started
> **Created**: 2026-07-29
> **Project**: custos (`tesseract-trading/custos/`)
> **Wave**: independent — unblocks the Philosophers Stone offline harness, does not touch the Crucible-signed path
> **For Claude**: `/forge:execute`; single session (3 Tasks, no wire or async changes)
> **Depends on**: Plan 13 ✅ Completed (`deploy/custos` support), Plan 17 ✅ Completed (vault JSON contract)
> **Blocks**: `philosophers-stone/deploy/custos` on any image built after `8c4454f`
> **multi_session_scope**: false

## Context

`8c4454f` (2026-07-21, "refactor(custos): converge canonical V1 runtime contracts")
removed the unsigned deployment path along with the work that introduced the
Crucible-signed one:

| Removed | Role |
|---|---|
| `src/custos/cli/subcommands/deployment.py` | `deployment publish` / `deployment validate` |
| `docs/gateway-contract/v1/deployment_spec.schema.json` | the spec contract those commands validate against |
| `docs/gateway-contract/v1/samples/deployment_spec_sandbox.json` | the sandbox sample |
| `tests/test_cli_deployment.py` | the CLI's own coverage |
| `tests/test_deployment_contract.py` | the spec contract coverage |
| `tests/test_deployment_reconciler.py` | reconciler coverage for the unsigned path |

The companion tests went with it, which is why nothing turned red on removal.

That commit carries a one-line message and cites no plan. Plan 19 does name
`deployment_spec_id` and `deployment_spec_digest`, but those are fields inside the new
`CrucibleRunnerDeploymentCommandV1`, not the removed CLI, so the removal was not within
its scope. It reads as incidental to building the signed path rather than a decision to
end the unsigned one.

The path has a live consumer. Philosophers Stone drives it from
`philosophers-stone/deploy/custos/docker-compose.yaml` — the `spec-publisher` service
runs `deployment publish --spec-file /runtime/deployment.json`, and the Makefile's
`spec-validate` target runs `deployment validate`. Custos supported this deliberately in
Plan 13 ✅, which exists precisely to back that harness.

The consumer is not a duplicate of the v1.team lane. It answers a different question:

| | offline harness | v1.team lane |
|---|---|---|
| question | is the strategy logic right | does PS + arx + Crucible + Custos work as one system |
| strategy source | bind-mounted from the PS checkout | wheel artifact plus manifest |
| delivery | `DeploymentSpec` published straight to NATS | Crucible-signed deployment command |
| external services | none | Crucible backend plus CONTROL PostgreSQL |

Philosophers Stone ratified that split on 2026-07-28 and recorded it as normative in
`philosophers-stone/.forge/plans/README.md` (lane table) and in its Plan 56, which now
forbids deleting or absorbing the offline harness. That decision was made without
noticing this removal one week earlier; this plan closes the gap from the Custos side.

### Why not simply pin an old image

`514c130` — the commit immediately before the removal — still has the CLI and already
contains the reliable-equity fix (`0117cc2`, 2026-07-15), so an image built there works
today. Pinning there is a workaround with no exit: every later Custos fix becomes
unreachable, including `c4b2eee` (market orders reporting an absent limit price).

### Why the trust model does not require removal

`arx-runner start` now takes `--crucible-domain-public-key` / `--crucible-domain-key-id`
and reconciles signed desired state. Requiring a signature is correct for testnet and
live. It is over-broad for sandbox, where the operator owns the machine, the identity and
the NATS instance, and where nothing the harness publishes can reach a funded venue. The
authority note in `.forge/README.md` already scopes new contracts to
sandbox/testnet/live; this plan asks only for sandbox.

## Goal

An operator running entirely on their own machine can publish a `DeploymentSpec` to a
local NATS instance and have a runner reconcile it, with no Crucible backend, no signing
authority, and no network dependency beyond the venue the strategy itself trades on.

## Non-goals

- No change to the Crucible-signed path. Plan 19 keeps sole ownership of it.
- Nothing here may run in testnet or live. Mode is refused at the boundary, not by convention.
- No return of the removed `crucible-runner-deployment-command-v1` artifacts. Those belong
  to the signed path and were correctly superseded.
- No new schema generation. The restored spec contract is the exact bytes from `514c130`
  unless a Task states otherwise.

## Tasks

### Task 1: Fail-closed mode boundary before restoring any entry point

**RED**: a test asserts the unsigned path refuses `testnet` and `live`, and that the
refusal happens before a spec is parsed, published or written anywhere.

**Implementation**: introduce the mode guard first, so the restored surface can never
exist in a state where it accepts a non-sandbox mode. The guard rejects on the mode
carried in the spec and, independently, on any mode passed at the command line — a spec
claiming sandbox must not be publishable into a testnet transport, and vice versa.

**Verify** (`tests/test_cli_deployment.py` is restored in Task 2; create it here with the
mode cases only, so Task 1 stands on its own):

```bash
uv run pytest tests/test_cli_deployment.py -v -k "mode"
```

**Commit**: `feat(custos): refuse non-sandbox modes on the unsigned path`

### Task 2: Restore `deployment validate` and `deployment publish` behind that guard

**RED**: tests reject a spec whose digest does not match its content, a spec for a
strategy directory that does not exist, a malformed generation, and a publish attempt
whose transport subject does not match the tenant and strategy in the spec.

**Implementation**: restore from `514c130` — `src/custos/cli/subcommands/deployment.py`,
`docs/gateway-contract/v1/deployment_spec.schema.json`,
`docs/gateway-contract/v1/samples/deployment_spec_sandbox.json`,
`tests/test_cli_deployment.py` and `tests/test_deployment_contract.py` — then wire them
through the Task 1 guard. Keep the restored bytes identical where the surrounding code
still permits it; where the CLI framework moved on since `8c4454f`, adapt the plumbing and
record the deviation rather than reshaping the contract.

`tests/test_deployment_reconciler.py` is deliberately out of scope: the reconciler it
covered was reshaped by Plan 19 and restoring that file would re-assert a superseded
internal structure. Only the publish/validate surface is restored.

Register the subcommand under the same name PS already invokes. Anything else is a silent
break for a consumer this plan exists to serve.

**Verify**:

```bash
uv run pytest tests/test_cli_deployment.py tests/test_deployment_contract.py \
  tests/test_gateway_contract_v1_samples.py -v
uv run --package custos-runner arx-runner deployment validate \
  --spec-file docs/gateway-contract/v1/samples/deployment_spec_sandbox.json \
  --strategy-dir <any existing directory>
```

**Commit**: `feat(custos): restore the sandbox deployment publish and validate commands`

### Task 3: Pin the contract so the next convergence cannot remove it silently

**RED**: a contract test fails if the `deployment` subcommand disappears from the CLI
surface, if the spec schema file is absent, or if the sandbox sample stops validating.

**Implementation**: add the assertions to `tests/test_gateway_contract_v1_samples.py`,
which already pins gateway-contract surfaces. State in the docstring that Philosophers
Stone's `deploy/custos` harness is the consumer and that removing this surface breaks it,
so the next refactor meets a red test rather than a silent break — the previous removal
was invisible precisely because the covering tests were deleted in the same commit.
Record the offline path in `.forge/README.md` next to the authority note, scoped to
sandbox.

**Verify**:

```bash
make lint && make test
```

**Commit**: `test(custos): pin the sandbox offline deployment surface`

## Verification

- [ ] `make lint` clean
- [ ] `make test` — no new failures against the pre-existing baseline. Two failures are
      known-red before this plan and unrelated to it:
      `test_runner_fact_contract_v1.py::test_v1_inventory_is_complete_and_byte_pinned`
      and `test_runner_policy_contract_consumer.py::test_runner_policy_pins_one_v1_producer_handoff`,
      both asserting cross-repository receipt commit pins.
- [ ] `make verify-local-v030` builds an image from this branch
- [ ] End-to-end from the consumer side, which is the only proof that matters here:
      ```bash
      cd philosophers-stone/deploy/custos
      make start STRATEGY=supertrend MODE=sandbox TENANT_ID=local
      ```
      reaches `wait-status` with the target generation, on an image built after `8c4454f`
- [ ] `MODE=testnet` and `MODE=live` are refused by the Task 1 guard, not merely absent
      from the templates

## Deviations and improvements

- Record here if the restored CLI cannot be byte-identical to `514c130` because the
  subcommand framework changed, including what was adapted and why.
- If Custos decides against restoring this path, this plan becomes the record of that
  decision. Say so explicitly here, and open a follow-up in Philosophers Stone to retire
  its offline harness and reverse the 2026-07-28 lane decision, so the two repositories do
  not disagree about whether the lane exists.

## Close-out Report

（执行完成后填写）

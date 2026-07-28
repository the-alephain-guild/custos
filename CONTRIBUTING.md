# Contributing to custos-runner

Thanks for looking. `custos-runner` is intentionally a small, single-purpose
daemon; contributions welcome, but the review bar is high because every
byte in the wheel ends up on operators' machines, holding the keys to
their exchange accounts. This document covers the practical mechanics; the
shape of the system is in [`README.md`](README.md) and at
[custos.alephain.com](https://custos.alephain.com).

## Prerequisites

- **Python >=3.11** (>=3.12 additionally, if you are working on the
  NautilusTrader engine)
- **`uv`** — the sole Python package manager here
- **git**
- **sops** and **age** — only if you touch vault handling
- a **NATS server** — only if you run transport integration tests

## Ground rules

- **The four guarantees are unnegotiable.** Keys never leave the host,
  live execution is always gated, local safety survives a disconnect, and
  money arithmetic is exact. They are described at
  [custos.alephain.com/trust-model/red-lines](https://custos.alephain.com/trust-model/red-lines)
  and enforced in review. Any PR that even looks like it might loosen one of
  them needs an explicit design discussion before code review.
- **English source artifacts.** Comments, log strings, exception
  messages, commit messages, and identifiers are English. The rationale
  is that deployment hosts do not reliably render CJK and log output must
  stay greppable across the ecosystem. A pre-commit hook (`scripts/check-code-english.py`)
  refuses to stage CJK characters in newly-added lines.
- **TDD is the flow, not a suggestion.** Every behavioural change lands
  with a failing test first, then the minimal implementation. Run
  `make help` for the available targets; `make verify` is the gate a PR has
  to pass.

## Local setup

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos

# Bootstrap the pre-commit hook (English guard + basic hygiene).
bash scripts/install-hooks.sh

# Install dev + engine + release-engineering extras as you need them.
make install               # base dev extras
make install-nt            # + nautilus (needs Python 3.12+)
make install-lts           # + sigstore / pytest-docker

# Green baseline sanity check.
make verify                # fmt-check + lint + baseline pytest
```

`uv` is the sole Python package manager; do NOT reach for `pip` or
`poetry`. `uv.lock` is committed so builds are reproducible.

## Running tests

```bash
make test                  # full pytest (may include known-fail nautilus)
make test-baseline         # green baseline (make verify's inner call)
make test-docker           # docker-marker gates; needs a Docker daemon
```

Slow / CI-only / docker tests are marker-gated so `make verify` doesn't
require any external infra. The full marker registry is in
`pyproject.toml [tool.pytest.ini_options].markers`.

## Adding a dependency

```bash
uv add <package>                 # runtime dependency
uv add --optional dev <package>  # development dependency
```

`uv.lock` updates automatically and must be committed in the same change.
A reproducible build is part of the non-custodial argument — an auditor has
to be able to determine exactly what they are running.

## Before you open the PR

- [ ] `make verify` is green
- [ ] the change respects `.claude/rules/code-style.md`
- [ ] none of the four guarantees is weakened — see the grep probes in
      `.claude/rules/verification.md`:
  - no key or KEK reaches a log, a message, or an HTTP body
  - no cloud SDK dependency was added
  - no `float` entered a money path
  - engine execution admission is not bypassed
- [ ] if you changed a wire contract (envelope or subject), you also updated
      `tests/test_crucible_nats_client.py` and `tests/test_nats_transport.py`,
      and re-ran `scripts/generate_wire_fixtures.py`
- [ ] if you changed something an authority document records, that document is
      updated in the same commit — run `make check-authority`
- [ ] commit messages follow Conventional Commits with the `custos` scope
- [ ] `git status --short` shows only the files you intended to stage

### Which guarantee does my change touch?

| If you are changing… | Read first |
|---|---|
| anything that logs, publishes or sends a field | keys never leave the host |
| `engines/nautilus/host.py` or a venue adapter | live execution is always gated |
| the disconnect path in the command runtime | safety survives a disconnect |
| a price, quantity or notional calculation | money arithmetic is exact |

All four are stated in `.claude/rules/mandatory-rules.md`. The emergency
response to a problem with any of them is to stop creating live deployments and
fall back to sandbox or testnet — never to bypass the guarantee.

Some source files are pinned byte-for-byte by the authority asset indexes under
`docs/authority/`. Editing one of those — even to fix a comment or reformat it —
breaks the evidence chain. Check before you edit:

```bash
grep -rho '"src/custos/[^"]*\.py"' docs/authority/ | sort -u
```

## Submitting a change

1. Fork + branch from `main`. Small, focused branches merge faster.
2. Land TDD-style: failing test first, then the minimal fix, then any
   needed refactor.
3. Use Conventional Commits: `feat(custos): …` / `fix(custos): …` /
   `docs(custos): …`. Subject in the imperative present tense.
4. Run `make verify` before push; CI runs the same target on PRs.
5. Open the PR against `main` with a short "why" + "what changed"
   summary. Point at any authority document whose recorded behaviour you are
   changing (see `.claude/rules/authority-docs.md`).
6. Review is via GitHub PR + inline comments. Expect at least one
   round of "hmm, does this break red line X?" — it isn't personal.

## Security-related changes

If your PR touches vault handling, network egress, key derivation, or
anything else on the Non-Custodial red-line surface, please reach out via
GitHub Security Advisories (see [`SECURITY.md`](SECURITY.md)) BEFORE
opening the PR. Some fixes need to land as a private advisory + patch
release rather than a public review.

## License

By contributing you agree that your work is dual-licensed with the rest
of the project under Apache-2.0. See [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). No CLA — Apache-2.0's contribution grant is enough.

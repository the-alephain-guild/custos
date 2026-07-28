# Contributing to custos-runner

Thanks for looking. `custos-runner` is intentionally a small, single-purpose
daemon; contributions welcome, but the review bar is high because every
byte in the wheel ends up on operators' machines, holding the keys to
their exchange accounts. This document covers the practical mechanics; the
philosophical shape is in [`docs/domain.md`](docs/domain.md) and
[`README.md`](README.md).

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

## Submitting a change

1. Fork + branch from `main`. Small, focused branches merge faster.
2. Land TDD-style: failing test first, then the minimal fix, then any
   needed refactor.
3. Use Conventional Commits: `feat(custos): …` / `fix(custos): …` /
   `docs(custos): …`. Subject in the imperative present tense.
4. Run `make verify` before push; CI runs the same target on PRs.
5. Open the PR against `main` with a short "why" + "what changed"
   summary. Point at any authoritative doc (`docs/domain.md`, red-line
   references, ADR) whose behaviour you're changing.
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

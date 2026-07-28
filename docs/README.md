# `docs/` — authority records and contract assets

**This directory is not the documentation.** Documentation lives in
[`docs-site/`](../docs-site/) and is published at
[custos.alephain.com](https://custos.alephain.com).

What remains here is consumed by machines, or records a cross-repository
agreement. Two subdirectories, and nothing else belongs at this level:

| Path | Contents | Consumed by |
|---|---|---|
| `authority/` | Signed receipts, asset indexes, goldens, vendored counterparty assets, and the prose records of cross-repository contracts | `make check-authority`, `scripts/check-authority-docs.py` |
| `gateway-contract/` | JSON Schemas and samples for the versioned wire contract | code and the authority gate |

## Do not add product documentation here

If you are writing something an operator, integrator or auditor would read,
it belongs in `docs-site/docs/`. That site has a disclosure gate
(`docs-site/scripts/check-disclosure.mjs`) which runs before the build, because
a leak that reaches the published site is irreversible while a failed build is
not.

Everything that used to live under `docs/design/`, `docs/guides/` and
`docs/ops/` has been folded into that site. Re-creating those directories would
re-create the problem the consolidation removed: two copies of the same
explanation, drifting apart, with no way to tell which one is current.

## Why some prose stayed

Four Markdown files remain under `authority/`:

- `nats-transport-contract.md`
- `nautilus-host-contract.md`
- `strategy-toolkit-contract.md`
- `strategy-toolkit-provenance.md`

These are **not** customer documentation and must not be rewritten as if they
were. They record ownership boundaries and coordination agreements with other
repositories, and `authority-manifest.json` asserts that specific phrases appear
in them — including the names of internal counterparties.

That is exactly the vocabulary the published site forbids. The two gates would
contradict each other if these records were moved to the site: the authority gate
requires a phrase that the disclosure gate rejects. So they stay here, where
naming an internal counterparty is correct rather than a leak.

Before editing one, check what the manifest requires of it:

```bash
python3 -c "import json;[print(c['path'], c['must_contain']) for c in json.load(open('authority-manifest.json'))['required_claims']]"
make check-authority
```

# Custos documentation site

Docusaurus serves English and Simplified Chinese documentation at
`custos.alephain.com`. GitHub Pages reads the `gh-pages` branch root.

## Local setup and verification

Use Node.js 20+, npm, uv and Python 3.12. Install the base runner dependencies
from the repository root so reference checks can load the actual CLI parser.

```bash
uv sync --locked --extra dev
cd docs-site
npm ci
npm start
npm run start:zh
npm run verify
```

`verify` runs disclosure, Chinese wrapping and terminology tests/checks,
source-reference comparison, both locale builds and TypeScript. The source
check uses the existing Python environment and does not install or remove
engine extras. Output is in `build/` and `build/zh-Hans/`.

Run from the repository root to regenerate checked-in reference tables:

```bash
uv run python scripts/check-docs-site.py --write
uv run pytest tests/test_docs_site_reference.py -q
```

The generator reads argparse, public package metadata, venue declarations and
explicitly classified public schemas. Engine documentation exposes its API series,
not the private dependency version or build origin. Unknown schemas require a
visibility decision before the reference check can pass. Translated table labels live in `data/reference-labels.json`. Edit
explanatory prose around generated blocks, not the generated tables themselves.

## Publishing

`.github/workflows/docs-check.yml` verifies pull requests and relevant main
changes, including source changes that can invalidate documentation. It has
read-only permissions and never publishes.

`.github/workflows/docs-deploy.yml` builds on documentation/workflow changes
pushed to main, or manual dispatch. It checks the public content boundary and
source references, then publishes `build/` to `gh-pages` with
`peaceiris/actions-gh-pages`. `static/CNAME` preserves the custom domain.

After publishing, check the workflow revision, the `gh-pages` commit and the
actual public pages in both locales. A successful upload alone does not prove
that the CDN is serving the new content. This workflow does not publish runner
packages, containers or release tags.

## Writing for operators

Public names are Custos and ARX. Present ARX as one product; do not expose the
services, storage topology or internal coordination behind it. Internal authority
records may be used to verify facts, but do not copy their narrative or link to
internal receipt paths from the site.

Custos's own public source paths and tests may be cited when they help an auditor.
Keep exact CLI flags, wire subjects and signing domains. If a literal product
identifier needs a disclosure exemption, use a same-line `disclosure-ok` comment
with a specific reason. Do not exempt ordinary prose about private systems.

Do not publish dependency fork/origin information, custom build suffixes, internal
candidate identifiers, consumer handoff records, historical image coordinates,
source snapshot hashes or internal receipt/schema inventories. Keep those in the
repository's development records. The disclosure gate checks translations as well
as Markdown/TSX and does not allow an exemption to reintroduce these details.
Preserve operator-required platform constraints, command spellings, public wire
fields and actual support limits.

Use concise technical prose:

- Tutorials: prerequisites, commands, expected results, troubleshooting and stop.
- Reference pages: exact fields, defaults, choices, supported combinations and scope.
- Concepts: definition and boundary first, then a short reason where useful.
- Status pages: supported workflows, user-visible restrictions and release guidance.

Avoid repeated warnings, rhetorical contrasts, slogans and claims such as
“always” or “the only path” without specifying the lane and scope. Separate daemon
health, instance application, engine readiness and production acceptance. Preserve
historical receipts instead of refreshing them to match current source.

## Translation

Maintain the same page paths in `docs/` and
`i18n/zh-Hans/docusaurus-plugin-content-docs/current/`. Add new page ids to
`sidebars.js`; category translations are in `current.json`. Home-page strings are
in `i18n/zh-Hans/code.json`.

Chinese paragraphs occupy one source line; Han-to-Han soft wraps render unwanted
spaces. Translate prose naturally while preserving commands, paths and identifiers.

| English | Chinese |
|---|---|
| venue / exchange | 交易所 |
| artifact | 产物 |
| credential | 凭据 |
| provenance | 来源记录 |
| vault | 金库 |
| circuit breaker | 熔断器 |

## Examples and verification scope

`examples/standalone/` contains non-trading fixtures and a status reader used by
the standalone sandbox tutorial. That exercise uses a real broker and encrypted
vault with `sandbox-sim`; it does not validate strategy behavior or venue execution.
Keep testnet and signed end-to-end acceptance separate in any report.

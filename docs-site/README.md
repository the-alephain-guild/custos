# custos-docs-site

Documentation site for **custos.alephain.com** — the non-custodial execution
runner.


## Prerequisites

- Node.js **20+**
- npm (or yarn)

## Local development

```bash
cd docs-site
npm ci
npm start                    # opens http://localhost:3000 in English
npm run start:zh             # opens http://localhost:3000 in 简体中文
```

## Build

```bash
npm run build                # output → docs-site/build/
npm run serve                # preview the production build locally
```

## i18n

- Default locale: `en`
- Alternate: `zh-Hans`
- Regenerate translation JSON (adds new/removed keys):

  ```bash
  npm run write-translations -- --locale zh-Hans
  ```

Translation source: `i18n/zh-Hans/`.

## Versioning (deferred)

After content is stable and the site is deployed once, freeze v0.3.0:

```bash
npm run docusaurus docs:version 0.3.0
```

This snapshots `docs/` into `versioned_docs/version-0.3.0/` and adds
`docsVersionDropdown` to the navbar (uncomment in `docusaurus.config.js`).

## Deploy

CI (`.github/workflows/docs-deploy.yml`) builds on push to `main`
under `docs-site/**` and deploys to the `gh-pages` branch. Custom domain is
`custos.alephain.com`, set via `static/CNAME`.

## This site is customer-facing — read before writing

Everything under `docs/`, `i18n/` and `src/` is published to the public web.
Two rules follow from that, and both are enforced mechanically.

**1. Do not use internal documents as your narrative source.**

`docs/**.md` at the repo root, `CLAUDE.md`, `.claude/rules/` and `.forge/`
exist to divide responsibility between internal systems. Every sentence in
them may be true and still be exactly what must not be published. Use them to
*check facts*; do not use them as the *skeleton* of a chapter.

Write from the product surface instead: what an operator installs, runs,
configures and observes. If a capability has no operator-visible surface, do
not claim it.

**2. Never name what is not public.**

Custos and ARX are the only public names. Do not name other systems in the
ecosystem, internal storage or migration identifiers, cross-service mechanism
vocabulary, private repository paths, or internal plan / deviation / lesson
numbers.

Do NOT surface `docs/authority/*` receipts on the site — those are internal
artifacts. They may be referenced by digest.

### The gate

```bash
npm run check:disclosure     # scan the site
npm run test:disclosure      # regression-test the gate itself
npm run verify               # disclosure → build → typecheck
```

The gate scans the **full file including code blocks and HTML comments** — an
internal identifier pasted into a sample payload discloses just as much as one
written in prose. It runs first in CI, before the build, because a leak that
reaches `gh-pages` is irreversible while a failed build is not.

If a banned term genuinely belongs on the page — a published container
coordinate, a wire subject an integrator must subscribe to — append
`disclosure-ok: <reason>` on that line. The reason is reviewed; the escape
hatch is not a way to silence the gate.

## Naming discipline

Follow
[`the-alephain-guild.github.io/data/naming-authority.md`](https://github.com/the-alephain-guild/the-alephain-guild.github.io/blob/main/data/naming-authority.md):

- ARX canonical positioning: **"the neutral quant operating system"** (do not use aliases)
- custos canonical positioning: **"the non-custodial execution runner"**
- ARX phased integrations (Speculum, Athanor, Argus, Synedrion, etc.) MUST NOT
  be presented as current capabilities — obey Phase 3 / Phase 4 discipline

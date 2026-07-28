#!/usr/bin/env node
/**
 * Regression tests for the disclosure gate.
 *
 * Fixtures live in a throwaway directory so a failing test cannot disturb the
 * real docs tree. Run: node scripts/test-check-disclosure.mjs
 */

import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const GATE = path.join(HERE, 'check-disclosure.mjs');

const FRONTMATTER = '---\ntitle: fixture\n---\n\n';

async function runGate(body, filename = 'fixture.md') {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'custos-disclosure-'));
  try {
    await fs.writeFile(path.join(dir, filename), FRONTMATTER + body, 'utf8');
    try {
      const { stdout, stderr } = await execFileAsync('node', [GATE, dir]);
      return { code: 0, out: stdout + stderr };
    } catch (err) {
      return { code: err.code ?? 1, out: (err.stdout ?? '') + (err.stderr ?? '') };
    }
  } finally {
    await fs.rm(dir, { recursive: true, force: true });
  }
}

const CASES = [
  {
    name: 'system behind ARX is rejected',
    body: 'Signed desired state is produced by Crucible Rust before delivery.\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'strategy research repository name is rejected',
    body: 'Strategies are authored in philosophers-stone and vendored here.\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'sibling engine codename is rejected',
    body: 'A future adapter will supervise the Athanor binary as a subprocess.\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'ARX-side migration numbering is rejected',
    body: 'Aggregate caps arrive with migration 0117 upstream.\n',
    expect: (r) => r.code === 1 && /ARX-side storage and mechanism/.test(r.out),
  },
  {
    name: 'how ARX consumes facts is rejected',
    body: 'Each fact reaches a projector and lands in a PostgreSQL projection.\n',
    expect: (r) => r.code === 1 && /ARX-side storage and mechanism/.test(r.out),
  },
  {
    name: "custos's own durable queue is allowed",
    body: 'Facts are written to the durable outbox before the transport is touched.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: "custos's own source path is allowed",
    body: 'The reconciler lives in `src/custos/core/deployment_reconciler.py`.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: "custos's own test path is allowed",
    body: 'Covered by `tests/test_reconcile.py` and the failure-mode suite.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'internal plan number is rejected',
    body: ':::warning\n本章中文正文将在 Plan 20 T6 完成。\n:::\n',
    expect: (r) => r.code === 1 && /development process/.test(r.out),
  },
  {
    name: 'internal gate number is rejected',
    body: 'Live deployment must pass the G6 gate before reaching a venue.\n',
    expect: (r) => r.code === 1 && /development process/.test(r.out),
  },
  {
    name: 'bare internal task number is rejected',
    body: 'Applied state commits in the T4 transaction after the T3 verification.\n',
    expect: (r) => r.code === 1 && /development process/.test(r.out),
  },
  {
    name: 'internal deviation id is rejected',
    body: 'See DEV-08-RENUMBER-FROM-06B for the rationale.\n',
    expect: (r) => r.code === 1 && /development process/.test(r.out),
  },
  {
    name: 'internal decision attribution is rejected',
    body: 'The namespace moved per a CEO clean-break decision.\n',
    expect: (r) => r.code === 1 && /development process/.test(r.out),
  },
  {
    name: 'assistant configuration path is rejected',
    body: 'The four guarantees are defined in CLAUDE.md and .claude/rules/.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'planning directory is rejected',
    body: 'See .forge/plans/2026-07/20-custos-docs-site-scaffold.md for scope.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'code blocks are scanned, not just prose',
    body: '```json\n{ "upstream": "crucible-rust" }\n```\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'HTML provenance comments are scanned',
    body: '<!-- source: docs/engines/athanor.md -->\n\nEngine roadmap.\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'TSX pages are scanned',
    body: 'export default () => <a href=".forge/plans/x.md">plan</a>;\n',
    filename: 'page.tsx',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'escape marker suppresses a reviewed hit',
    body: 'Upstream is Crucible. <!-- disclosure-ok: reviewed internal note -->\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'public names Custos and ARX are allowed',
    body: 'Custos enrolls against ARX and holds venue credentials locally.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'ordinary version numbers are not mistaken for plan numbers',
    body: 'Custos 0.3.0 supports v1 of the gateway contract and pins Python 3.12.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'published container coordinate is exempt',
    body: 'Run `docker pull ghcr.io/the-alephain-guild/custos:v0.3.0` to fetch the image.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'published repository coordinate is exempt',
    body: 'Clone it: `git clone https://github.com/the-alephain-guild/custos.git`\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'signing preimage constant is exempt',
    body: '```text\ncrucible.runner.enrollment.pop.v1\ntenant_id=<tenant>\n```\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'fact subject an integrator must subscribe to is exempt',
    body: '```text\ncrucible.runner_fact.{mode}.{tenant_id}.{runner_id}.{deployment_instance_id}\n```\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'exemption does not cover ordinary prose about that system',
    body: 'Crucible validates the business rules before signing.\n',
    expect: (r) => r.code === 1 && /systems behind ARX/.test(r.out),
  },
  {
    name: 'exemption does not whitelist other org paths',
    body: 'See the-alephain-guild/synedrion for the council design notes.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'removed prose tree is rejected (design)',
    body: 'The full design lives in docs/design/reconcile.md.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'removed prose tree is rejected (guides)',
    body: 'See docs/guides/dev-guide.md for setup.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'removed prose tree is rejected (ops and moved files)',
    body: 'Deployment is in docs/ops/05-deployment.md and docs/lts-commitment.md.\n',
    expect: (r) => r.code === 1 && /assistant and workflow configuration/.test(r.out),
  },
  {
    name: 'surviving machine-asset directories are still allowed',
    body: 'Schemas live under docs/gateway-contract/v1/ and receipts under docs/authority/.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'clean page passes',
    body: 'The runner verifies signed desired state before starting a strategy.\n',
    expect: (r) => r.code === 0,
  },
];

let failed = 0;
for (const testCase of CASES) {
  const result = await runGate(testCase.body, testCase.filename);
  const ok = testCase.expect(result);
  process.stdout.write(`${ok ? '✅' : '❌'} ${testCase.name}\n`);
  if (!ok) {
    failed++;
    process.stdout.write(`   exit=${result.code}\n   ${result.out.trim().split('\n').join('\n   ')}\n`);
  }
}

if (failed > 0) {
  process.stderr.write(`\n${failed}/${CASES.length} disclosure gate test(s) failed\n`);
  process.exit(1);
}
process.stdout.write(`\n${CASES.length}/${CASES.length} disclosure gate tests passed\n`);

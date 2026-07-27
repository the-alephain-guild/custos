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
    name: 'internal system name is rejected',
    body: 'Signed desired state is produced by Crucible Rust before delivery.\n',
    expect: (r) => r.code === 1 && /internal system names/.test(r.out),
  },
  {
    name: 'strategy research repository name is rejected',
    body: 'Strategies are authored in philosophers-stone and vendored here.\n',
    expect: (r) => r.code === 1 && /internal system names/.test(r.out),
  },
  {
    name: 'future engine codename is rejected',
    body: 'A future adapter will supervise the Athanor binary as a subprocess.\n',
    expect: (r) => r.code === 1 && /internal system names/.test(r.out),
  },
  {
    name: 'internal migration numbering is rejected',
    body: 'Aggregate caps arrive with migration 0117 upstream.\n',
    expect: (r) => r.code === 1 && /internal storage and topology identifiers/.test(r.out),
  },
  {
    name: 'internal mechanism vocabulary is rejected',
    body: 'Facts are drained from the outbox and reach a PostgreSQL projection.\n',
    expect: (r) => r.code === 1 && /internal mechanism vocabulary/.test(r.out),
  },
  {
    name: 'private planning path is rejected',
    body: 'See .forge/plans/2026-07/20-custos-docs-site-scaffold.md for scope.\n',
    expect: (r) => r.code === 1 && /private repository paths/.test(r.out),
  },
  {
    name: 'private rules path is rejected',
    body: 'The four red lines are defined in CLAUDE.md and .claude/rules/.\n',
    expect: (r) => r.code === 1 && /private repository paths/.test(r.out),
  },
  {
    name: 'code blocks are scanned, not just prose',
    body: '```json\n{ "upstream": "crucible-rust" }\n```\n',
    expect: (r) => r.code === 1 && /internal system names/.test(r.out),
  },
  {
    name: 'HTML provenance comments are scanned',
    body: '<!-- source: docs/engines/athanor.md -->\n\nEngine roadmap.\n',
    expect: (r) => r.code === 1 && /internal system names/.test(r.out),
  },
  {
    name: 'TSX pages are scanned',
    body: 'export default () => <a href=".forge/plans/x.md">plan</a>;\n',
    filename: 'page.tsx',
    expect: (r) => r.code === 1 && /private repository paths/.test(r.out),
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
    name: 'internal plan number is rejected',
    body: ':::warning\n本章中文正文将在 Plan 20 T6 完成。\n:::\n',
    expect: (r) => r.code === 1 && /internal tracking identifiers/.test(r.out),
  },
  {
    name: 'internal deviation id is rejected',
    body: 'See DEV-08-RENUMBER-FROM-06B for the rationale.\n',
    expect: (r) => r.code === 1 && /internal tracking identifiers/.test(r.out),
  },
  {
    name: 'internal red-line numbering is rejected',
    body: 'This is required by red line 0.1 of the mandatory rules.\n',
    expect: (r) => r.code === 1 && /internal tracking identifiers/.test(r.out),
  },
  {
    name: 'ordinary version numbers are not mistaken for plan numbers',
    body: 'Custos 0.3.0 ships the T1 connector and supports v1 of the gateway contract.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'published container coordinate is exempt',
    body: 'Run `docker pull ghcr.io/the-alephain-guild/custos:v0.3.0` to fetch the image.\n',
    expect: (r) => r.code === 0,
  },
  {
    name: 'exemption does not whitelist other org paths',
    body: 'See the-alephain-guild/synedrion for the council design notes.\n',
    expect: (r) => r.code === 1 && /private repository paths/.test(r.out),
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

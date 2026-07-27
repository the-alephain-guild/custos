#!/usr/bin/env node
/**
 * CI gate: keep internal implementation detail out of the public custos site.
 *
 * The published product surface is Custos, the self-hosted execution runner,
 * together with ARX as the control plane it enrolls against. Every other system
 * in the ecosystem is internal implementation: which service owns a slice of
 * state, how storage is partitioned, and where the private planning documents
 * live are all detail that must not reach customer-facing documentation.
 *
 * Unlike a prose-only linter this gate scans the FULL file including code
 * blocks and HTML comments: an internal identifier pasted into a sample payload
 * or a `<!-- source: ... -->` provenance marker discloses just as much as one
 * written in body text.
 *
 * Escape hatch: append a same-line comment `disclosure-ok: <reason>` when a term
 * genuinely must appear. Use sparingly — the reason is reviewed.
 *
 * Usage:
 *   node scripts/check-disclosure.mjs [dir ...]
 *
 * Exit codes:
 *   0 = clean
 *   1 = disclosure found
 *   2 = runtime error
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

const DEFAULT_ROOTS = ['docs', 'i18n', 'src'];
const SCANNED_EXTENSIONS = new Set(['.md', '.mdx', '.tsx', '.ts', '.jsx', '.js']);

/**
 * Banned patterns grouped by what they would reveal. Each entry carries the
 * guidance an author needs to rewrite the line, not just a refusal.
 *
 * Custos and ARX are the two public names and are deliberately absent here.
 */
const BANNED = [
  {
    group: 'internal system names',
    patterns: [
      /\bCrucible\b/i,
      /\bcrucible[-_ ]rust\b/i,
      /\bthe[-_]crucible\b/i,
      /\bPhilosophers[-_ ]?Stone\b/i,
      /\bSpeculum\b/i,
      /\bSynedrion\b/i,
      /\bAthanor\b/i,
      /\bArgus\b/i,
      /\bAletheia\b/i,
      /\bGrimoire\b/i,
      /\bScriptorium\b/i,
    ],
    guidance:
      'Describe the capability, not the system that provides it. Custos and ARX are the only public names.',
  },
  {
    group: 'internal storage and topology identifiers',
    patterns: [/\bsearch_path\b/, /\bDSN\b/, /\bmigration\s+0\d{3}\b/i, /\barx_(live|sim|control)\b/],
    guidance:
      'Storage partitioning and migration numbering are implementation detail. Describe the guarantee, not the topology.',
  },
  {
    group: 'internal mechanism vocabulary',
    patterns: [/\boutbox\b/i, /\binbox\b/i, /\bsaga\b/i, /\bPostgreSQL projection\b/i, /\bprojector\b/i],
    guidance:
      'Describe the observable behaviour instead — what the operator can rely on, not how it is delivered upstream.',
  },
  {
    group: 'private repository paths and commands',
    patterns: [
      /CLAUDE\.md/,
      /\.claude\/rules/,
      /\.forge\//,
      /codex\/projects/,
      /ecosystem-authority/,
      /authority-manifest/,
      /make\s+check-authority/,
      /\bthe-alephain-guild\b/,
      /\bdocs\/authority\//,
    ],
    guidance:
      'External readers cannot open our private material. Link to public documentation or point at the in-product location.',
  },
  {
    group: 'internal tracking identifiers',
    patterns: [
      /\bplan\s+\d{1,3}[a-z]?\s*T\d/i,
      /\bplan\s+\d{1,3}[a-z]?\b/i,
      /\bDEV-\d{2,3}[A-Z-]/,
      /\blesson\s+#\d+/i,
      /\bred[- ]line\s+0\.\d\b/i,
      /\bADR-\d{3}\b/,
      /\bTier\s+[A-D]\b/,
      /\b\d+\s*BC\b/,
    ],
    guidance:
      'Internal plan, deviation and lesson numbers are meaningless to readers. Describe the capability or name the guarantee instead.',
  },
];

/**
 * Lines that legitimately carry an otherwise-banned token because they are part
 * of the public product surface. A published container coordinate is something
 * an operator must type to pull the image, so it is not disclosure.
 */
const CONTEXT_EXEMPTIONS = [/ghcr\.io\/the-alephain-guild\/custos/];

const ALLOW_MARKER = /disclosure-ok:/;

async function walk(dir) {
  const out = [];
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name === 'build' || entry.name === '.docusaurus') {
      continue;
    }
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...(await walk(full)));
    } else if (SCANNED_EXTENSIONS.has(path.extname(entry.name))) {
      out.push(full);
    }
  }
  return out;
}

/** Report every banned hit in a file, honouring same-line escape markers. */
export function scanDisclosure(text) {
  const findings = [];
  const lines = text.split('\n');
  lines.forEach((line, index) => {
    if (ALLOW_MARKER.test(line)) return;
    const exempt = CONTEXT_EXEMPTIONS.filter((rule) => rule.test(line));
    for (const { group, patterns, guidance } of BANNED) {
      for (const pattern of patterns) {
        const match = line.match(pattern);
        if (match && exempt.some((rule) => rule.source.includes(match[0]))) continue;
        if (match) {
          findings.push({
            line: index + 1,
            group,
            term: match[0],
            guidance,
            excerpt: line.trim().slice(0, 90),
          });
          break;
        }
      }
    }
  });
  return findings;
}

async function main() {
  const roots = process.argv.length > 2 ? process.argv.slice(2) : DEFAULT_ROOTS;
  const files = [];
  for (const root of roots) {
    files.push(...(await walk(path.resolve(ROOT, root))));
  }

  let total = 0;
  const byGroup = new Map();

  for (const file of files) {
    const rel = path.relative(ROOT, file);
    const findings = scanDisclosure(await fs.readFile(file, 'utf8'));
    for (const finding of findings) {
      process.stderr.write(
        `❌ ${rel}:${finding.line} [${finding.group}] "${finding.term}"\n` +
          `   ${finding.excerpt}\n` +
          `   → ${finding.guidance}\n`,
      );
      total++;
      byGroup.set(finding.group, (byGroup.get(finding.group) ?? 0) + 1);
    }
  }

  if (total > 0) {
    process.stderr.write(`\n${total} disclosure issue(s) across ${files.length} file(s):\n`);
    for (const [group, count] of byGroup) {
      process.stderr.write(`  ${count} × ${group}\n`);
    }
    process.stderr.write(
      '\nIf a term genuinely must appear, append `disclosure-ok: <reason>` on that line.\n',
    );
    process.exit(1);
  }

  process.stdout.write(`✅ disclosure check: ${files.length} file(s) scanned, 0 issue(s)\n`);
  process.exit(0);
}

main().catch((err) => {
  process.stderr.write(`check-disclosure fatal: ${err?.stack ?? err}\n`);
  process.exit(2);
});

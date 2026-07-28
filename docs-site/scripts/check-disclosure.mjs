#!/usr/bin/env node
/**
 * CI gate: keep the boundary between what Custos is and what sits above it.
 *
 * Custos is open on purpose. Operators are expected to audit it, and many of
 * them become contributors — so its own implementation is not a secret. Source
 * paths, module names and internal mechanisms of the runner are exactly what an
 * auditor came to read, and they belong in the documentation.
 *
 * What must not appear is the layer above. ARX is a commercial, closed product
 * presented as one thing: this documentation may describe the contract Custos
 * holds with it — what is signed, what is verified, what is exchanged — but not
 * which services implement it, how its state is partitioned, or how it consumes
 * what Custos emits.
 *
 * The third category is not about layers at all: plan numbers, gate numbers,
 * lesson references and assistant configuration are development process. They
 * are meaningless to a reader regardless of which layer they describe.
 *
 * Unlike a prose-only linter this gate scans the FULL file including code
 * blocks and HTML comments: an identifier pasted into a sample payload or a
 * `<!-- source: ... -->` provenance marker discloses just as much as one
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
 * Deliberately absent: anything belonging to Custos itself. Its source paths,
 * module names, tests and internal mechanisms are what an auditor came to read.
 */
const BANNED = [
  {
    group: 'systems behind ARX',
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
      'ARX is one product to a reader. Name the contract Custos holds with ARX, not the services implementing it.',
  },
  {
    group: 'ARX-side storage and mechanism',
    patterns: [
      /\bsearch_path\b/,
      /\bDSN\b/,
      /\bmigration\s+0\d{3}\b/i,
      /\barx_(live|sim|control)\b/,
      /\bprojector\b/i,
      /\b(PostgreSQL|Postgres|database|read[- ]model)\s+projection\b/i,
      /\bprojects?\s+(the\s+)?facts?\b/i,
      /\bsaga\b/i,
    ],
    guidance:
      'How ARX stores or consumes what Custos emits is closed. State the contract — what is sent, signed and acknowledged.',
  },
  {
    group: 'development process',
    patterns: [
      /\bplan\s+\d{1,3}[a-z]?\s*T\d/i,
      /\bplan\s+\d{1,3}[a-z]?\b/i,
      /\bT\d{1,2}[a-z]?\b(?![-\w])/,
      /\bDEV-\d{2,3}[A-Z-]/,
      /\blesson\s+#\d+/i,
      /\bred[- ]line\s+0\.\d\b/i,
      /\bADR-\d{3}\b/,
      /\bTier\s+[A-D]\b/,
      /\b\d+\s*BC\b/,
      /\bG\d+(-[A-Za-z]+)?\b/,
      /\bG-SoD\b/,
      /\bvision\s*支柱/,
      /\b六件套\b/,
      /\b三件套\b/,
      /\bCEO\b/,
      /\bdirective\s*\(\d{4}-\d{2}-\d{2}\)/,
    ],
    guidance:
      'Plan, gate, lesson and decision references are development process. Describe what the code does or name the guarantee.',
  },
  {
    group: 'assistant and workflow configuration',
    patterns: [
      /CLAUDE\.md/,
      /\.claude\/rules/,
      /\.forge\//,
      /codex\/projects/,
      /ecosystem-authority/,
      /\bthe-alephain-guild\b/,
    ],
    guidance:
      'These configure how we build, not how Custos works. Point at the documentation or the code instead.',
  },
];

/**
 * Lines that legitimately carry an otherwise-banned token because they are part
 * of the public product surface. The published repository and container
 * coordinates are things an operator must type to clone or pull, so they are
 * not disclosure. The exemption is scoped to the `custos` project — it does not
 * whitelist other paths under the same organisation.
 */
const CONTEXT_EXEMPTIONS = [
  /ghcr\.io\/the-alephain-guild\/custos/,
  /github\.com\/the-alephain-guild\/custos/,
  /the-alephain-guild\/custos\b/,
  // Signing-domain constants are literal bytes of a signing preimage. An
  // auditor who retypes them differently cannot verify the signature, so the
  // exact string is product surface even though it embeds a legacy name.
  /^crucible[.-][\w.-]*(pop\.v\d|BATCH-V\d)/i,
  /CRUCIBLE-RUNNER-FACT-BATCH-V\d/,
];

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

/**
 * One English term, one Chinese rendering.
 *
 * The site had four terms with two or three renderings each. `venue` was
 * 场所 / 交易场所 / 交易所, `artifact` was 产物 / 制品, and `vault` was sometimes
 * translated and sometimes not. None of that is wrong sentence by sentence,
 * and all of it costs the reader: two words for one thing reads as two things.
 *
 * The worst was 凭证, which carried both `credential` and `provenance` — so a
 * page could say 机器凭据 and 交易所凭证 four lines apart and mean the same kind
 * of thing, while 来源凭证 five lines later meant something unrelated.
 *
 * Identifiers are exempt. A `vault` inside backticks is the CLI subcommand,
 * the flag or the path; translating it would document a command that does not
 * exist. Only prose is checked.
 */

import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname;
const LOCALE = join(ROOT, 'i18n/zh-Hans/docusaurus-plugin-content-docs/current');

/** Rejected rendering → what to use instead, and why the pair collides. */
const BANNED = [
  {term: '场所', use: '交易所', note: 'venue'},
  {term: '交易场所', use: '交易所', note: 'venue'},
  {term: '制品', use: '产物', note: 'artifact'},
  {term: '凭证', use: '凭据（credential）或 来源记录（provenance）', note: 'ambiguous'},
  {term: '保险库', use: '金库', note: 'vault'},
  {term: '断路器', use: '熔断器', note: 'circuit breaker'},
];

const CODE_SPAN = /`[^`]*`/g;
const LINK_TARGET = /\]\([^)]*\)/g;
const HAN = '\\u4e00-\\u9fff';
// `vault` as a prose word: not part of a path, flag, identifier or filename.
const PROSE_VAULT = new RegExp(`(?<![\\w/-])vault(?![\\w/.-])`);

function markdownFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return markdownFiles(path);
    return path.endsWith('.md') ? [path] : [];
  });
}

/** Strip what is not prose: code fences, code spans and link targets. */
export function proseLines(text) {
  const out = [];
  let fenced = false;
  text.split('\n').forEach((line, index) => {
    if (line.trimStart().startsWith('```')) {
      fenced = !fenced;
      return;
    }
    const stripped = line.replace(CODE_SPAN, '').replace(LINK_TARGET, '');
    // A fenced block still holds Chinese comments, and those are prose. Only
    // the Latin-identifier rules are skipped there, never the Han ones.
    out.push({line: index + 1, text: stripped, fenced});
  });
  return out;
}

export function findViolations(text) {
  const found = [];
  for (const {line, text: content, fenced} of proseLines(text)) {
    for (const {term, use, note} of BANNED) {
      // 交易场所 contains 场所; report the longer one only.
      if (term === '场所' && content.includes('交易场所')) continue;
      if (content.includes(term)) found.push({line, term, use, note});
    }
    if (!fenced && PROSE_VAULT.test(content) && new RegExp(`[${HAN}]`).test(content)) {
      found.push({line, term: 'vault', use: '金库', note: 'vault in Chinese prose'});
    }
  }
  return found;
}

function main() {
  let count = 0;
  let files = 0;

  for (const path of markdownFiles(LOCALE).sort()) {
    const violations = findViolations(readFileSync(path, 'utf8'));
    if (!violations.length) continue;
    files += 1;
    count += violations.length;
    console.error(`\n${relative(ROOT, path)}`);
    for (const {line, term, use} of violations) {
      console.error(`  :${line}  ${term} → ${use}`);
    }
  }

  if (count) {
    console.error(
      `\n❌ ${count} inconsistent term(s) across ${files} file(s).\n` +
        '   One English term gets one Chinese rendering. Identifiers stay English —\n' +
        '   put them in backticks and they are exempt.',
    );
    process.exit(1);
  }

  console.log('✅ terminology check: each term has one Chinese rendering');
}

if (import.meta.url === `file://${process.argv[1]}`) main();

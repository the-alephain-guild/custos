/**
 * A newline inside a Markdown paragraph is a soft break, rendered as a space.
 *
 * That is right for English and wrong for Chinese. A paragraph wrapped between
 * two Han characters renders with a gap inside the word: `只 会由` instead of
 * `只会由`. It is invisible in the source, invisible in review, and obvious to
 * every reader of the published page. The site shipped 470 of them.
 *
 * So Chinese paragraphs are not hard-wrapped: one paragraph, one line. Lines
 * that wrap between a Latin word and Han text are left alone, because there the
 * space is correct and wanted.
 */

import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname;
const LOCALE = join(ROOT, 'i18n/zh-Hans/docusaurus-plugin-content-docs/current');

const HAN = '\\u4e00-\\u9fff\\u3000-\\u303f\\uff00-\\uffef';
// Trailing or leading inline markers still count as Han text either side.
const ENDS_HAN = new RegExp(`[${HAN}][\\]\\)\`*_]*$`);
const STARTS_HAN = new RegExp(`^[\\[\`*_]*[${HAN}]`);
// A block marker means the next line is its own block, not a continuation.
const BLOCK = /^(\s*([-*+]|\d+\.)\s|\s*>|\s*\||\s*#|:::|<!--|\s*```)/;

function markdownFiles(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return markdownFiles(path);
    return path.endsWith('.md') ? [path] : [];
  });
}

export function findSoftBreaks(text) {
  const lines = text.split('\n');
  const found = [];
  let fenced = false;

  for (let i = 0; i < lines.length - 1; i += 1) {
    if (lines[i].trimStart().startsWith('```')) {
      fenced = !fenced;
      continue;
    }
    if (fenced || BLOCK.test(lines[i]) || BLOCK.test(lines[i + 1])) continue;

    const current = lines[i].replace(/<!--.*?-->\s*$/, '').trimEnd();
    const next = lines[i + 1].trim();
    if (!current || !next) continue;

    if (ENDS_HAN.test(current) && STARTS_HAN.test(next)) {
      found.push({line: i + 1, excerpt: `${current.slice(-12)} ⏎ ${next.slice(0, 12)}`});
    }
  }
  return found;
}

function main() {
  let count = 0;
  let files = 0;

  for (const path of markdownFiles(LOCALE).sort()) {
    const breaks = findSoftBreaks(readFileSync(path, 'utf8'));
    if (!breaks.length) continue;
    files += 1;
    count += breaks.length;
    console.error(`\n${relative(ROOT, path)}`);
    for (const {line, excerpt} of breaks) console.error(`  :${line}  ${excerpt}`);
  }

  if (count) {
    console.error(
      `\n❌ ${count} Chinese paragraph line break(s) across ${files} file(s) render as a ` +
        'space inside a word.\n' +
        '   Put each Chinese paragraph on one line. Wrapping between a Latin word and Han ' +
        'text is fine — the space belongs there.',
    );
    process.exit(1);
  }

  console.log('✅ CJK wrapping check: no paragraph line breaks render as an in-word space');
}

if (import.meta.url === `file://${process.argv[1]}`) main();

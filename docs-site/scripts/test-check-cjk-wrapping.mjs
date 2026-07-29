/** The wrapping check is only worth having if it can fail. */

import {findSoftBreaks} from './check-cjk-wrapping.mjs';

const CASES = [
  {
    name: 'Han on both sides of the break is caught',
    body: '兼容路径意味着同一条消息有两个解析器，而它们之间的差异，只\n会由那条消息暴露出来。\n',
    expect: (found) => found.length === 1,
  },
  {
    name: 'a Latin word before the break is left alone',
    body: '每一份 schema 都是 additionalProperties: false\n，这让两类改动都必须协同。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'Han before a Latin word is left alone',
    body: '这些 schema 没有远程引用，因此校验用的是\njsonschema 库。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'one paragraph on one line passes',
    body: '兼容路径意味着同一条消息有两个解析器，而它们之间的差异只会由那条消息暴露出来。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'code fences are not paragraphs',
    body: '```text\n第一行\n第二行\n```\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'consecutive list items are separate blocks',
    body: '- 第一项内容\n- 第二项内容\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'table rows are separate blocks',
    body: '| 字段 | 含义 |\n| 租户 | 所属租户 |\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'an admonition title is not joined to its body',
    body: ':::note 目前还没有已发布的产物\n尚未有任何版本发布。\n:::\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'a trailing HTML comment does not hide the break',
    body: '本章说明每一份的用途， <!-- disclosure-ok: reason -->\n以及这组内容不覆盖什么。\n',
    expect: (found) => found.length === 1,
  },
  {
    name: 'inline markers either side still count as Han text',
    body: '这是一次**静默的丢弃**\n**在这里**后者的价值更高。\n',
    expect: (found) => found.length === 1,
  },
];

let failed = 0;
for (const {name, body, expect} of CASES) {
  const found = findSoftBreaks(body);
  if (expect(found)) {
    console.log(`✅ ${name}`);
  } else {
    failed += 1;
    console.error(`❌ ${name} — got ${found.length}: ${JSON.stringify(found)}`);
  }
}

console.log(`\n${CASES.length - failed}/${CASES.length} CJK wrapping tests passed`);
process.exit(failed ? 1 : 0);

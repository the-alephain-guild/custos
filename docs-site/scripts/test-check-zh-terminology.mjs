/** The terminology check is only worth having if it can fail. */

import {findViolations} from './check-zh-terminology.mjs';

const CASES = [
  {
    name: 'a rejected rendering of venue is caught',
    body: 'runner 从不连接场所。\n',
    expect: (found) => found.length === 1 && found[0].use === '交易所',
  },
  {
    name: 'the long form of venue is caught once, not twice',
    body: '它不接触任何交易场所。\n',
    expect: (found) => found.length === 1 && found[0].term === '交易场所',
  },
  {
    name: 'the accepted rendering passes',
    body: 'runner 从不连接交易所。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'the ambiguous credential rendering is caught',
    body: '交易所凭证解密失败。\n',
    expect: (found) => found.length === 1 && found[0].term === '凭证',
  },
  {
    name: 'artifact has one rendering',
    body: '校验并激活精确制品。\n',
    expect: (found) => found.length === 1 && found[0].use === '产物',
  },
  {
    name: 'vault in Chinese prose is caught',
    body: '凭据留在加密的机器 vault 里。\n',
    expect: (found) => found.length === 1 && found[0].term === 'vault',
  },
  {
    name: 'the vault subcommand in backticks is exempt',
    body: '运行 `arx-runner vault put` 写入一份凭据。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'a vault flag in backticks is exempt',
    body: '用 `--vault-dir` 覆盖金库目录。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'a link target containing vault is exempt',
    body: '见[凭据金库](/zh-Hans/operator-guide/credential-vault)。\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'an English command block is not Chinese prose',
    body: '```bash\narx-runner vault list\n```\n',
    expect: (found) => found.length === 0,
  },
  {
    name: 'a Chinese comment inside a code block is still prose',
    body: '```bash\narx-runner vault list   # 每个场所一个文件\n```\n',
    expect: (found) => found.length === 1 && found[0].use === '交易所',
  },
  {
    name: 'clean prose passes',
    body: '凭据留在加密的机器金库里，产物在导入前校验。\n',
    expect: (found) => found.length === 0,
  },
];

let failed = 0;
for (const {name, body, expect} of CASES) {
  const found = findViolations(body);
  if (expect(found)) {
    console.log(`✅ ${name}`);
  } else {
    failed += 1;
    console.error(`❌ ${name} — got ${JSON.stringify(found)}`);
  }
}

console.log(`\n${CASES.length - failed}/${CASES.length} terminology tests passed`);
process.exit(failed ? 1 : 0);

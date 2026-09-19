---
title: "参与贡献"
sidebar_position: 3
---

仓库流程见[CONTRIBUTING.md](https://github.com/the-alephain-guild/custos/blob/main/CONTRIBUTING.md)。

变更需保留本地凭据处理、执行准入、断线持续安全检查和十进制金额边界。源码标识、注释、运行消息和 commit message 使用英文；公开文档支持中英文。

## 验证

执行与变更相关的检查并说明范围。行为变化需要覆盖受影响的成功与失败路径。`make verify` 是仓库基础检查；引擎、Docker 和外部服务测试另有前提。

修改网站前安装基础 runner 依赖，然后运行：

```bash
cd docs-site
npm ci
npm run verify
```

站点检查公开内容边界、中文换行/术语、从源码生成的参考资料、双语构建和 TypeScript。中英文一起更新，围绕操作任务写作并保留精确命令名称。从仓库根运行 `uv run python scripts/check-docs-site.py --write` 重新生成源码参考表。

## 契约与证据变更

演进中的代码、schema 和内部契约由 Git review 与 CI 管理。历史收据记录自己的 revision，不应仅因源码字节变化而改写。契约有意变更时与消费者协调，需要时记录新的验收证据。

疑似漏洞按[安全政策](/release-governance/security-policy)私下报告。普通改进可走正常 pull request 流程，避免公开敏感利用细节。

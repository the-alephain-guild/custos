---
title: "审计清单"
sidebar_position: 7
---

审查 runner 前记录源码 revision、依赖 profile 和工作树状态。源码检查、镜像检查与已部署验收分别报告。

## 源码检查

```bash
make verify
make toolkit-typecheck
uv run python scripts/check-docs-site.py
```

先安装依赖。基础测试可能跳过 Nautilus 用例；评估引擎行为时应检查 skip 并运行 NT profile。基础检查通过只证明已测试范围，不能证明所有安全声明。

| 范围 | 源码/测试入口 |
|---|---|
| 凭据 | `src/custos/core/per_key_vault.py`、`tests/test_credential_lifecycle.py` |
| 签名准入 | `src/custos/core/engine_lifecycle.py`、`tests/test_engine_lifecycle.py` |
| 离线边界 | `src/custos/offline/mode_guard.py`、`tests/test_offline_mode_guard.py` |
| 交易所声明 | `tests/test_nt_venue_wiring.py`、`tests/test_nt_sodex_venue.py` |
| 熔断与风险控制 | `src/custos/offline/safety.py`、`tests/core/test_fallback_breaker.py` |
| 事实持久化与金额 | `tests/test_runner_fact_store.py`、`tests/test_strategy_signal_fact_contract_v1.py` |

同时阅读失败用例和成功路径。核对密钥不进入日志或对外遥测、模式无法绕过准入、被拒绝操作不会到达交易所。文本搜索可定位实现，不能单独证明不存在泄露或绕过。

## 操作检查

运行[独立 sandbox 练习](/getting-started/standalone-sandbox)，实际检查本地身份、加密、broker、应用和停止行为。它使用模拟宿主，不验证真实交易所或签名端到端部署。

```bash
make verify-local-v030
```

该命令检查构建镜像契约及 revision 标签，不包含完整独立或签名部署验收。应单独测试精确部署产物，并把结果与 image digest、revision 一起保存。

## 需要审查的边界

签名指令要求上游授权；离线指令是显式的未签名 sandbox/testnet 输入，不能授权 live。当前 live 组合仍禁用。交易所密钥实际权限与宿主访问控制需要操作者核实，不能只依赖本地 scope 声明。

签名产物见[发布验证](/trust-model/signed-release-chain)，未关闭验收项见[发布状态](/release-governance/release-status)。

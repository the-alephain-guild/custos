---
title: "Live 执行准入"
sidebar_position: 3
---

Live 执行要求签名部署授权、兼容引擎与 connector、允许的凭据、验证后的产物能力和正确绑定的晋升证据。当前 daemon 组合仍禁用 live。

完整条件表统一维护在[执行准入](/concepts/live-execution-gate)。`SandboxSimulationHost` 只声明 sandbox，离线通道也独立拒绝 live。建立 live 传输会话不会改变这些执行检查。

## 验证

```bash
uv run pytest tests/test_engine_lifecycle.py \
  tests/test_nautilus_host_capability.py \
  tests/test_nt_venue_wiring.py tests/test_offline_mode_guard.py
```

测试覆盖引擎操作前的拒绝、按模式声明的 connector 支持和离线模式边界。这些是源码级证据，不能认证已部署镜像或交易所账户。当前验收边界见[发布状态](/release-governance/release-status)。

准入后仍持续执行本地敞口和回撤检查。通过准入的部署也可能被安全保护停止。

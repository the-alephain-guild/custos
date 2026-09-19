---
title: "升级路径"
sidebar_position: 2
---

升级前记录当前 Git revision 或 image digest、依赖 lock 和状态路径，保留身份、加密凭据及一致数据库备份。先看[发布状态](/release-governance/release-status)，区分源码版本、已发布候选产物和受支持稳定版本。

## Nautilus 2 checkout

当前适配器在 Python 3.12 上使用 NautilusTrader 2 API。通过 `make install-nt` 安装兼容依赖，不要单独升级引擎包。

1. 通过正常目标状态路径停止受影响部署，检查交易所持仓和订单。
2. 在目标 checkout 执行 `make install-nt`。
3. 运行相关源码、类型与引擎检查。
4. 在 sandbox 演练启动、就绪和停止，再运行 testnet。
5. 重建本地镜像并核对 revision 标签。

宿主在同一 event loop 上只接受一个活动 node。投资组合初始化后还需可靠估值，引擎就绪才会通过。SoDEX 的模式和输入限制见[此处](/engines/sodex)。

## 签名与离线输入

签名部署仍由 ARX 提供。本地 `deployment validate/publish` 处理独立的 `OfflineDeploymentSpec`，仅支持 sandbox/testnet，不能校验或发布正式签名部署指令。

`identity standalone` 用于离线操作。不要编辑 `runner.toml` 来转换已注册身份，应使用独立状态根。离线代码目录 hash 与签名不可变发布摘要属于不同契约，不能互换。

## 旧状态布局

旧版单文件交易所金库需要通过 `vault put` 按 key 主动重新配置。解密迁移材料应保存在受保护的本地位置，通过 stdin 提供秘密，验证后移除临时明文。迁移交易所密钥时不要覆盖机器金库。

状态使用 `~/.arx`。保留每个 runner 的元数据/金库绑定；旧身份格式不再受支持时应重新注册。只复制旧注册记录不会生成有效的当前 `runner.toml` 和加密机器身份。

## 回退

恢复经过测试的源码/镜像 revision 及其匹配依赖。复用新版写入的状态前，应检查状态格式与契约兼容性。旧二进制不一定能理解新数据库或签名载荷。保留备份及记录的证据，直到恢复验证完成。

## 生产与 1.0

生产需要独立的已部署验收，本地镜像检查不够。未来 1.0 还需满足文档中的兼容与支持承诺，包括实际履行支持窗口。Sandbox/testnet 本地成功不能授权生产晋升。

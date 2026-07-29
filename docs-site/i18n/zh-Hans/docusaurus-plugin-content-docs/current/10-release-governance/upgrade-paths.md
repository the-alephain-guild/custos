---
title: "升级路径"
sidebar_position: 2
---

# 升级路径

版本线之间变了什么，以及你需要为此做什么。章节按倒序排列，页面顶部始终是最近的一次变更。

:::note 目前还没有可供升级的已发布产物
尚未有任何版本以 wheel 或镜像形式发布。现存的每个 runner 都是从源码安装或本地构建的，因此没有 `pip install --upgrade` 可跑，也没有镜像 tag 可拉取 —— 见[安装](/zh-Hans/getting-started/installation)。

但这并不意味着本页是假设性的。下面列出的变更是真实的：把一个 runner 从一个源码版本挪到下一个，你仍然要做这些事。延后的是打包发布，不是这些工作。
:::

## 0.2.x → 0.3.0

0.3.0 把 NautilusTrader 设为默认引擎，让每条期望状态消息在触及 vault、执行门或宿主代码之前先过严格契约校验，并把完整运行时交付为一个你自己构建并验证的镜像。

1. **构建并验证镜像。**在你的 checkout 中，`make verify-local-v030` 会构建
   `custos-runner:v0.3.0` 并对它跑完整的运行时契约。删掉你自己那份仅仅为了加
   NautilusTrader、PyYAML、`sops` 或 `age` 而存在的 Dockerfile —— 镜像现在自带它们。
2. **替换掉已移除的布尔引擎开关。**引擎选择是一个封闭枚举：`--engine nautilus`
   （默认）或 `--engine sandbox-sim`。模拟宿主只声明 `sandbox`，因此它会拒绝 testnet
   或 live 部署，而不是在没有交易所连接的情况下悄悄把它跑起来。
3. **更新每一份 spec。** `generation` 必须 `>= 1`，`lifecycle_state` 现在与
   `trading_mode` 分离，`strategy_config` 原样透传。未声明的字段会被拒绝，而不是被忽略。
4. **在每个 runner 上装好域事件公钥**，并在上游把流拓扑准备好。Custos 验证已签名的命令；它不创建流，也不会接受一条未签名的命令。相关参数见
   [CLI 参考](/zh-Hans/reference/cli)。
5. **以就绪状态而非进程启动作为放量的判据** —— 在 runner 真正可服务之前，
   `arx-runner health` 会以非零退出。见[就绪与健康探针](/zh-Hans/operator-guide/readiness-health)。

没有离线的 spec 校验命令。spec 是在到达之后、签名验证通过之后才被校验的：在本地校验未签名的材料，只会告诉你这条消息格式正确，却不会告诉你它是否可信 —— 而后一个问题才是重要的那个。

策略仓库直接消费这个已验证的镜像。基于它再派生一个自己的镜像，会让你退回到该镜像本就是为了消除的那种处境：一个没有人验证过的产物。

## 0.1.x → 0.2.0

0.2.0 是第一次彻底断开兼容。有两样东西发生位移，且都不是自动的。

**状态迁到 `~/.arx`。**

```bash
mkdir -p ~/.arx
mv ~/.custos/enrollment.json ~/.arx/enrollment.json  # 若存在
mv ~/.custos/state           ~/.arx/state            # 若存在
```

**每个交易所 key 逐个重新配置。**单个 sops JSON 文件被"一 key 一文件"的加密文件取代。这里刻意不做自动迁移：旧文件是一个装着你全部密钥的整块 blob，而迁移意味着要把它们一次性全部解密再重写。

```bash
sops --decrypt ~/.old-vault/vault.json > /tmp/legacy.json
# 然后对该文件中的每个 key：
arx-runner vault put --key-id <id> --tenant-id <tenant> \
  --api-key <api-key> --api-secret-stdin --scope-digest <lowercase-sha256>
shred -u /tmp/legacy.json
```

随后从任何 systemd unit、launchd plist 或 Compose service 中删掉已退役的
`--sops-file` 与 `--age-key-file` 参数，并在靠近真实资金之前先证明 runner 仍然可用：

```bash
arx-runner start --enabled-mode sandbox --engine sandbox-sim
```

容器操作者必须挂载 `~/.arx`。Dockerfile 声明了 `VOLUME ["/home/custos/.arx"]`；临时挂载会丢失机器身份与交易所凭据，而缺了它们 runner 会拒绝启动。见[容器示例](/zh-Hans/operator-guide/deployment)。

## 把 0.x 提升到 1.0

1.0 是一个关于兼容性的承诺，所以它的门禁是"有证据表明这个承诺守得住"，而不是某个日期：

- [ ] 命令与事实两条链路达到生产就绪：已签名的命令抵达精确的 runner subject，已签名的事实被可靠地摄入。
- [ ] 连续三个 minor 版本，对已发布 schema 与控制台入口没有破坏性变更。
- [ ] 已发布的 schema 覆盖命令解码缝与已签名事实的输出契约。
- [ ] [EOL 表](/zh-Hans/release-governance/semver-lts)中至少有一行已经处于自己的支持窗口之内 —— 也就是说，在把这个承诺永久化之前，它已经在实践中被兑现过一次。

最后一条才是这套门禁的意义所在。若某条版本线尚未走完过自己的支持窗口就宣布 1.0，那是一个背后没有证据的承诺。

各项勾选完毕后，提升本身只是机械动作：升版本号、加 changelog 章节、打 tag、在 EOL 表里加上新的一行。

## minor 版本的模板

```
## `0.<prev>.x` → `0.<next>.0`

### 变更内容

- {功能 | 修复 | 是否破坏性变更}

### 迁移步骤

- {操作者需要执行的命令}

### 回滚

- 重新安装上一条 minor 线。同一条 minor 线内配置保持向后兼容，因此回滚只是换个版本，
  不涉及其他改动。
```

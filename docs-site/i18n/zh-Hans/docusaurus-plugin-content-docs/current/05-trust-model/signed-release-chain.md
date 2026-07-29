---
title: "签名发布链"
sidebar_position: 6
---

# 签名发布链

wheel 构建是逐字节可复现的，因此审计者可以从源码重新构建，再把哈希与已发布的产物做对比。

这正是"源码开放"之所以有价值的原因。读代码告诉你的是：**如果**你正在运行的二进制产物由这份源码构建而来，那么 runner 会做什么；而可复现性才是你用来确认"它确实由这份源码构建"的手段。没有可复现性，审计覆盖的是一棵源码树，而操作者运行的是另一个东西。

**远程发布在 0.3.0 上被延后。**目前还没有已发布的产物可供比对；当前的消费者门禁是一个由你自己构建并验证的镜像。

## 三个旋钮

1. **`SOURCE_DATE_EPOCH`** —— 一个 Unix 时间戳（秒）。hatchling 用它代替主机时钟，来写入 wheel 的 ZIP 元数据中的文件 mtime。不钉住它，每次重新构建都会嵌入"此刻"，于是即便源码完全相同，产出的 wheel 字节哈希也不同。
2. **`uv.lock`** —— 提交进仓库的锁文件把每一个传递依赖冻结到确定的版本 + 摘要。
   `uv build` 通过 `[tool.uv].package = true` 读取它，因此锁文件过期会被立刻发现。
3. **`hatch_build.py`** —— 一个自定义的 `BuildHookInterface` 子类，经
   `[tool.hatch.build.hooks.custom]` 接入。hatchling ≥ 1.20 已原生支持
   `SOURCE_DATE_EPOCH`，所以这个 hook 的方法体是空操作 —— 它的职责是把"epoch 是否已设置"**打印出来**（让在本地跑 `uv build` 的操作者一眼看到旋钮是否生效），并在
   hatchling 未来在原生确定性上退化时，提供一个现成的落点去长出真正的实现。

## 手工复现（审计者流程）

```bash
# 1. 在你想验证的发布 tag 上克隆仓库
git clone https://github.com/the-alephain-guild/custos.git
cd custos
git checkout <release-tag>

# 2. 把 epoch 钉到该 tag 的提交时间戳 —— 与发布流水线用的是同一条命令
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct <release-tag>)"

# 3. 构建；产出的 wheel 必须与已发布的 wheel 哈希一致
uv build --out-dir /tmp/verify
sha256sum /tmp/verify/*.whl
```

当远程发布存在时，与它的 SHA256SUMS 附件比对。不一致意味着两种情况之一：epoch 取错了（查发布说明里流水线实际使用的值），或者源码被篡改过 —— 后一种情况下，sigstore 证明也会在 cert-identity 上验证失败。

## 自动化验证

`tests/test_reproducible_build.py` 会在钉住 epoch 的前提下跑两轮 `uv build`，断言两个
wheel 的哈希完全一致。它被标记为 slow —— 双次构建要花几十秒 —— 但并没有被排除在默认选择之外：`make verify` 会跑它，因此这项性质在每一次发布门禁上都被检查，而不是等谁想起来才去验证。

单独跑它：

```bash
uv run pytest tests/test_reproducible_build.py
```

还有一个配套测试 `test_wheel_bytes_differ_without_epoch`，断言的是**相反**的事情：去掉
epoch 会让字节发生变化。它被标为 `xfail(strict=True)`，因为事实并非如此 ——
hatchling ≥ 1.20 原生就是确定性的，不设 epoch 重新构建同样产出完全相同的 wheel。

这个反转是刻意的，也值得顺着想一遍。如果未来某个 hatchling 版本重新引入了主机时钟泄漏，这个测试就会开始通过 —— 而一个 strict 的 xfail 一旦通过，会被报告为失败。于是
"可复现性重新变成依赖 epoch 钉死"的那一天，就是测试套件变红的那一天，而不是无人察觉的一天。epoch 钉死是纵深防御，而这就是我们发现它已经变成承重件的方式。

## Docker 镜像的可复现性

Docker 镜像的可复现性是另一条独立的工作线（buildkit 的时间戳归一化在不同 buildkit 版本之间并不稳定）。在当前 0.3.0 的本地开发中，"审计二进制产物"的镜像一侧由以下几点承担：

- `make verify-local-v030` 构建 `custos-runner:v0.3.0`，注入
  `org.opencontainers.image.revision = <commit sha>`，并跑完 Docker 与独立 NATS 两组门禁。
- 打印出的镜像 ID 与 revision 标签，为下游开发提供本地的来源证据。
- 远程发布还会重新拉取已发布的镜像，并针对已发布的 digest 验证命令矩阵、Nautilus 与
  PyYAML 的导入、`sops` 与 `age` 可执行文件、就绪探针、非 root 身份，以及 cosign 签名。

逐比特的镜像可复现性 —— 把构建钉到特定的 buildkit 与 `SOURCE_DATE_EPOCH` 组合 ——
尚未完成。与其暗示镜像具备 wheel 那样的性质，不如明说：今天镜像的来源证据是它的
revision 标签和它通过的那道门禁，这比逐比特复现要弱。

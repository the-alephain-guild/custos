---
title: "签名发布验证"
sidebar_position: 6
---

验证你实际准备运行的精确产物。源码审查、wheel 可复现构建、镜像签名和已部署验收分别证明不同属性。

按正式发布说明选择产物，并检查[支持范围](/release-governance/release-status)。不要将本地构建镜像视为已获准用于生产。

## Wheel 复现

使用发布时的精确源码 revision、构建工具和 epoch。runner 发布流程从源码 commit 时间戳取得 `SOURCE_DATE_EPOCH`；以待验证产物记录的 epoch 为准。

```bash
git checkout "$RELEASE_REF"
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
uv build --out-dir /tmp/custos-wheel-verify
sha256sum /tmp/custos-wheel-verify/*.whl
```

使用匹配的构建输入，与已发布 wheel 校验和比较。不一致时先核对 revision、epoch、工具版本和构建环境，再判断是否存在篡改。源码 lock 文件本身不能证明构建后端环境完全相同。

`hatch_build.py` 记录 epoch 使用情况，底层时间戳行为由 hatchling 提供。可复现测试构建两次并比较字节：

```bash
uv run pytest tests/test_reproducible_build.py
```

测试还记录不显式设置 epoch 时的预期行为。应结合被测试工具链解释 xfail/skip，不能假定所有构建环境具有相同确定性。

## 镜像验证

稳定版本 tag 指向的，正是通过完整 runtime gate 的那个镜像 digest。发布工作流只构建一次候选镜像，针对该 digest 运行 gate，然后对同一个 digest 打稳定 tag 并签名；在 gate 与稳定 tag 之间不会重新构建。

```bash
make verify-local-v030
```

本地目标检查镜像运行契约和 revision 标签，不运行完整签名部署或独立 broker 验收，也不能证明 Docker 镜像逐字节可复现。

使用发布镜像时，对照精确镜像验证摘要、签名身份、源码 revision 和记录的运行检查。候选验证不能关闭已部署生产验收。产物证据应保留在其记录的 revision 上。

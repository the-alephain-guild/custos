---
title: "许可证与 NOTICE"
sidebar_position: 4
---

# 许可证与 NOTICE

Custos 采用 **Apache-2.0** 许可，自首个公开版本起即如此。

- **[LICENSE](https://github.com/the-alephain-guild/custos/blob/main/LICENSE)** —— 完整文本
- **[NOTICE](https://github.com/the-alephain-guild/custos/blob/main/NOTICE)** —— 第 4(d) 条要求的署名

## 为什么偏偏是这个许可证

这个 runner 持有你的交易所凭据。让「信任它」成为理性选择的唯一条件，是你能读到它对这些凭据做了什么 —— 所以一个允许阅读、审计、fork 与运行的许可证，在这里不是分发偏好，**它是使核心主张可被核实的前提**。

Apache-2.0 还带有明确的专利授予，而不含专利条款的宽松许可证没有。对于一个执行金融交易的软件，这个区别值得白纸黑字写下来。

## 这对你意味着什么

你可以使用、修改并再分发 Custos，**包括商业用途**，只要保留许可证与 NOTICE 并声明重大改动。对你自己的策略、以及你围绕 runner 构建的任何东西，都不存在 copyleft 义务。

## 贡献

**没有 CLA。** Apache-2.0 自带的贡献授予已经足够，因此参与贡献不需要另外签署任何东西 ——
见[参与贡献](/zh-Hans/release-governance/contributing)。

## 第三方组件

vendored 的第三方代码保留各自的许可证，并列在 NOTICE 文件中。用精确 digest 守护它、而不是重写它，是刻意的：一个被你改过的 vendored 依赖，就是一个你再也无法与其上游作比对的依赖。

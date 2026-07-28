---
title: "SEMVER & LTS Commitment"
sidebar_position: 1
---


# SEMVER & LTS Commitment

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

This document is the authoritative statement of the Long-Term Support (LTS)
window, security patch SLA, release cadence, and key-rotation protocol for
`custos-runner`. It is deliberately hand-maintained (rather than generated
from an LTS-status page) at the 0.x stage — an automated status page is a
follow-up plan tracked in [upgrade paths](/release-governance/upgrade-paths).

The concrete numbers below are contractual — a change to any row must go
through a MINOR bump (loosening) or a MAJOR bump (tightening) plus a
matching `CHANGELOG.md` entry. See [`../CHANGELOG.md`](https://github.com/the-alephain-guild/custos/blob/main/CHANGELOG.md) and
the SEMVER contract table below for the full envelope.

## SEMVER 契约

哪种改动算哪一级是固定的 —— 这样版本号本身就能告诉你升级前是否需要做什么。

| 级别 | 覆盖 | 明确**不**覆盖 |
|---|---|---|
| **MAJOR** | 把 gateway 契约切到新版本目录；重命名或移除 console-script 入口点；收紧 `requires-python`；重命名或移除 `ExecutionEngineProtocol` Tier-1 的字段或方法；`~/.arx/` 状态布局的破坏性变更；**新增 required gateway-contract 字段** | 任何让现有调用方继续工作的改动 |
| **MINOR** | 仅新增：新入口点、新的**可选** gateway-contract 字段、新的 optional-dependency extra、新子命令、不替换旧 job 的新 CI job；依赖 **major** 升级 | 把现有字段改为 required、删除字段、重命名入口点、收紧 `requires-python` |
| **PATCH** | 修复、安全补丁、文档订正、无外部可观测变化的内部重构；依赖 **patch/minor** 升级（`uv.lock` 同 commit 更新） | 任何字段 / 入口点 / schema / 已文档化语义的变化；依赖 major 升级 |

其中两行值得说明，因为集成方常弄错。

**新增可选字段是 MINOR，新增必填字段是 MAJOR。** schema 是严格的
（`additionalProperties: false`），所以未更新的消费方会拒绝一个新字段 —— 即便是可选新增，
也需要两侧都部署。而**必填**新增更糟：不发送该字段的旧生产方会直接校验失败，那是破坏，
不只是协调问题。

**依赖 major 升级算 MINOR，不算 PATCH。** 即便我方表面一处未动，它也可能把传递性破坏
带进你的环境，因此不属于「可以闭眼升」的那一级。

## EOL Window

Each minor release line (`0.Y.x`) is supported for **at least 12 months**
from the first `0.Y.0` tag. During that window the line receives security
patches (see next section) and — best-effort — bug-fix patches. EOL is
announced at least 30 days in advance in the GitHub release notes and
copied to the `CHANGELOG.md` `### Deprecated` section (audit-non-silence).

| Minor line | First release | EOL |
| ---------- | ------------- | --- |
| 0.3.x      | 2026-07-12    | 2027-07-12 (best-effort ≥ 12 months) |
| 0.2.x      | 2026-07-11    | 2027-07-11 (best-effort ≥ 12 months) |

Additional lines will be appended as they cut. Each row is a hard commitment
— a line is not dropped before its published end-of-life date.

## Security Patch SLA

Security fixes ship as a patch release (`0.Y.z+1`) within **30 days** of
public CVE disclosure (best-effort; a note in this doc's Deviations log
covers any miss).

- Report via [GitHub Security Advisories](https://github.com/the-alephain-guild/custos/security/advisories)
  — see [`SECURITY.md`](https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md) for the disclosure protocol.
- Public advisories go live within 24 hours of the patch release.
- Backport policy: security fixes land on every active LTS line. Critical functional-bug
  backports are assessed case by case and announced with the release.

## Release Cadence

Best-effort **quarterly** minor releases. The cadence is not a hard
contract — a missed quarter is annotated in the Deviations log below,
and the LTS window is measured from actual release dates, not from the
target cadence.

## Deprecation Grace

Any field, entry point, or observable behaviour marked `deprecated` in
one minor release stays available for at least the following minor
release (≥ 3 months in practice) before it can be removed. Every minor
release notes emit a reminder for still-deprecated items so nothing
falls off quietly (audit-non-silence).

## Key Rotation Protocol

Sigstore + cosign are keyless (OIDC-backed), so there is no "custos
signing key" to rotate. The rotation surface is the CI workflow's
`cert-identity` template — if the workflow file moves or the tag
naming scheme changes, existing bundles will no longer verify. Handle
that by:

1. Announce the identity change in the next release notes and in the
   `## [Unreleased]` section of `CHANGELOG.md`.
2. Ship a follow-up patch release whose bundles use the new identity.
3. Add a Deviations-log row here linking to the affected tag.

An identity break that only affects re-verification of prior tags
does *not* affect the artifact contents — auditors can still verify
via the tag-time cert-identity that was in effect when the tag was
cut. Verification instructions live in
[`../.github/workflows/scripts/verify-release.sh`](https://github.com/the-alephain-guild/custos/blob/main/.github/workflows/scripts/verify-release.sh).

## Upgrade Path

Concrete upgrade steps for each minor bump live in
[upgrade paths](/release-governance/upgrade-paths), including the 0.x → 1.0 promote
checklist (arx-side gateway wire ready + 3 consecutive minor releases
without breaking changes + gateway-contract v1 covered 100%).

## Follow-up

- Automated LTS status page (0.x → 1.0 timeline) — separate follow-up
  plan; not scoped into 0.3.0.
- Machine-readable EOL feed (`docs/lts-commitment.json`) — same follow-up.

## Deviations log

| Date | Line | Deviation | Notes |
| ---- | ---- | --------- | ----- |
| — | — | — | first entry appears here when a deviation ships |

---
title: "Installation"
sidebar_position: 1
---

Install from a checkout for development, or build a local container. The CLI is `arx-runner`.

## Requirements

| Component | Requirement |
|---|---|
| Base runner | Python 3.11 or later |
| Nautilus distribution | Python 3.12 (`>=3.12,<3.13`) |
| Dependency manager | `uv`, using the checked-in lock file |
| Credential encryption | `sops` and `age` |
| Container workflow | Docker with Compose v2 |

The locked Nautilus fork provides CPython 3.12 wheels for macOS arm64, Linux arm64 and Linux x86_64. Its Linux wheel tags are `manylinux_2_39`; the host must meet that compatibility requirement. Other platform/interpreter combinations are not configured in the current lock sources.

## Source installation

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos
make install
make install-nt
uv run arx-runner --help
```

`make install-nt` adds the Nautilus runtime and requires Python 3.12. An audit-only base installation can omit it. Commands in these guides use `uv run arx-runner` from the repository; an activated virtual environment also exposes `arx-runner` directly.

```bash
make verify
make toolkit-typecheck
```

`make verify` checks formatting, lint, the baseline suite and repository authority. Base tests may skip Nautilus-dependent cases. Use the Nautilus verification target when evaluating engine behavior; a documentation build does not test execution.

## Local container

```bash
make verify-local-v030
```

This builds `custos-runner:v0.3.0`, labels it with the source revision, checks the image runtime contract and prints the image id/revision. It does not exercise a full signed deployment round trip or prove production readiness. A modified derivative image needs its own verification.

Candidate images and toolkit release candidates have separate publication records. See [release status](/release-governance/release-status); do not infer a stable release or current-source acceptance from an older candidate image.

## Next step

- Without ARX: [standalone sandbox](/getting-started/standalone-sandbox).
- With ARX: [enrollment](/getting-started/enrollment), then [signed sandbox](/getting-started/first-sandbox-run).
- All commands: [CLI reference](/reference/cli).

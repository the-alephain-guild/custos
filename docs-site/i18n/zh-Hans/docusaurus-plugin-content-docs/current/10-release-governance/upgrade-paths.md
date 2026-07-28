---
title: "Upgrade Paths"
sidebar_position: 2
---


# Upgrade Paths

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

The authoritative upgrade guide. Every minor / major release adds a section
here in reverse-chronological order; the top of the file always describes
the current recommended path.

## 0.2.x → 0.3.0 (complete runtime clean break)

0.3.0 replaces the boolean engine switch with `--engine nautilus|noop`, makes
Nautilus the default, validates every desired-state message through the strict
`DeploymentSpec`, and provides the complete runtime as a verified local image.

1. From the Custos checkout, run `make verify-local-v030` to build and gate
   `custos-runner:v0.3.0`; remove any derived Custos Dockerfile used only to
   add NautilusTrader, PyYAML, sops, or age.
2. Replace the removed boolean engine flag with `--engine nautilus`. Use
   `--engine sandbox-sim` only for non-live contract tests.
3. Add `generation >= 1`, `lifecycle_state`, and `strategy_config` to every
   spec; validate with `arx-runner deployment validate --spec-file <path>`.
4. Provision ARX-owned JetStream topology and install the exact
   ARX domain-event public key on each runner. Custos does not create
   business streams.
5. Submit desired state through ARX. Custos only validates local files
   offline and consumes signed commands; gate readiness with `arx-runner health`.

Downstream strategy repositories remain blocked until this upgrade is complete. They consume the
verified local image directly, maintains no derived Custos Dockerfile, and
owns only strategy code plus `strategy_config` assembly. Remote release is
deferred and is not a prerequisite for local PS integration.

## 0.1.x → 0.2.0 (breaking release)

0.2.0 is the first clean-break release since the 0.1.x extraction. The
`CHANGELOG.md` 0.2.0 entry is the canonical summary; the exact operator
steps are:

```bash
# 1. install the new wheel
pip install --upgrade custos-runner              # or: uv sync --extra dev

# 2. move state
mkdir -p ~/.arx
mv ~/.custos/enrollment.json ~/.arx/enrollment.json  # if present
mv ~/.custos/state           ~/.arx/state            # if present

# 3. re-provision every key one at a time (per-key .enc replaces the
#    single sops JSON; there is deliberately no auto-migration)
sops --decrypt ~/.old-vault/vault.json > /tmp/legacy.json
# read /tmp/legacy.json, then for each { key_id, key_material } pair:
arx-runner vault put --key-id <id> --tenant-id <tenant> \
  --api-key <api-key> --api-secret-stdin --scope-digest <lowercase-sha256>
# ... and after the last one:
shred -u /tmp/legacy.json

# 4. drop the old --sops-file / --age-key-file CLI flags
#    from any systemd unit / launchd plist / docker-compose service.

# 5. verify a sandbox reconcile before enabling live
arx-runner start --enabled-mode sandbox --engine sandbox-sim
```

Docker operators must additionally mount the new `~/.arx` volume so
per-key `.enc` files persist across container restarts. The Dockerfile
declares `VOLUME ["/home/custos/.arx"]`; the operator side is:

```bash
docker run --rm \
  -v ~/.arx:/home/custos/.arx \
  ghcr.io/the-alephain-guild/custos:v0.2.0
```

See [`ops/05-deployment.md`](/operator-guide/deployment) §Docker Runtime Volume
Mount for the full pattern including `--user "$(id -u):$(id -g)"` when
mounting a host directory whose ownership doesn't match container UID/GID.

## 0.x → 1.0 Promote Checklist

The 0.x → 1.0 promote is contractually gated on ALL of:

- [ ] ARX command and fact wires are production ready: signed deployment
      commands reach exact runner subjects and signed RunnerFacts are durably
      ingested without an ARX business-fact relay.
- [ ] Three consecutive minor releases (`0.Y.0`, `0.Y+1.0`, `0.Y+2.0`)
      with zero breaking changes to `gateway-contract/v1/*.schema.json`
      or `[project.scripts]`.
- [ ] `gateway-contract/v1/` covers the local `DeploymentMessage` decode seam
      and the signed RunnerFact output contract.
- [ ] the [SemVer and LTS](/release-governance/semver-lts) EOL table has at least one row already inside its
      EOL window (i.e. we've kept the LTS promise on a prior line).
- [ ] Council-level RFC: 1.0 promote is a MAJOR SEMVER bump and
      requires a Council debate + ADR entry per the workspace
      `deviation-protocol.md`.

Once all boxes are checked, the promote itself is:

1. Bump `pyproject.toml [project].version = "1.0.0"`.
2. Add a `## [1.0.0] - YYYY-MM-DD` section to `CHANGELOG.md`.
3. Tag `v1.0.0`; the standard release workflow handles the rest.
4. Add a `1.0.x` row to the [SemVer and LTS](/release-governance/semver-lts) EOL table.

## Minor-line upgrade template

Copy this into a new section for each minor bump:

```
## `0.<prev>.x` → `0.<next>.0`

### What changed

- {feature | fix | breaking? summary}

### Migration steps

- {commands the operator must run}

### Rollback

- `pip install "custos-runner==0.<prev>.*"` — configuration remains
  backward-compatible within a minor line, so rollback is a wheel bump.
```

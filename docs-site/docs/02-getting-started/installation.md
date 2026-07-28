---
title: "Installation"
sidebar_position: 1
---

# Installation

There are two supported ways to get a runner today: install from source, or
build the container image locally. Both produce the same `arx-runner` command.

:::note No published artifact yet for 0.3.0
The remote release for 0.3.0 is deferred. There is no wheel to `pip install`
and no image to `docker pull` — the consumer gate for this version is an image
you build and verify yourself. The instructions below reflect that rather than
describing a package that does not exist.
:::

## Prerequisites

| Requirement | Needed for |
|---|---|
| Python >= 3.11 | the runner itself |
| Python >= 3.12 | additionally, if you use the NautilusTrader engine |
| [`uv`](https://docs.astral.sh/uv/) | the only supported Python package manager here |
| [`sops`](https://github.com/getsops/sops) and [`age`](https://github.com/FiloSottile/age) | encrypting and decrypting the credential vault |
| Docker with Compose v2 | only for the container path |

`uv` is not optional. The lock file is committed so that a build is
reproducible, which is part of what makes the runner auditable — `pip` or
`poetry` would resolve a different dependency graph.

## From source

```bash
git clone https://github.com/the-alephain-guild/custos.git
cd custos

make install        # base + development extras
make install-nt     # additionally the NautilusTrader engine (needs Python 3.12+)
```

Check that the command is present and the tree is green:

```bash
uv run arx-runner --help
make verify
```

`make verify` runs formatting, lint and the baseline test suite. Running it once
before you configure anything separates "my environment is wrong" from "my
configuration is wrong" later.

## As a container

```bash
make verify-local-v030
```

This builds `custos-runner:v0.3.0`, stamps it with the current Git revision, and
runs the full runtime contract and standalone acceptance against the built
image. It prints the image ID and revision on success.

Do not build a derived Dockerfile that adds NautilusTrader, sops or age on top
of this image. The verified image is the artifact the gate covers; a derivative
of it is not.

## The command surface

```bash
arx-runner enroll              # obtain a provable machine identity
arx-runner vault put|verify|list   # manage venue credentials
arx-runner credential          # verify, rotate or revoke the machine credential
arx-runner publish-capability  # publish the next capability revision
arx-runner nats-transport      # issue, rotate, revoke or verify transport authority
arx-runner start               # run the daemon
arx-runner health              # check readiness
```

Every subcommand takes `--help`. Run it rather than guessing — the flags are
long and mostly mandatory by design.

## What you still need

Installing the runner does not give you anything to run. Before a first
deployment you need three things, none of which the runner can issue for itself:

1. a **one-time enrollment token** from ARX;
2. an **age identity** on this host, which you generate locally;
3. **venue API credentials** scoped `trade_no_withdraw`.

That asymmetry is the point of the design — see
[the trust model](/introduction/trust-model). A runner that could mint its own
authority would not be safe to hand credentials to.

## Next

[Enrollment](/getting-started/enrollment) — give the runner an identity it can
prove.

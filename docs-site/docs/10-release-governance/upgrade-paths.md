---
title: "Upgrade Paths"
sidebar_position: 2
---

# Upgrade Paths

What changes between version lines, and what you have to do about it. Sections
are in reverse-chronological order, so the top of the page is always the most
recent move.

:::note No published artifact to upgrade from yet
No version has been published as a wheel or an image. Every runner in existence
was installed from source or built locally, so there is no `pip install
--upgrade` to run and no image tag to pull — see
[installation](/getting-started/installation).

That does not make this page hypothetical. The changes below are real, and if
you move a runner from one source revision to the next you still have to make
them. What is deferred is the packaging, not the work.
:::

## 0.2.x → 0.3.0

0.3.0 makes NautilusTrader the default engine, validates every desired-state
message through a strict contract before any vault, gate or host code runs, and
delivers the complete runtime as an image you build and verify locally.

1. **Build and gate the image.** From your checkout, `make verify-local-v030`
   builds `custos-runner:v0.3.0` and runs the full runtime contract against it.
   Delete any Dockerfile of your own that existed only to add NautilusTrader,
   PyYAML, `sops` or `age` — the image now carries them.
2. **Replace the removed boolean engine switch.** Engine selection is a closed
   enum: `--engine nautilus` (the default) or `--engine sandbox-sim`. The
   simulation host declares `sandbox` only, so it will refuse a testnet or live
   deployment rather than quietly running one without a venue.
3. **Update every spec.** `generation` must be `>= 1`, `lifecycle_state` is now
   separate from `trading_mode`, and `strategy_config` is passed through
   untouched. Unknown fields are rejected rather than ignored.
4. **Install the domain-event public key on each runner** and provision the
   stream topology upstream. Custos verifies signed commands; it does not create
   streams, and it will not accept an unsigned one. The flags are in
   [the CLI reference](/reference/cli).
5. **Gate rollout on readiness**, not on the process starting —
   `arx-runner health` exits non-zero until the runner is genuinely in service.
   See [readiness and health](/operator-guide/readiness-health).

There is no offline spec-validation command. A spec is validated when it
arrives, after its signature is verified, because validating unsigned material
locally would tell you a message is well-formed without telling you whether it
is authentic — and the second question is the one that matters.

Strategy repositories consume the verified image directly. Deriving your own
image from it puts you back in the position the image exists to remove: an
artifact nobody verified.

## 0.1.x → 0.2.0

0.2.0 was the first clean break. Two things move, and neither is automatic.

**State moves to `~/.arx`.**

```bash
mkdir -p ~/.arx
mv ~/.custos/enrollment.json ~/.arx/enrollment.json  # if present
mv ~/.custos/state           ~/.arx/state            # if present
```

**Each venue key is re-provisioned individually.** The single sops JSON file is
replaced by one encrypted file per key. There is deliberately no automatic
migration: the old file is a single blob holding every secret you have, and a
migration would have to decrypt all of them at once to rewrite them.

```bash
sops --decrypt ~/.old-vault/vault.json > /tmp/legacy.json
# then, for each key in that file:
arx-runner vault put --key-id <id> --tenant-id <tenant> \
  --api-key <api-key> --api-secret-stdin --scope-digest <lowercase-sha256>
shred -u /tmp/legacy.json
```

Then drop the retired `--sops-file` and `--age-key-file` flags from any systemd
unit, launchd plist or Compose service, and prove the runner still works before
going anywhere near real money:

```bash
arx-runner start --enabled-mode sandbox --engine sandbox-sim
```

Container operators must mount `~/.arx`. The Dockerfile declares
`VOLUME ["/home/custos/.arx"]`; an ephemeral mount loses machine authority and
venue credentials, and the runner will refuse to start without them. See the
[container example](/operator-guide/deployment).

## Promoting 0.x to 1.0

1.0 is a promise about compatibility, so it is gated on evidence that the
promise can be kept rather than on a date:

- [ ] The command and fact wires are production ready: signed commands reach
      exact runner subjects, and signed facts are durably ingested.
- [ ] Three consecutive minor releases with no breaking change to the published
      schemas or the console entry point.
- [ ] The published schemas cover the command decode seam and the signed fact
      output contract.
- [ ] The [EOL table](/release-governance/semver-lts) has at least one line
      already inside its window — that is, the support promise has been kept
      once in practice before it is made permanently.

The last one is the point of the exercise. A 1.0 declared before any line has
been carried through its own support window is a promise with no evidence
behind it.

Once the boxes are checked the promote itself is mechanical: bump the version,
add the changelog section, tag it, and add the new line to the EOL table.

## Template for a minor bump

```
## `0.<prev>.x` → `0.<next>.0`

### What changed

- {feature | fix | breaking? summary}

### Migration steps

- {commands the operator must run}

### Rollback

- Reinstall the previous minor line. Configuration stays backward-compatible
  within a minor line, so a rollback is a version change and nothing else.
```

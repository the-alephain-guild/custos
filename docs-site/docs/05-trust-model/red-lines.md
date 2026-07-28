---
title: "The Four Guarantees"
sidebar_position: 1
---

# The Four Guarantees

Custos asks you to hand it exchange credentials. Four properties are what make
that a reasonable thing to do. They are not features and not defaults — they
hold regardless of what a deployment asks for, and each one is enforced in more
than one place so that a single mistake cannot remove it.

| # | Guarantee | In one sentence |
|---|---|---|
| 1 | [Keys never leave the host](./keys-never-leave-the-host) | The decryption key is never transmitted, logged or published. |
| 2 | [Live execution is always gated](./live-execution-is-gated) | Reaching a live venue requires four checks, and the gate fails closed. |
| 3 | [Safety survives a disconnect](./safety-survives-disconnect) | Local enforcement keeps working while the platform is unreachable. |
| 4 | [Money arithmetic is exact](./exact-money-arithmetic) | Decimal end to end, strings on the wire, no floating point. |

## Why these four

Each one closes a way the system could betray you that you would not detect in
time.

**Keys.** A custodial service can be compromised once and lose everyone's
credentials at the same time. Custos cannot: the credentials are on your
machine, decrypted by a key that is also on your machine. Compromising the
platform gets an attacker deployment metadata, not your funds.

**The gate.** A live order routed to a host that quietly does nothing looks
exactly like a live order that succeeded. You would find out at reconciliation,
which is far too late. So the gate refuses rather than degrades.

**Disconnect.** Two opposite failures are both real: stopping on a network blip
flattens positions nobody asked to close; continuing without limits removes the
protection precisely when nobody is watching. The runner does neither.

**Money.** Floating point error in a notional calculation does not announce
itself. It produces a number that is almost right, passes every eyeball check,
and is wrong in the direction of the rounding. Decimal arithmetic removes the
class.

## How they are enforced

Every guarantee has more than one enforcement point. A credential's scope is
checked when written *and* on every decrypt; the gate is a capability
declaration *and* a venue check *and* a code hash *and* a credential scope.

This is deliberate. A single enforcement point is one refactor away from being
bypassed, and the bypass is invisible until it matters.

## Verifying them yourself

Do not take this page's word for any of it. The
[audit checklist](./audit-checklist) is a step-by-step procedure — commands to
run, files to read, and what a passing result actually proves. It needs no
cooperation from us beyond the source you already have.

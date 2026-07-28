# Security Policy

`custos-runner` is the non-custodial edge of the Alephain Guild
trading ecosystem: it holds live exchange API keys on the operator's
machine and it is what the ecosystem's "keys never leave the operator's
process" promise ultimately means in practice. Security reports are
prioritised.

## Supported versions

Only the current minor line receives security patches inside the SLA
window. The full LTS commitment (which minor lines are still supported
and until when) lives in
[`docs/lts-commitment.md`](docs/lts-commitment.md) — that document is
authoritative; this section is a pointer.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**
Instead, use GitHub Security Advisories:

- [Report a vulnerability](https://github.com/the-alephain-guild/custos/security/advisories/new)

This creates a private issue visible only to the reporter and the
project maintainers.

If you cannot use the GitHub UI for some reason, an alternative report
channel will be added here in a future release (a PGP contact is not
provided in the 0.2.x line; opening a private advisory is the
recommended path).

## What to include

- A short description of the finding and its impact.
- A reproduction path — ideally a minimal script or unit-test snippet.
- Which version(s) you observed the finding on.
- Whether you plan to disclose publicly, and on what timeline.

## What we commit to

- Acknowledge receipt within **72 hours** of the advisory being filed.
- Coordinate a fix and, if feasible, ship a patch release within
  **30 days** of confirmation (best-effort; see
  [`docs/lts-commitment.md`](docs/lts-commitment.md) §Security Patch
  SLA).
- Publish a corresponding GitHub Security Advisory within **24 hours**
  of the patch release.
- Give credit to the reporter in the advisory (unless requested
  otherwise).

## Backport policy

Security fixes are backported to every still-supported minor line
listed in the LTS commitment. Critical functional-bug backports are assessed case by case and
announced with the release.

## Scope

The `custos-runner` daemon, its bundled `arx-runner` CLI, its Dockerfile,
its `.github/workflows/release.yml` release pipeline, and any code
under `src/custos/` are in scope. <!-- disclosure-ok: researchers need the exact code scope --> Third-party dependencies (NautilusTrader,
`nats-py`, sigstore-python, etc.) should be reported directly to their
upstreams; if a downstream integration bug in this repo can be triggered
by a well-behaved upstream, it is in scope.

## Non-custodial red lines

Four properties are the security backbone of this daemon:

1. Keys never leave the host process.
2. Live execution is always gated and the gate fails closed.
3. Local safety enforcement survives losing the platform connection.
4. Money arithmetic is exact — decimal end to end, never floating point.

They are described in full at
[custos.alephain.com/trust-model/red-lines](https://custos.alephain.com/trust-model/red-lines).
A finding that breaks any of them is categorically Critical and will get an
expedited fix.

## Warranty disclaimer

`custos-runner` is licensed under
[Apache-2.0](LICENSE), which is a permissive open-source license
provided **"as is", without warranty of any kind, either express or
implied**, including without limitation any warranties of title,
non-infringement, merchantability, or fitness for a particular
purpose. See LICENSE §7 "Disclaimer of Warranty" and §8 "Limitation
of Liability" for the full text. Operators run the daemon at their
own risk against their own trading capital; the SLA numbers above are
best-effort maintainer commitments, not contractual guarantees.

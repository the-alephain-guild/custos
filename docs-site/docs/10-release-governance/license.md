---
title: "License & NOTICE"
sidebar_position: 4
---

# License & NOTICE

Custos is licensed under **Apache-2.0**, and has been since its first public
release.

- **[LICENSE](https://github.com/the-alephain-guild/custos/blob/main/LICENSE)** — the full text
- **[NOTICE](https://github.com/the-alephain-guild/custos/blob/main/NOTICE)** — attribution required by section 4(d)

## Why this license, specifically

The runner holds your exchange credentials. The only condition under which
trusting it is rational is that you can read what it does with them — so a
license that permits reading, auditing, forking and running the code is not a
distribution preference here, it is what makes the central claim checkable.

Apache-2.0 also carries an explicit patent grant, which a permissive license
without one does not. For software that executes financial transactions, that
distinction is worth having in writing.

## What it means for you

You may use, modify and redistribute Custos, including commercially, provided
you retain the license and NOTICE and state significant changes. There is no
copyleft obligation on your own strategies or on anything you build around the
runner.

## Contributing

There is no CLA. Apache-2.0's own contribution grant is sufficient, so
contributing does not require signing anything separate — see
[contributing](/release-governance/contributing).

## Third-party components

Vendored third-party code retains its own license and is listed in the NOTICE
file. Guarding it by exact digest rather than rewriting it is deliberate: a
vendored dependency you have modified is one you can no longer compare against
its upstream.

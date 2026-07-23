"""``arx-runner`` subcommand dispatcher.

Registered as ``[project.scripts].arx-runner = "custos.cli.subcommands:main"``
in ``pyproject.toml``. The legacy ``python -m custos`` entry returns exit
2 with a pointer to this dispatcher; there is no compatibility bridge.

Wires top-level lifecycle and management subcommands
each in its own handler module so parse and execution live together.
``vault`` further nests ``put`` / ``verify`` / ``list`` because they share
key-id parsing and the ``~/.arx/vault/`` directory conventions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from custos.cli.subcommands import (
    credential,
    enroll,
    health,
    nats_transport,
    publish_capability,
    start,
    vault,
)


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the chosen subcommand.

    Returns the subcommand handler's exit code. Argparse itself calls
    ``sys.exit`` on parse errors and on ``--help``; the caller
    (``pyproject.toml`` console script) wraps this in ``sys.exit`` so
    the process exits with the handler's return code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("no subcommand given")  # exits non-zero
    result = handler(args)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return int(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arx-runner",
        description=(
            "custos self-hosted runner — enroll, provision keys, reconcile "
            "signed desired state, and publish signed RunnerFacts."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="cmd",
        metavar="{credential,enroll,nats-transport,publish-capability,health,start,vault}",
    )
    credential.register(subparsers)
    enroll.register(subparsers)
    publish_capability.register(subparsers)
    health.register(subparsers)
    nats_transport.register(subparsers)
    start.register(subparsers)
    vault.register(subparsers)
    return parser


if __name__ == "__main__":
    sys.exit(main())

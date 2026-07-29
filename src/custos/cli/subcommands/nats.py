"""``arx-runner nats bootstrap`` — create the offline lane's JetStream topology.

Only the standalone profile exists: this command provisions infrastructure the
operator owns. The signed lane's transport is provisioned by its own authority
and is not reachable from here.
"""

from __future__ import annotations

import argparse
import sys

from custos.cli.validators import validate_id
from custos.offline.transport import bootstrap_standalone_streams


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("nats", help="Manage Custos-owned NATS infrastructure.")
    actions = parser.add_subparsers(dest="action", metavar="{bootstrap}")
    bootstrap = actions.add_parser(
        "bootstrap", help="Create the offline lane's JetStream topology."
    )
    bootstrap.add_argument("--profile", required=True, choices=["standalone"])
    bootstrap.add_argument("--nats-url", default="nats://localhost:4222")
    bootstrap.add_argument(
        "--tenant-id", required=True, type=lambda value: validate_id("tenant_id", value)
    )
    bootstrap.add_argument("--timeout-secs", type=float, default=30.0)
    bootstrap.set_defaults(action_handler=_bootstrap)
    parser.set_defaults(handler=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    handler = getattr(args, "action_handler", None)
    if handler is None:
        print("nats requires an action ({bootstrap})", file=sys.stderr)
        return 2
    return handler(args)


async def _bootstrap(args: argparse.Namespace) -> int:
    try:
        await bootstrap_standalone_streams(
            nats_url=args.nats_url,
            tenant_id=args.tenant_id,
            timeout_secs=args.timeout_secs,
        )
    except Exception as exc:  # noqa: BLE001 - the CLI reports infrastructure failure as exit 1
        print(f"NATS bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(f"offline lane topology ready for tenant {args.tenant_id}")
    return 0

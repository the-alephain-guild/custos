"""``arx-runner health`` readiness probe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from custos.core.readiness import read_ready_file

DEFAULT_READY_FILE = Path.home() / ".arx" / "state" / "runner-ready.json"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("health", help="Check runner readiness state.")
    parser.add_argument("--ready-file", type=Path, default=DEFAULT_READY_FILE)
    parser.add_argument(
        "--json", action="store_true", help="Print the exact runtime health V1 JSON."
    )
    parser.set_defaults(handler=_health)


def _health(args: argparse.Namespace) -> int:
    state = read_ready_file(args.ready_file)
    json_output = bool(getattr(args, "json", False))
    if state is None:
        if json_output:
            print(json.dumps({"ready": False, "path": str(args.ready_file)}, sort_keys=True))
            return 1
        print(f"runner is not ready: {args.ready_file}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(state, separators=(",", ":"), sort_keys=True))
        return 0
    print(f"runner is ready: {args.ready_file}")
    return 0

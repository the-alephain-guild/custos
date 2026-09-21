"""``arx-runner breaker`` — inspect and lift a local circuit-breaker freeze.

The fallback breaker trips on a notional or drawdown breach, flattens positions,
and freezes further orders "until an operator intervenes". This is that
intervention. Nothing else lifts a freeze: restarts, config refreshes and day
boundaries all leave it standing.

Releasing is not a pardon. The next evaluation re-freezes if the breach is still
there, which is the point -- this command records a decision, it does not change
the account.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from custos.core.runner_fact import RunnerFactOutbox, RunnerStateAuthorityError

DEFAULT_RUNNER_FACT_OUTBOX = Path.home() / ".arx" / "state" / "runner-fact-outbox.db"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "breaker",
        help="Inspect or lift a deployment instance's circuit-breaker freeze.",
    )
    actions = parser.add_subparsers(dest="action", metavar="{status,clear}")

    status = actions.add_parser("status", help="Show the durable breaker state.")
    _add_common(status)
    status.add_argument("--json", action="store_true", help="Print the state as JSON.")
    status.set_defaults(handler=_status)

    clear = actions.add_parser("clear", help="Lift a freeze, on the record.")
    _add_common(clear)
    clear.add_argument(
        "--operator",
        required=True,
        help="Who is lifting it. Attribution, not authentication.",
    )
    clear.add_argument("--reason", required=True, help="Why it is safe to resume.")
    clear.set_defaults(handler=_clear)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment-instance-id", required=True, type=UUID)
    parser.add_argument("--runner-fact-outbox", type=Path, default=DEFAULT_RUNNER_FACT_OUTBOX)


def _status(args: argparse.Namespace) -> int:
    state = RunnerFactOutbox(args.runner_fact_outbox).load_breaker_state_sync(
        args.deployment_instance_id
    )
    if state is None:
        if bool(getattr(args, "json", False)):
            print(json.dumps({"deployment_instance_id": str(args.deployment_instance_id)}))
            return 1
        print(
            f"no breaker state recorded for {args.deployment_instance_id}",
            file=sys.stderr,
        )
        return 1
    if bool(getattr(args, "json", False)):
        print(json.dumps(_as_json(state), separators=(",", ":"), sort_keys=True))
        return 0
    if state.frozen:
        print(f"frozen since {state.frozen_at_ns} — {state.reason_code}")
        print(f"peak equity {state.peak_equity}")
        return 1
    print(f"not frozen — peak equity {state.peak_equity}")
    if state.released_by is not None:
        print(f"last released by {state.released_by}: {state.release_reason}")
    return 0


def _clear(args: argparse.Namespace) -> int:
    try:
        state = RunnerFactOutbox(args.runner_fact_outbox).release_breaker_sync(
            deployment_instance_id=args.deployment_instance_id,
            operator=args.operator,
            reason=args.reason,
        )
    except RunnerStateAuthorityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"released by {state.released_by}: {state.release_reason}")
    print("the next evaluation re-freezes if the breach is still there")
    return 0


def _as_json(state) -> dict[str, object]:
    return {
        "deployment_instance_id": str(state.deployment_instance_id),
        "peak_equity": str(state.peak_equity),
        "frozen": state.frozen,
        "reason_code": state.reason_code,
        "frozen_at_ns": state.frozen_at_ns,
        "released_at_ns": state.released_at_ns,
        "released_by": state.released_by,
        "release_reason": state.release_reason,
    }

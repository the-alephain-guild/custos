"""``arx-runner deployment {validate,publish}`` — the offline lane's entry points.

Both actions pass through ``custos.offline.mode_guard`` before anything is parsed
as a contract or sent anywhere, so a live spec is refused without a connection
ever being opened.

The flag names are the consumer's, not ours: `philosophers-stone/deploy/custos`
invokes these from its compose file and Makefile. Renaming one breaks it silently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import nats
from pydantic import ValidationError

from custos.cli.validators import validate_id
from custos.core.log import get_logger
from custos.offline.mode_guard import OfflineModeRefused, admit_offline_spec
from custos.offline.spec import (
    OfflineDeploymentMessage,
    OfflineDeploymentSpec,
    compute_strategy_code_hash,
)

_log = get_logger("custos.cli.deployment")

_SPEC_FAILURES = (OSError, ValueError, ValidationError, OfflineModeRefused)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "deployment",
        help="Validate or publish offline desired state (sandbox and testnet only).",
    )
    actions = parser.add_subparsers(dest="action", metavar="{validate,publish}")
    _register_validate(actions)
    _register_publish(actions)
    parser.set_defaults(handler=_dispatch)


def _register_validate(actions: argparse._SubParsersAction) -> None:
    parser = actions.add_parser(
        "validate", help="Check a deployment spec offline, without connecting."
    )
    parser.add_argument("--spec-file", required=True, type=Path)
    parser.add_argument("--strategy-dir", type=Path, default=None)
    _add_mode_argument(parser)
    parser.set_defaults(action_handler=_validate)


def _register_publish(actions: argparse._SubParsersAction) -> None:
    parser = actions.add_parser("publish", help="Publish a deployment spec to a local NATS.")
    parser.add_argument("--spec-file", required=True, type=Path)
    parser.add_argument(
        "--tenant-id", required=True, type=lambda value: validate_id("tenant_id", value)
    )
    parser.add_argument(
        "--strategy-id", required=True, type=lambda value: validate_id("strategy_id", value)
    )
    parser.add_argument("--nats-url", default="nats://localhost:4222")
    parser.add_argument("--strategy-dir", type=Path, default=None)
    _add_mode_argument(parser)
    parser.set_defaults(action_handler=_publish)


def _add_mode_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Assert the trading mode the spec must carry. Omit to let the spec's own "
            "claim stand alone; live is refused either way."
        ),
    )


def _dispatch(args: argparse.Namespace) -> int:
    handler = getattr(args, "action_handler", None)
    if handler is None:
        print("deployment requires an action ({validate,publish})", file=sys.stderr)
        return 2
    return handler(args)


def _validate(args: argparse.Namespace) -> int:
    try:
        spec = _load_spec(args)
    except _SPEC_FAILURES as exc:
        print(f"deployment spec rejected: {exc}", file=sys.stderr)
        return 1
    digest = spec.code_hash or "not pinned"
    print(
        f"deployment spec valid: {spec.spec_id} generation {spec.generation} "
        f"mode {spec.trading_mode.value} state {spec.lifecycle_state.value} digest {digest}"
    )
    return 0


async def _publish(args: argparse.Namespace) -> int:
    try:
        spec = _load_spec(args)
        message = OfflineDeploymentMessage.create(
            tenant_id=args.tenant_id,
            strategy_id=args.strategy_id,
            spec=spec,
        )
    except _SPEC_FAILURES as exc:
        print(f"deployment spec rejected: {exc}", file=sys.stderr)
        return 1

    connection = None
    try:
        connection = await nats.connect(args.nats_url)
        acknowledgement = await connection.jetstream().publish(message.subject, message.to_bytes())
        if acknowledgement is None:
            raise RuntimeError("JetStream accepted the publish without acknowledging it")
    except Exception as exc:  # noqa: BLE001 - the CLI reports transport failure and exits non-zero
        _log.warning("offline_spec_publish_failed", subject=message.subject, error=str(exc))
        print(f"deployment publish failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            await connection.drain()

    _log.info(
        "offline_spec_published",
        subject=message.subject,
        generation=spec.generation,
        mode=spec.trading_mode.value,
    )
    print(f"published {spec.spec_id} generation {spec.generation} to {message.subject}")
    return 0


def _load_spec(args: argparse.Namespace) -> OfflineDeploymentSpec:
    """Refuse on mode first, then resolve the strategy digest, then parse."""

    raw = args.spec_file.read_bytes()
    admit_offline_spec(raw, command_mode=args.mode)
    document = _spec_document(raw)
    if args.strategy_dir is not None:
        _bind_strategy_digest(document, args.strategy_dir)
    return OfflineDeploymentSpec.model_validate(document)


def _spec_document(raw: bytes) -> dict[str, Any]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("a deployment spec must be a JSON object")
    return document


def _bind_strategy_digest(document: dict[str, Any], strategy_dir: Path) -> None:
    """Compute the digest, then either supply it or check what was declared.

    The consumer renders a null digest and expects the bind-mounted directory to
    supply it. A digest that *is* declared is an assertion about the directory,
    so it gets checked rather than overwritten.
    """

    actual = compute_strategy_code_hash(strategy_dir)
    declared = document.get("code_hash")
    if declared is None:
        document["code_hash"] = actual
        return
    if declared != actual:
        raise ValueError(
            f"strategy directory digest {actual} differs from the declared code_hash {declared}"
        )

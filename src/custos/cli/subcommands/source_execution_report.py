"""Export one exact target's local callback evidence without modifying its inbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority

_DELIVERY_ID = re.compile(r"[0-9a-f]{64}\Z")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export local source target callbacks; this is not a venue receipt."
    )
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--delivery-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--spec-id", required=True)
    parser.add_argument("--spec-digest", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--mode", choices=("sandbox", "testnet"), required=True)
    return parser


def read_report(args: argparse.Namespace) -> dict[str, object]:
    path = Path(args.inbox)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("source target inbox does not exist") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("source target inbox must be an owner-only regular file")
    if not _DELIVERY_ID.fullmatch(args.delivery_id):
        raise ValueError("source target delivery ID is invalid")
    authority = EngineLifecycleAuthority(
        deployment_instance_id=UUID(args.instance_id),
        deployment_spec_id=UUID(args.spec_id),
        deployment_spec_digest=args.spec_digest,
        generation=args.generation,
        trading_mode=args.mode,
    )
    uri = f"file:{quote(str(path.absolute()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        required_columns = {
            "external_target_delivery": {
                "delivery_id",
                "source_event_id",
                "connector",
                "target",
                "status",
                "deployment_instance_id",
                "deployment_spec_id",
                "deployment_spec_digest",
                "generation",
                "tenant_id",
                "source_id",
                "pair",
            },
            "external_target_flat_fence": {
                "flat_delivery_id",
                "deployment_instance_id",
                "tenant_id",
                "source_id",
                "pair",
                "confirmation_source",
                "observation_at_ns",
                "confirmed_at_ns",
            },
            "external_target_order_link": {
                "deployment_instance_id",
                "delivery_id",
                "client_order_id",
                "instrument",
                "initialized_at_ns",
            },
            "external_target_fill_link": {
                "deployment_instance_id",
                "client_order_id",
                "fill_event_id",
                "venue_order_id",
                "venue_trade_id",
                "quantity",
                "filled_at_ns",
            },
        }
        for table, required in required_columns.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(columns):
                raise ValueError("source target inbox schema requires runner migration")
        target = connection.execute(
            """SELECT target.delivery_id, target.source_event_id, target.connector,
                      target.target, target.status, fence.confirmation_source,
                      fence.observation_at_ns, fence.confirmed_at_ns
               FROM external_target_delivery AS target
               LEFT JOIN external_target_flat_fence AS fence
                 ON fence.flat_delivery_id = target.delivery_id
                AND fence.deployment_instance_id = target.deployment_instance_id
                AND fence.tenant_id = target.tenant_id
                AND fence.source_id = target.source_id
                AND fence.pair = target.pair
               WHERE target.delivery_id = ? AND target.deployment_instance_id = ?
                 AND target.deployment_spec_id = ?
                 AND target.deployment_spec_digest = ?
                 AND target.generation = ?""",
            (
                args.delivery_id,
                str(authority.deployment_instance_id),
                str(authority.deployment_spec_id),
                authority.deployment_spec_digest,
                authority.generation,
            ),
        ).fetchone()
        if target is None:
            raise ValueError("source target was not found for the supplied authority")
        orders = connection.execute(
            """SELECT client_order_id, instrument, initialized_at_ns
               FROM external_target_order_link
               WHERE deployment_instance_id = ? AND delivery_id = ?
               ORDER BY initialized_at_ns, client_order_id""",
            (str(authority.deployment_instance_id), args.delivery_id),
        ).fetchall()
        rendered_orders = []
        local_fill_count = 0
        for order in orders:
            fills = connection.execute(
                """SELECT fill_event_id, venue_order_id, venue_trade_id,
                          quantity, filled_at_ns
                   FROM external_target_fill_link
                   WHERE deployment_instance_id = ? AND client_order_id = ?
                   ORDER BY filled_at_ns, fill_event_id""",
                (str(authority.deployment_instance_id), order["client_order_id"]),
            ).fetchall()
            local_fill_count += len(fills)
            rendered_orders.append(
                {
                    "clientOrderId": order["client_order_id"],
                    "instrument": order["instrument"],
                    "initializedAtNs": order["initialized_at_ns"],
                    "filledQuantity": str(
                        sum((Decimal(fill["quantity"]) for fill in fills), Decimal(0))
                    ),
                    "fillEventIds": [fill["fill_event_id"] for fill in fills],
                    "venueOrderIds": [fill["venue_order_id"] for fill in fills],
                    "venueTradeIds": [fill["venue_trade_id"] for fill in fills],
                    "fillAtNs": [fill["filled_at_ns"] for fill in fills],
                }
            )
    finally:
        connection.close()
    return {
        "evidenceGrade": "local_callbacks_only",
        "identitySource": "caller_input_matched_to_local_inbox",
        "deploymentInstanceId": str(authority.deployment_instance_id),
        "deploymentSpecId": str(authority.deployment_spec_id),
        "deploymentSpecDigest": authority.deployment_spec_digest,
        "generation": authority.generation,
        "modeClaimed": authority.trading_mode,
        "target": {
            "deliveryId": target["delivery_id"],
            "sourceEventId": target["source_event_id"],
            "connector": target["connector"],
            "target": target["target"],
            "handoffStatus": target["status"],
            "flatConfirmationSourceClaim": target["confirmation_source"],
            "flatObservedAtNs": target["observation_at_ns"],
            "flatConfirmedAtNs": target["confirmed_at_ns"],
            "localFillCount": local_fill_count,
            "orders": rendered_orders,
        },
    }


def main(argv: list[str] | None = None) -> int:
    document = read_report(_build_parser().parse_args(argv))
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

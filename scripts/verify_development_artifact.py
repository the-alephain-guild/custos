#!/usr/bin/env python3
"""Verify one sandbox-only, content-addressed development strategy artifact."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from custos.artifacts.development_source import (
    DevelopmentSourceRefV1,
    DevelopmentSourceVerificationError,
    verify_development_artifact,
)


def _required_digest(value: str) -> str:
    normalized = value.strip()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise argparse.ArgumentTypeError("value must be a lowercase SHA-256 digest")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a PS development artifact and its exact publication receipt. "
            "This lane is sandbox-only and cannot be promoted."
        )
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(
            os.environ.get(
                "CUSTOS_DEVELOPMENT_ARTIFACT_ROOT",
                str(Path.home() / ".alephain" / "v1-team" / "strategy-artifacts"),
            )
        ),
    )
    parser.add_argument("--source-sha256", required=True, type=_required_digest)
    parser.add_argument(
        "--publication-receipt-digest",
        required=True,
        type=_required_digest,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = args.artifact_root.expanduser().resolve()
    source_path = root / "sha256" / args.source_sha256
    source_ref = DevelopmentSourceRefV1(
        schema_version=1,
        source_path=str(source_path),
        source_sha256=args.source_sha256,
        trading_mode="sandbox",
        promotable=False,
    )
    try:
        verified = verify_development_artifact(
            source_ref,
            publication_receipt_digest=args.publication_receipt_digest,
            configured_root=root,
            runtime_mode="sandbox",
        )
    except (DevelopmentSourceVerificationError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"Development artifact verification failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "schema_version": 1,
                "verification_status": "verified",
                "trading_mode": "sandbox",
                "promotable": False,
                "source_sha256": args.source_sha256,
                "publication_receipt_digest": args.publication_receipt_digest,
                "entry_point_group": verified.entry_point_group,
                "entry_point": verified.entry_point,
                "strategy_wheel": verified.strategy_wheel_path.name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

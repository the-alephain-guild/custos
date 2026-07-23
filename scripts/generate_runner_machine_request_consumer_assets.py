#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "docs/authority/crucible-runner-machine-request-consumer-assets-v1.json"
RECEIPT_PATH = (
    ROOT / "docs/authority/receipts/custos-runner-machine-request-v1-consumer-receipt.json"
)
SIDECAR_PATH = RECEIPT_PATH.with_suffix(f"{RECEIPT_PATH.suffix}.sha256")

PRODUCER_COMMIT = "d9df47501f7a871c5b0691b8daf6d83fc3cd82c0"
CONSUMER_COMMIT = "de194eb623f06725dfdd417d71f364098692d76f"

PRODUCER_ASSETS = (
    (
        "docs/authority/runner-machine-request-golden-v1.json",
        "docs/authority/vendor/crucible-runner-machine-request-golden-v1.json",
    ),
    (
        "docs/authority/runner-machine-request-golden-v1.schema.json",
        "docs/authority/vendor/crucible-runner-machine-request-golden-v1.schema.json",
    ),
)
CONSUMER_ASSETS = (
    "src/custos/core/machine_credential_vault.py",
    "src/custos/cli/subcommands/enroll.py",
    "tests/test_runner_machine_request_contract.py",
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_pin(logical_path: str, actual_path: str | None = None) -> dict[str, object]:
    data = (ROOT / (actual_path or logical_path)).read_bytes()
    return {"path": logical_path, "sha256": _sha256(data), "size_bytes": len(data)}


def _verify_consumer_commit() -> None:
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{CONSUMER_COMMIT}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if resolved != CONSUMER_COMMIT:
        raise SystemExit("consumer commit does not resolve exactly")
    tracked = subprocess.run(
        ["git", "diff", "--quiet", CONSUMER_COMMIT, "--", *CONSUMER_ASSETS],
        cwd=ROOT,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *CONSUMER_ASSETS],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tracked.returncode != 0 or untracked:
        raise SystemExit("machine-request consumer assets differ from the pinned commit")


def _build_index() -> dict[str, object]:
    return {
        "schema_version": 1,
        "asset_index_id": "custos.crucible-runner-machine-request-consumer.v1",
        "status": "DIRECT_CREDENTIAL_V1_READY_PENDING_CR100_0029_RUNTIME_RECEIPT",
        "producer": {
            "repository": "tesseract-trading/crucible-rust",
            "plan": 100,
            "commit": PRODUCER_COMMIT,
            "status": "CONTRACT_READY_RUNTIME_PENDING",
        },
        "consumer": {
            "repository": "tesseract-trading/custos",
            "plan": 19,
            "commit": CONSUMER_COMMIT,
            "status": "DIRECT_CREDENTIAL_CLIENT_READY_NATS_TRANSPORT_PENDING",
        },
        "contract": {
            "request_domain": "crucible.runner.machine.request.v1",
            "machine_header_prefix": "X-Crucible-",
            "enrollment_path": "/api/v1/runner-enrollments",
            "credential_paths": [
                "/api/v1/runner-credentials/verify",
                "/api/v1/runner-credentials/rotate",
                "/api/v1/runner-credentials/revoke",
            ],
            "max_clock_skew_seconds": 60,
            "legacy_aliases": [],
        },
        "producer_assets": [
            _asset_pin(logical_path, actual_path) for logical_path, actual_path in PRODUCER_ASSETS
        ],
        "consumer_assets": [_asset_pin(path) for path in CONSUMER_ASSETS],
        "claims": {
            "exact_cross_language_golden_verified": True,
            "direct_enrollment_and_credential_client_ready": True,
            "arx_machine_relay_required": False,
            "durable_request_replay_ledger_verified": False,
            "per_mode_nats_issuance_verified": False,
            "production_transport_ready": False,
        },
        "open_blockers": [
            "CR100 control migration 0029 after executed CR88 0028",
            "durable same-request replay and conflict receipt",
            "per-mode NATS JWT, ACL, durable readback, rotation and revocation receipt",
        ],
    }


def _build_receipt(index_bytes: bytes) -> dict[str, object]:
    return {
        "schema_version": 1,
        "stable_receipt_id": "CUSTOS-RUNNER-MACHINE-REQUEST-V1",
        "status": "DIRECT_CREDENTIAL_V1_READY_PENDING_CONTROL_DATABASE_AND_NATS_RUNTIME",
        "producer": {
            "repository": "tesseract-trading/crucible-rust",
            "commit": PRODUCER_COMMIT,
        },
        "consumer": {
            "repository": "tesseract-trading/custos",
            "commit": CONSUMER_COMMIT,
        },
        "asset_index": {
            "path": str(INDEX_PATH.relative_to(ROOT)),
            "sha256": _sha256(index_bytes),
            "size_bytes": len(index_bytes),
        },
        "contract": {
            "request_domain": "crucible.runner.machine.request.v1",
            "machine_header_prefix": "X-Crucible-",
            "enrollment_path": "/api/v1/runner-enrollments",
            "credential_paths": [
                "/api/v1/runner-credentials/verify",
                "/api/v1/runner-credentials/rotate",
                "/api/v1/runner-credentials/revoke",
            ],
        },
        "claims": {
            "exact_cross_language_golden_verified": True,
            "direct_enrollment_and_credential_client_ready": True,
            "arx_machine_relay_absent": True,
            "durable_request_replay_ledger_verified": False,
            "per_mode_nats_issuance_verified": False,
            "production_transport_ready": False,
        },
        "verification": [
            {
                "command": (
                    "uv run pytest -q tests/test_runner_machine_request_contract.py "
                    "tests/test_cli_enroll.py"
                ),
                "result": "15 passed",
            },
            {
                "command": (
                    "uv run ruff check src/custos/core/machine_credential_vault.py "
                    "src/custos/cli/subcommands/enroll.py "
                    "tests/test_runner_machine_request_contract.py tests/test_cli_enroll.py"
                ),
                "result": "passed",
            },
        ],
        "open_blockers": [
            "control migration 0029 after executed StrategyRelease evidence migration 0028",
            "durable same-request replay and conflict receipt",
            "per-mode NATS JWT, ACL, durable readback, rotation and revocation receipt",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    _verify_consumer_commit()

    index_bytes = _json_bytes(_build_index())
    receipt_bytes = _json_bytes(_build_receipt(index_bytes))
    sidecar_bytes = f"{_sha256(receipt_bytes)}  {RECEIPT_PATH.name}\n".encode()
    expected = {
        INDEX_PATH: index_bytes,
        RECEIPT_PATH: receipt_bytes,
        SIDECAR_PATH: sidecar_bytes,
    }

    drift = [
        path for path, data in expected.items() if not path.is_file() or path.read_bytes() != data
    ]
    if args.check:
        for path in drift:
            print(f"generated machine-request consumer asset differs: {path.relative_to(ROOT)}")
        return 1 if drift else 0
    for path, data in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"generated {len(expected)} machine-request consumer assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

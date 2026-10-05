#!/usr/bin/env python3
"""Generate the contract revision index consumers vendor from, and enforce its rule.

docs/authority/contract-revisions-v1.json lists every contract this repository
produces for another repository: its current revision, wire fingerprint, the
assets a consumer vendors and the conformance vectors it runs, with the
fingerprints of every earlier revision. A contract's revision is raised only when
its wire shape or the meaning of a field changes, which the vectors pin. The rule
is mechanical: a changed wire fingerprint or vector file under an unchanged
revision fails; a raised revision with neither changed is only a warning, since
a meaning change the vectors do not yet cover is possible.

``--check`` fails on a rule violation or when the committed index differs from
the regenerated one; without it the index is rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = "docs/authority/contract-revisions-v1.json"
PRODUCER_REPOSITORY = "tesseract-trading/custos"
FIRST_REVISION_NOTE = "first revision; earlier versions of the contract carried no revision"


def _contract_vendor():
    path = Path(__file__).resolve().with_name("contract_vendor.py")
    spec = importlib.util.spec_from_file_location("contract_vendor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Contract:
    contract_id: str
    assets: tuple[str, ...]
    schema_path: str | None = None
    vectors_path: str | None = None
    # Only for a contract without a schema to carry its revision.
    index_revision: int | None = None


RUNNER_FACT_VECTORS = "docs/authority/conformance/runner-fact-batch-v1.vectors.json"
STRATEGY_VECTORS = "docs/authority/conformance/strategy-canonical-json-v1.vectors.json"

CONTRACTS = (
    Contract(
        contract_id="alephain.custos.runner_fact_batch.v1",
        schema_path="docs/gateway-contract/v1/runner_fact_batch_v1.schema.json",
        vectors_path=RUNNER_FACT_VECTORS,
        assets=(
            "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json",
            "docs/authority/runner-fact-golden-v1.json",
            "docs/authority/runner-fact-capability-manifest-v1.json",
            "docs/authority/runner-fact-capability-receipt-golden-v1.json",
            "docs/authority/runner-fact-parity-matrix-v1.json",
            "docs/authority/runner-fact-signing-preimage-golden-v1.json",
            "docs/authority/runner-fact-terminal-boundary-golden-v1.json",
            "docs/authority/runner-fact-contract-assets-v1.json",
            RUNNER_FACT_VECTORS,
            RUNNER_FACT_VECTORS + ".sha256",
        ),
    ),
    Contract(
        contract_id="alephain.custos.strategy_artifact_ref.v1",
        schema_path="docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json",
        assets=(
            "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json",
            "docs/authority/strategy-artifact-ref-v1.golden.json",
            "docs/authority/strategy-contract-assets-v1.json",
        ),
    ),
    Contract(
        contract_id="alephain.custos.strategy_artifact_pre_import_verification_receipt.v1",
        schema_path=(
            "docs/gateway-contract/v1/"
            "strategy_artifact_pre_import_verification_receipt_v1.schema.json"
        ),
        assets=(
            "docs/gateway-contract/v1/"
            "strategy_artifact_pre_import_verification_receipt_v1.schema.json",
        ),
    ),
    Contract(
        contract_id="alephain.custos.runtime_candidate_acceptance.v1",
        schema_path="docs/gateway-contract/v1/runtime_candidate_acceptance_v1.schema.json",
        assets=("docs/gateway-contract/v1/runtime_candidate_acceptance_v1.schema.json",),
    ),
    Contract(
        contract_id="alephain.custos.strategy_canonical_json.v1",
        vectors_path=STRATEGY_VECTORS,
        index_revision=1,
        assets=(STRATEGY_VECTORS, STRATEGY_VECTORS + ".sha256"),
    ),
)


class RevisionRuleError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(root: Path, relative: str) -> Any:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _current(root: Path, contract: Contract, vendor) -> dict[str, Any]:
    if contract.schema_path is not None:
        schema = _json(root, contract.schema_path)
        if schema.get("x-contract-id") != contract.contract_id:
            raise RevisionRuleError(
                f"{contract.contract_id}: {contract.schema_path} names x-contract-id "
                f"{schema.get('x-contract-id')!r}"
            )
        revision = schema.get("x-contract-revision")
        wire = vendor.wire_fingerprint(schema)
    else:
        revision = contract.index_revision
        wire = None
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise RevisionRuleError(f"{contract.contract_id}: revision must be a positive integer")
    vectors = None
    if contract.vectors_path is not None:
        data = (root / contract.vectors_path).read_bytes()
        document = json.loads(data)
        if (
            document.get("contract_id") != contract.contract_id
            or document.get("contract_revision") != revision
        ):
            raise RevisionRuleError(
                f"{contract.contract_id}: {contract.vectors_path} names "
                f"{document.get('contract_id')} revision {document.get('contract_revision')}, "
                f"not revision {revision}"
            )
        vectors = _sha256(data)
    assets = []
    for relative in contract.assets:
        data = (root / relative).read_bytes()
        assets.append({"path": relative, "sha256": _sha256(data), "size_bytes": len(data)})
    return {"revision": revision, "wire_sha256": wire, "vectors_sha256": vectors, "assets": assets}


def build_index(root: Path, committed: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Return the regenerated index and warnings; raise when the revision rule fails."""

    vendor = _contract_vendor()
    previous = (committed or {}).get("contracts", {})
    warnings: list[str] = []
    contracts: dict[str, Any] = {}
    for contract in CONTRACTS:
        current = _current(root, contract, vendor)
        revision = current["revision"]
        history = list(previous.get(contract.contract_id, {}).get("history", []))
        recorded = {entry["revision"]: entry for entry in history}
        fingerprint = {
            "wire_sha256": current["wire_sha256"],
            "vectors_sha256": current["vectors_sha256"],
        }
        if revision in recorded:
            entry = recorded[revision]
            if (entry["wire_sha256"], entry["vectors_sha256"]) != tuple(fingerprint.values()):
                raise RevisionRuleError(
                    f"{contract.contract_id}: the wire fingerprint or the conformance vectors "
                    f"changed but the contract revision is still {revision}; raise "
                    "x-contract-revision (or the index revision) by one"
                )
        else:
            highest = max(recorded, default=0)
            if revision != highest + 1:
                raise RevisionRuleError(
                    f"{contract.contract_id}: revision {revision} does not follow the recorded "
                    f"revision {highest}"
                )
            if highest and (
                recorded[highest]["wire_sha256"],
                recorded[highest]["vectors_sha256"],
            ) == tuple(fingerprint.values()):
                warnings.append(
                    f"{contract.contract_id}: revision {revision} changes neither the wire "
                    "fingerprint nor the vectors; make sure a meaning change justifies it"
                )
            note = FIRST_REVISION_NOTE if revision == 1 else "raised"
            history.append({"revision": revision, **fingerprint, "note": note})
        contracts[contract.contract_id] = {
            "revision": revision,
            "revision_carrier": "schema" if contract.schema_path else "index",
            "schema_path": contract.schema_path,
            **fingerprint,
            "vectors_path": contract.vectors_path,
            "assets": current["assets"],
            "history": sorted(history, key=lambda entry: entry["revision"]),
        }
    index = {
        "index_schema_version": 1,
        "producer_repository": PRODUCER_REPOSITORY,
        "canonicalization": "utf8-json-sort-keys-compact-v1",
        "revision_rule": (
            "raise a contract's revision by one when its wire fingerprint or its conformance "
            "vectors change; descriptive text alone keeps the revision"
        ),
        "contracts": contracts,
    }
    return index, warnings


def _pretty(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    path = root / INDEX_PATH
    committed = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    try:
        index, warnings = build_index(root, committed)
    except RevisionRuleError as exc:
        print(f"contract revision rule failed: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    payload = _pretty(index)
    if args.check:
        if committed is None or path.read_bytes() != payload:
            print(
                f"{INDEX_PATH} differs from the regenerated index; regenerate it", file=sys.stderr
            )
            return 1
        print("contract revision index is exact")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    print(f"generated {INDEX_PATH} ({len(index['contracts'])} contracts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

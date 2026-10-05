#!/usr/bin/env python3
"""Vendor cross-repository contracts by schema revision and check the pins offline.

Canonical copy: tesseract-trading/crucible-rust scripts/contract_vendor.py. Every
other repository keeps a byte-identical copy; `check --sibling-copy <path>`
compares two copies when both checkouts are present. Standard library only.

A producer lists each contract in docs/authority/contract-revisions-v1.json with
its current revision, wire fingerprint, assets and conformance vectors. A
consumer copies those bytes with `sync` and records them in
docs/authority/vendor/contract-pins-v1.json, which names no commit of either
repository. `check` needs no producer checkout and verifies, per contract, the
vendored bytes, the schema's own revision, the wire fingerprint, the vectors'
revision and every `.sha256` sidecar against the file it describes.

Exit codes: 0 pins hold, 1 a pin and the bytes disagree, 2 usage or structure error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRODUCER_INDEX_PATH = "docs/authority/contract-revisions-v1.json"
PINS_PATH = "docs/authority/vendor/contract-pins-v1.json"
PINS_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1

# Annotation keywords of a schema object. They are removed only where a schema
# object is expected, never from a name table or from data such as `const`.
DESCRIPTIVE_KEYWORDS = frozenset(
    {"description", "title", "$comment", "examples", "x-contract-revision"}
)
NAME_TABLE_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "additionalItems",
        "unevaluatedItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
    }
)
SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems", "items"})


class UsageError(Exception):
    """Arguments or file structure are wrong; exit 2."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wire_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_wire_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in DESCRIPTIVE_KEYWORDS:
            continue
        if key in NAME_TABLE_KEYWORDS and isinstance(value, dict):
            result[key] = {name: _wire_schema(sub) for name, sub in value.items()}
        elif key in SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            result[key] = [_wire_schema(item) for item in value]
        elif key in SCHEMA_KEYWORDS and isinstance(value, dict):
            result[key] = _wire_schema(value)
        else:
            result[key] = value
    return result


def wire_fingerprint(schema: Any) -> str:
    """SHA-256 of the schema without descriptive keywords, canonically encoded."""

    return sha256_hex(canonical_json(_wire_schema(schema)))


def _load_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsageError(f"{label} is not JSON: {exc}") from exc


def _read_file(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise UsageError(f"{label} not found: {path}") from exc


class _Producer:
    def __init__(self, root: Path, ref: str | None) -> None:
        self.root = root
        self.ref = ref

    def read(self, relative: str) -> bytes:
        if self.ref is None:
            return _read_file(self.root / relative, "producer file")
        completed = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{self.ref}:{relative}"],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise UsageError(
                f"producer file {relative} not found at {self.ref}: "
                + completed.stderr.decode("utf-8", "replace").strip()
            )
        return completed.stdout

    def index(self) -> dict[str, Any]:
        index = _load_json(self.read(PRODUCER_INDEX_PATH), "producer revision index")
        if not isinstance(index, dict) or index.get("index_schema_version") != INDEX_SCHEMA_VERSION:
            raise UsageError("producer revision index has an unknown index_schema_version")
        if not isinstance(index.get("contracts"), dict) or not isinstance(
            index.get("producer_repository"), str
        ):
            raise UsageError("producer revision index lacks contracts or producer_repository")
        return index


def _load_pins(consumer: Path, *, required: bool) -> dict[str, Any]:
    path = consumer / PINS_PATH
    if not path.is_file():
        if required:
            raise UsageError(f"{PINS_PATH} not found under {consumer}")
        return {"pins_schema_version": PINS_SCHEMA_VERSION, "contracts": {}}
    pins = _load_json(path.read_bytes(), PINS_PATH)
    if (
        not isinstance(pins, dict)
        or pins.get("pins_schema_version") != PINS_SCHEMA_VERSION
        or not isinstance(pins.get("contracts"), dict)
    ):
        raise UsageError(f"{PINS_PATH} has an unknown structure")
    for contract_id, pin in pins["contracts"].items():
        if not isinstance(pin, dict) or not isinstance(pin.get("assets"), list):
            raise UsageError(f"{PINS_PATH} contract {contract_id} lacks assets")
        for asset in pin["assets"]:
            if not isinstance(asset, dict) or not all(
                isinstance(asset.get(field), str) for field in ("producer_path", "local_path")
            ):
                raise UsageError(f"{PINS_PATH} contract {contract_id} has a malformed asset")
        if not isinstance(pin.get("revision"), int) or isinstance(pin.get("revision"), bool):
            raise UsageError(f"{PINS_PATH} contract {contract_id} lacks an integer revision")
    return pins


def _write_pins(consumer: Path, pins: dict[str, Any]) -> None:
    path = consumer / PINS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(pins, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _producer_name(repository: str) -> str:
    return repository.rstrip("/").rsplit("/", 1)[-1]


def _default_local_path(producer_name: str, producer_path: str, vectors_path: str | None) -> str:
    name = Path(producer_path).name
    if vectors_path is not None and producer_path in (vectors_path, vectors_path + ".sha256"):
        return f"docs/authority/conformance/{producer_name}/{name}"
    return f"docs/authority/vendor/{producer_name}/{name}"


def _sidecar_digest(data: bytes) -> str | None:
    try:
        token = data.decode("ascii").split()[0]
    except (UnicodeDecodeError, IndexError):
        return None
    return token if len(token) == 64 and set(token) <= set("0123456789abcdef") else None


def _check_contract(consumer: Path, contract_id: str, pin: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    contents: dict[str, bytes] = {}
    for asset in pin["assets"]:
        local = asset["local_path"]
        path = consumer / local
        if not path.is_file():
            errors.append(f"{contract_id}: vendored file {local} is missing")
            continue
        data = path.read_bytes()
        contents[local] = data
        if sha256_hex(data) != asset.get("sha256") or len(data) != asset.get("size_bytes"):
            errors.append(f"{contract_id}: vendored file {local} differs from its pin")
    for local, data in contents.items():
        if local.endswith(".sha256") and local[: -len(".sha256")] in contents:
            described = contents[local[: -len(".sha256")]]
            if _sidecar_digest(data) != sha256_hex(described):
                errors.append(f"{contract_id}: sidecar {local} does not describe its file")

    revision = pin["revision"]
    schema_local = pin.get("schema_local_path")
    if pin.get("revision_carrier") == "schema":
        if schema_local not in contents:
            errors.append(f"{contract_id}: pinned schema {schema_local} is not vendored")
        else:
            schema = _load_json(contents[schema_local], schema_local)
            if schema.get("x-contract-id") != contract_id:
                errors.append(
                    f"{contract_id}: {schema_local} names x-contract-id "
                    f"{schema.get('x-contract-id')!r}"
                )
            if schema.get("x-contract-revision") != revision:
                errors.append(
                    f"{contract_id}: {schema_local} carries x-contract-revision "
                    f"{schema.get('x-contract-revision')} but the pin is revision {revision}"
                )
            if wire_fingerprint(schema) != pin.get("wire_sha256"):
                errors.append(
                    f"{contract_id}: {schema_local} wire fingerprint differs from the pin"
                )
    elif pin.get("revision_carrier") != "index":
        errors.append(f"{contract_id}: unknown revision_carrier {pin.get('revision_carrier')!r}")

    vectors_local = pin.get("vectors_local_path")
    if vectors_local is not None:
        if vectors_local not in contents:
            errors.append(f"{contract_id}: pinned vectors {vectors_local} are not vendored")
        else:
            data = contents[vectors_local]
            if sha256_hex(data) != pin.get("vectors_sha256"):
                errors.append(f"{contract_id}: {vectors_local} differs from vectors_sha256")
            vectors = _load_json(data, vectors_local)
            if vectors.get("contract_id") != contract_id:
                errors.append(f"{contract_id}: {vectors_local} names another contract")
            if vectors.get("contract_revision") != revision:
                errors.append(
                    f"{contract_id}: {vectors_local} carries contract_revision "
                    f"{vectors.get('contract_revision')} but the pin is revision {revision}"
                )
    return errors


def _compare_with_producer(pins: dict[str, Any], producer: _Producer) -> list[str]:
    index = producer.index()
    repository = index["producer_repository"]
    errors: list[str] = []
    for contract_id, pin in sorted(pins["contracts"].items()):
        if pin.get("producer_repository") != repository:
            continue
        entry = index["contracts"].get(contract_id)
        if entry is None:
            errors.append(f"{contract_id}: the producer no longer lists this contract")
            continue
        if entry["revision"] > pin["revision"]:
            errors.append(
                f"{contract_id}: producer is at revision {entry['revision']}, pinned "
                f"{pin['revision']}; evaluate the change and sync"
            )
            continue
        if entry["revision"] < pin["revision"]:
            errors.append(
                f"{contract_id}: producer is at revision {entry['revision']}, below the pinned "
                f"{pin['revision']}"
            )
            continue
        current = {asset["path"]: asset["sha256"] for asset in entry["assets"]}
        pinned = {asset["producer_path"]: asset["sha256"] for asset in pin["assets"]}
        if current != pinned:
            errors.append(
                f"{contract_id}: producer assets changed at the same revision "
                f"{pin['revision']} (descriptive change); run sync"
            )
    return errors


def command_sync(args: argparse.Namespace) -> int:
    consumer = Path(args.consumer_root).resolve()
    producer = _Producer(Path(args.producer_root).resolve(), args.producer_ref)
    index = producer.index()
    entry = index["contracts"].get(args.contract)
    if entry is None:
        raise UsageError(f"producer does not list contract {args.contract}")
    if args.revision is not None and entry["revision"] != args.revision:
        print(
            f"{args.contract}: producer is at revision {entry['revision']}, "
            f"requested {args.revision}",
            file=sys.stderr,
        )
        return 1
    mapping: dict[str, str] = {}
    for item in args.map or ():
        producer_path, separator, local_path = item.partition("=")
        if not separator or not producer_path or not local_path:
            raise UsageError(f"--map expects producer_path=local_path, got {item!r}")
        mapping[producer_path] = local_path

    pins = _load_pins(consumer, required=False)
    previous = pins["contracts"].get(args.contract, {})
    previous_paths = {
        asset["producer_path"]: asset["local_path"] for asset in previous.get("assets", [])
    }
    name = _producer_name(index["producer_repository"])
    vectors_path = entry.get("vectors_path")
    stale: list[str] = []
    assets: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for asset in entry["assets"]:
        producer_path = asset["path"]
        data = producer.read(producer_path)
        if sha256_hex(data) != asset["sha256"] or len(data) != asset["size_bytes"]:
            stale.append(producer_path)
            continue
        local_path = mapping.get(
            producer_path,
            previous_paths.get(
                producer_path, _default_local_path(name, producer_path, vectors_path)
            ),
        )
        payloads[local_path] = data
        assets.append(
            {
                "producer_path": producer_path,
                "local_path": local_path,
                "sha256": asset["sha256"],
                "size_bytes": asset["size_bytes"],
            }
        )
    if stale:
        for producer_path in stale:
            print(
                f"{args.contract}: producer file {producer_path} differs from the producer "
                "revision index; regenerate the producer index first",
                file=sys.stderr,
            )
        return 1
    local_by_producer = {asset["producer_path"]: asset["local_path"] for asset in assets}
    schema_path = entry.get("schema_path")
    if entry.get("revision_carrier") == "schema" and schema_path not in local_by_producer:
        raise UsageError(f"{args.contract}: the schema {schema_path} is not a listed asset")
    if vectors_path is not None and vectors_path not in local_by_producer:
        raise UsageError(f"{args.contract}: the vectors {vectors_path} are not a listed asset")

    for local_path, data in payloads.items():
        target = consumer / local_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for producer_path, local_path in previous_paths.items():
        if local_path not in payloads:
            print(f"{args.contract}: {producer_path} is no longer listed; {local_path} kept")
    pins["contracts"][args.contract] = {
        "producer_repository": index["producer_repository"],
        "revision": entry["revision"],
        "revision_carrier": entry.get("revision_carrier"),
        "wire_sha256": entry.get("wire_sha256"),
        "schema_local_path": local_by_producer.get(schema_path) if schema_path else None,
        "vectors_sha256": entry.get("vectors_sha256"),
        "vectors_local_path": local_by_producer.get(vectors_path) if vectors_path else None,
        "assets": sorted(assets, key=lambda asset: asset["producer_path"]),
    }
    _write_pins(consumer, pins)
    errors = _check_contract(consumer, args.contract, pins["contracts"][args.contract])
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"{args.contract}: pinned revision {entry['revision']} ({len(assets)} files)")
    return 0


def command_check(args: argparse.Namespace) -> int:
    consumer = Path(args.consumer_root).resolve()
    pins = _load_pins(consumer, required=True)
    errors: list[str] = []
    for contract_id, pin in sorted(pins["contracts"].items()):
        errors.extend(_check_contract(consumer, contract_id, pin))
    if args.producer_root:
        errors.extend(_compare_with_producer(pins, _Producer(Path(args.producer_root), None)))
    if args.sibling_copy:
        sibling = Path(args.sibling_copy)
        if not sibling.is_file():
            print(f"SKIP sibling copy comparison: {sibling} is absent")
        elif sibling.read_bytes() != Path(__file__).read_bytes():
            errors.append(f"contract_vendor.py differs from the sibling copy {sibling}")
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"contract pins hold: {len(pins['contracts'])} contracts")
    return 0


def command_wire_fingerprint(args: argparse.Namespace) -> int:
    schema = _load_json(_read_file(Path(args.schema), "schema"), args.schema)
    print(wire_fingerprint(schema))
    return 0


def _parser() -> argparse.ArgumentParser:
    default_root = str(Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="vendor one contract at the producer's revision")
    sync.add_argument("--consumer-root", default=default_root)
    sync.add_argument("--producer-root", required=True)
    sync.add_argument("--producer-ref")
    sync.add_argument("--contract", required=True)
    sync.add_argument("--revision", type=int)
    sync.add_argument("--map", action="append", metavar="PRODUCER_PATH=LOCAL_PATH")
    sync.set_defaults(handler=command_sync)

    check = commands.add_parser("check", help="verify vendored bytes against the pins")
    check.add_argument("--consumer-root", default=default_root)
    check.add_argument("--producer-root")
    check.add_argument("--sibling-copy")
    check.set_defaults(handler=command_check)

    fingerprint = commands.add_parser("wire-fingerprint", help="print a schema's wire fingerprint")
    fingerprint.add_argument("schema")
    fingerprint.set_defaults(handler=command_wire_fingerprint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        return args.handler(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests for scripts/contract_vendor.py; standard library only.

Run with ``python3 -B scripts/tests/test_contract_vendor.py``. The script and
this file are byte-identical copies in every repository that vendors a
cross-repository contract.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "contract_vendor.py"
CONTRACT = "alephain.example.widget.v1"


def _load():
    spec = importlib.util.spec_from_file_location("contract_vendor_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cv = _load()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pretty(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _schema(revision: int = 1, *, extra_property: str = "description") -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Widget",
        "description": "A widget.",
        "x-contract-id": CONTRACT,
        "x-contract-revision": revision,
        "type": "object",
        "additionalProperties": False,
        "required": ["amount"],
        "properties": {
            "amount": {"type": "string", "description": "Canonical decimal."},
            extra_property: {"type": "string", "title": "A property named like a keyword"},
        },
    }


def _vectors(revision: int = 1) -> dict:
    return {
        "vector_set_version": 1,
        "contract_id": CONTRACT,
        "contract_revision": revision,
        "vectors": [{"id": "one", "raw": "{}", "expected": {"outcome": "accept"}}],
    }


class _Producer:
    """A producer checkout with one contract in its revision index."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, schema: dict, vectors: dict, revision: int | None = None) -> None:
        files = {
            "contracts/widget.schema.json": _pretty(schema),
            "contracts/widget-golden.json": _pretty({"amount": "1.5"}),
            "conformance/widget.vectors.json": _pretty(vectors),
        }
        files["conformance/widget.vectors.json.sha256"] = (
            f"{_sha(files['conformance/widget.vectors.json'])}  widget.vectors.json\n"
        ).encode("ascii")
        for relative, data in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        revision = schema["x-contract-revision"] if revision is None else revision
        index = {
            "index_schema_version": 1,
            "producer_repository": "example/producer",
            "canonicalization": "utf8-json-sort-keys-compact-v1",
            "contracts": {
                CONTRACT: {
                    "revision": revision,
                    "revision_carrier": "schema",
                    "schema_path": "contracts/widget.schema.json",
                    "wire_sha256": cv.wire_fingerprint(schema),
                    "assets": [
                        {"path": relative, "sha256": _sha(data), "size_bytes": len(data)}
                        for relative, data in sorted(files.items())
                    ],
                    "vectors_path": "conformance/widget.vectors.json",
                    "vectors_sha256": _sha(files["conformance/widget.vectors.json"]),
                    "history": [],
                }
            },
        }
        path = self.root / cv.PRODUCER_INDEX_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_pretty(index))


def _run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cv.main(list(argv))
    return code, out.getvalue()


class WireFingerprintTest(unittest.TestCase):
    def test_descriptive_keywords_do_not_change_the_fingerprint(self) -> None:
        schema = _schema()
        edited = copy.deepcopy(schema)
        edited["description"] = "Reworded."
        edited["title"] = "Renamed"
        edited["$comment"] = "note"
        edited["examples"] = [{"amount": "1"}]
        edited["x-contract-revision"] = 7
        edited["properties"]["amount"]["description"] = "Reworded too."
        self.assertEqual(cv.wire_fingerprint(schema), cv.wire_fingerprint(edited))

    def test_a_property_named_description_is_part_of_the_wire(self) -> None:
        # A fingerprint that dropped every "description" key would also drop this
        # property, and renaming it would leave the fingerprint unchanged.
        named = _schema(extra_property="description")
        renamed = _schema(extra_property="summary")
        self.assertNotEqual(cv.wire_fingerprint(named), cv.wire_fingerprint(renamed))
        without = copy.deepcopy(named)
        del without["properties"]["description"]
        self.assertNotEqual(cv.wire_fingerprint(named), cv.wire_fingerprint(without))

    def test_data_values_keep_keyword_named_members(self) -> None:
        schema = {"const": {"description": "a"}, "x-invariants": {"title": "kept"}}
        changed = {"const": {"description": "b"}, "x-invariants": {"title": "kept"}}
        self.assertNotEqual(cv.wire_fingerprint(schema), cv.wire_fingerprint(changed))
        renamed_invariant = {"const": {"description": "a"}, "x-invariants": {"title": "x"}}
        self.assertNotEqual(cv.wire_fingerprint(schema), cv.wire_fingerprint(renamed_invariant))

    def test_wire_changes_change_the_fingerprint(self) -> None:
        schema = _schema()
        for mutate in (
            lambda s: s["properties"]["amount"].update(type="number"),
            lambda s: s["required"].append("description"),
            lambda s: s.update(additionalProperties=True),
            lambda s: s["properties"]["amount"].update(pattern="^[0-9]+$"),
        ):
            edited = copy.deepcopy(schema)
            mutate(edited)
            self.assertNotEqual(cv.wire_fingerprint(schema), cv.wire_fingerprint(edited))

    def test_cli_prints_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.json"
            path.write_bytes(_pretty(_schema()))
            code, output = _run("wire-fingerprint", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), cv.wire_fingerprint(_schema()))


class VendorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.producer = _Producer(base / "producer")
        self.producer.write(_schema(), _vectors())
        self.consumer = base / "consumer"
        self.consumer.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sync(self, *extra: str) -> tuple[int, str]:
        return _run(
            "sync",
            "--consumer-root",
            str(self.consumer),
            "--producer-root",
            str(self.producer.root),
            "--contract",
            CONTRACT,
            "--map",
            "contracts/widget.schema.json=docs/authority/producer-widget.schema.json",
            *extra,
        )

    def _check(self, *extra: str) -> tuple[int, str]:
        return _run("check", "--consumer-root", str(self.consumer), *extra)

    def _pins(self) -> dict:
        return json.loads((self.consumer / cv.PINS_PATH).read_text(encoding="utf-8"))

    def test_sync_vendors_every_asset_and_pins_the_revision_without_commits(self) -> None:
        code, output = self._sync()
        self.assertEqual(code, 0, output)
        pin = self._pins()["contracts"][CONTRACT]
        self.assertEqual(pin["revision"], 1)
        self.assertEqual(pin["producer_repository"], "example/producer")
        self.assertEqual(pin["wire_sha256"], cv.wire_fingerprint(_schema()))
        local = {asset["producer_path"]: asset["local_path"] for asset in pin["assets"]}
        self.assertEqual(
            local["contracts/widget.schema.json"], "docs/authority/producer-widget.schema.json"
        )
        self.assertEqual(
            local["conformance/widget.vectors.json"],
            "docs/authority/conformance/producer/widget.vectors.json",
        )
        self.assertEqual(
            local["contracts/widget-golden.json"],
            "docs/authority/vendor/producer/widget-golden.json",
        )
        for asset in pin["assets"]:
            data = (self.consumer / asset["local_path"]).read_bytes()
            self.assertEqual(data, (self.producer.root / asset["producer_path"]).read_bytes())
        self.assertNotRegex(json.dumps(self._pins()), r"\b[0-9a-f]{40}\b")
        self.assertEqual(self._check(), (0, self._check()[1]))

    def test_sync_keeps_the_local_path_a_pin_already_uses(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        code, output = _run(
            "sync",
            "--consumer-root",
            str(self.consumer),
            "--producer-root",
            str(self.producer.root),
            "--contract",
            CONTRACT,
        )
        self.assertEqual(code, 0, output)
        local = {
            asset["producer_path"]: asset["local_path"]
            for asset in self._pins()["contracts"][CONTRACT]["assets"]
        }
        self.assertEqual(
            local["contracts/widget.schema.json"], "docs/authority/producer-widget.schema.json"
        )

    def test_sync_refuses_a_revision_the_producer_is_not_at(self) -> None:
        code, output = self._sync("--revision", "2")
        self.assertEqual(code, 1)
        self.assertIn("producer is at revision 1", output)
        self.assertFalse((self.consumer / cv.PINS_PATH).exists())

    def test_sync_refuses_a_producer_index_that_differs_from_its_files(self) -> None:
        (self.producer.root / "contracts/widget-golden.json").write_bytes(b"{}\n")
        code, output = self._sync()
        self.assertEqual(code, 1)
        self.assertIn("contracts/widget-golden.json", output)

    def test_sync_reads_a_producer_git_ref(self) -> None:
        root = self.producer.root
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        for argv in (
            ["init", "-q"],
            ["add", "."],
            ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "r1"],
            ["tag", "r1"],
        ):
            subprocess.run(["git", "-C", str(root), *argv], check=True)
        self.producer.write(_schema(2, extra_property="summary"), _vectors(2))
        code, output = self._sync("--producer-ref", "r1", "--revision", "1")
        self.assertEqual(code, 0, output)
        self.assertEqual(self._pins()["contracts"][CONTRACT]["revision"], 1)
        self.assertEqual(self._check()[0], 0)

    def test_check_without_pins_is_a_usage_error(self) -> None:
        code, output = self._check()
        self.assertEqual(code, 2)
        self.assertIn(cv.PINS_PATH, output)

    def test_check_refuses_malformed_pins(self) -> None:
        path = self.consumer / cv.PINS_PATH
        path.parent.mkdir(parents=True)
        path.write_text('{"pins_schema_version": 1}\n', encoding="utf-8")
        self.assertEqual(self._check()[0], 2)

    def test_check_names_a_vendored_file_whose_bytes_changed(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        target = self.consumer / "docs/authority/vendor/producer/widget-golden.json"
        data = bytearray(target.read_bytes())
        data[2] ^= 0x01
        target.write_bytes(bytes(data))
        code, output = self._check()
        self.assertEqual(code, 1)
        self.assertIn("docs/authority/vendor/producer/widget-golden.json", output)

    def test_check_refuses_a_schema_revision_other_than_the_pin(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        pins = self._pins()
        pins["contracts"][CONTRACT]["revision"] = 2
        (self.consumer / cv.PINS_PATH).write_bytes(_pretty(pins))
        code, output = self._check()
        self.assertEqual(code, 1)
        self.assertIn("x-contract-revision 1", output)

    def test_check_refuses_a_wire_fingerprint_other_than_the_pin(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        pins = self._pins()
        pins["contracts"][CONTRACT]["wire_sha256"] = "0" * 64
        (self.consumer / cv.PINS_PATH).write_bytes(_pretty(pins))
        code, output = self._check()
        self.assertEqual(code, 1)
        self.assertIn("wire fingerprint", output)

    def test_check_refuses_vectors_that_differ_from_their_sidecar(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        sidecar = self.consumer / "docs/authority/conformance/producer/widget.vectors.json.sha256"
        sidecar.write_text(f"{'0' * 64}  widget.vectors.json\n", encoding="ascii")
        pins = self._pins()
        for asset in pins["contracts"][CONTRACT]["assets"]:
            if asset["local_path"].endswith(".sha256"):
                asset["sha256"] = _sha(sidecar.read_bytes())
                asset["size_bytes"] = len(sidecar.read_bytes())
        (self.consumer / cv.PINS_PATH).write_bytes(_pretty(pins))
        code, output = self._check()
        self.assertEqual(code, 1)
        self.assertIn("sidecar", output)

    def test_check_refuses_vectors_for_another_revision(self) -> None:
        self.producer.write(_schema(), _vectors(revision=3), revision=1)
        code, output = self._sync()
        self.assertEqual(code, 1)
        self.assertIn("contract_revision 3", output)
        code, output = self._check()
        self.assertEqual(code, 1)
        self.assertIn("contract_revision 3", output)

    def test_check_against_the_producer_reports_a_descriptive_change(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        edited = _schema()
        edited["description"] = "Reworded."
        self.producer.write(edited, _vectors())
        code, output = self._check("--producer-root", str(self.producer.root))
        self.assertEqual(code, 1)
        self.assertIn("same revision", output)
        self.assertIn("run sync", output)
        self.assertEqual(self._check()[0], 0)

    def test_check_against_the_producer_reports_a_newer_revision(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        self.producer.write(_schema(2, extra_property="summary"), _vectors(2))
        code, output = self._check("--producer-root", str(self.producer.root))
        self.assertEqual(code, 1)
        self.assertIn("producer is at revision 2", output)

    def test_check_against_an_unchanged_producer_passes(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        code, output = self._check("--producer-root", str(self.producer.root))
        self.assertEqual(code, 0, output)

    def test_sibling_copy_is_skipped_when_absent_and_compared_when_present(self) -> None:
        self.assertEqual(self._sync()[0], 0)
        code, output = self._check("--sibling-copy", str(self.consumer / "missing.py"))
        self.assertEqual(code, 0)
        self.assertIn("SKIP", output)
        copy_path = self.consumer / "copy.py"
        copy_path.write_bytes(SCRIPT.read_bytes())
        self.assertEqual(self._check("--sibling-copy", str(copy_path))[0], 0)
        copy_path.write_bytes(SCRIPT.read_bytes() + b"\n")
        code, output = self._check("--sibling-copy", str(copy_path))
        self.assertEqual(code, 1)
        self.assertIn("differs", output)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)

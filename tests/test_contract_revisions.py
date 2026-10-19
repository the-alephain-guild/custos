"""The contract revision index enforces when a contract revision must be raised."""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = runpy.run_path(
    str(ROOT / "scripts/generate_contract_revisions.py"), run_name="contract_revisions_test"
)
RUNNER_FACT = "alephain.custos.runner_fact_batch.v1"
RUNNER_FACT_SCHEMA = "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json"
RUNNER_FACT_VECTORS = "docs/authority/conformance/runner-fact-batch-v1.vectors.json"
RUNNER_FACT_SEQUENCES = "docs/authority/conformance/runner-fact-batch-v1.sequences.json"
ARTIFACT_REF_SCHEMA = "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    paths = {GENERATOR["INDEX_PATH"]}
    for contract in GENERATOR["CONTRACTS"]:
        paths.update(contract.assets)
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _run(root: Path, *argv: str, capsys) -> tuple[int, str]:
    code = GENERATOR["main"]([*argv, "--root", str(root)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _edit_schema(root: Path, edit) -> None:
    path = root / RUNNER_FACT_SCHEMA
    schema = json.loads(path.read_text(encoding="utf-8"))
    edit(schema)
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _revision(root: Path) -> int:
    return json.loads((root / RUNNER_FACT_SCHEMA).read_text(encoding="utf-8"))[
        "x-contract-revision"
    ]


def _rename_property(schema: dict) -> None:
    properties = schema["properties"]
    properties["batch_identifier"] = properties.pop("batch_id")


def test_the_committed_index_is_exact(root: Path, capsys) -> None:
    assert _run(root, "--check", capsys=capsys) == (0, "contract revision index is exact\n")


def test_a_renamed_property_without_a_raised_revision_fails(root: Path, capsys) -> None:
    _edit_schema(root, _rename_property)
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert (
        f"{RUNNER_FACT}: the wire fingerprint, the conformance vectors or the sequences changed"
        in output
    )
    assert f"still {_revision(root)}" in output
    # Regenerating does not launder the change into the index.
    assert _run(root, capsys=capsys)[0] == 1


def test_a_renamed_property_with_a_raised_revision_is_recorded(root: Path, capsys) -> None:
    current = _revision(root)
    _edit_schema(root, _rename_property)
    _edit_schema(root, lambda schema: schema.update({"x-contract-revision": current + 1}))
    for relative in (RUNNER_FACT_VECTORS, RUNNER_FACT_SEQUENCES):
        _edit_document(
            root, relative, lambda document: document.update(contract_revision=current + 1)
        )
    assert _run(root, "--check", capsys=capsys)[0] == 1
    assert _run(root, capsys=capsys)[0] == 0
    assert _run(root, "--check", capsys=capsys)[0] == 0
    index = json.loads((root / GENERATOR["INDEX_PATH"]).read_text(encoding="utf-8"))
    entry = index["contracts"][RUNNER_FACT]
    assert entry["revision"] == current + 1
    assert [item["revision"] for item in entry["history"]] == list(range(1, current + 2))
    assert entry["history"][-2]["wire_sha256"] != entry["history"][-1]["wire_sha256"]


def test_descriptive_text_alone_keeps_the_revision(root: Path, capsys) -> None:
    def describe(schema: dict) -> None:
        schema["description"] = "Reworded for readers."
        schema["properties"]["batch_id"]["description"] = "Reworded too."

    current = _revision(root)
    _edit_schema(root, describe)
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert "regenerate" in output and "revision rule failed" not in output
    assert _run(root, capsys=capsys)[0] == 0
    assert _run(root, "--check", capsys=capsys)[0] == 0
    index = json.loads((root / GENERATOR["INDEX_PATH"]).read_text(encoding="utf-8"))
    assert index["contracts"][RUNNER_FACT]["revision"] == current


def _edit_document(root: Path, relative: str, edit) -> None:
    path = root / relative
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_changed_vectors_without_a_raised_revision_fail(root: Path, capsys) -> None:
    _edit_document(
        root,
        RUNNER_FACT_VECTORS,
        lambda vectors: vectors["vectors"][0]["expected"].update(typed_roundtrip_equal=False),
    )
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert "the conformance vectors or the sequences changed" in output


def test_one_changed_sequence_expectation_without_a_raised_revision_fails(
    root: Path, capsys
) -> None:
    def edit(document: dict) -> None:
        step = document["sequences"][0]["steps"][-1]["expected"]
        step["order_state"] = "canceled"

    _edit_document(root, RUNNER_FACT_SEQUENCES, edit)
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert "the conformance vectors or the sequences changed" in output
    assert f"still {_revision(root)}" in output
    # Regenerating does not launder the change into the index either.
    assert _run(root, capsys=capsys)[0] == 1


def test_sequences_naming_another_revision_fail(root: Path, capsys) -> None:
    raised = _revision(root) + 1
    _edit_document(
        root, RUNNER_FACT_SEQUENCES, lambda document: document.update(contract_revision=raised)
    )
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert f"{RUNNER_FACT_SEQUENCES} names {RUNNER_FACT} revision {raised}" in output


def _set_artifact_ref_revision(root: Path, revision_from_current) -> int:
    path = root / ARTIFACT_REF_SCHEMA
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["x-contract-revision"] = revision_from_current(schema["x-contract-revision"])
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    return schema["x-contract-revision"]


def test_a_raised_revision_without_a_change_only_warns(root: Path, capsys) -> None:
    raised = _set_artifact_ref_revision(root, lambda current: current + 1)
    code, output = _run(root, capsys=capsys)
    assert code == 0
    assert (
        f"warning: alephain.custos.strategy_artifact_ref.v1: revision {raised} changes neither"
        in output
    )


@pytest.mark.parametrize(
    "revision_from_current",
    [lambda current: 0, lambda current: current + 2],
    ids=["lowered", "skipped"],
)
def test_a_skipped_or_lowered_revision_fails(root: Path, revision_from_current, capsys) -> None:
    _set_artifact_ref_revision(root, revision_from_current)
    code, output = _run(root, capsys=capsys)
    assert code == 1
    assert "alephain.custos.strategy_artifact_ref.v1: revision" in output


def test_vectors_naming_another_revision_fail(root: Path, capsys) -> None:
    raised = _revision(root) + 1
    _edit_schema(root, lambda schema: schema.update({"x-contract-revision": raised}))
    code, output = _run(root, "--check", capsys=capsys)
    assert code == 1
    assert f"not revision {raised}" in output

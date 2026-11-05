"""Publisher schemas must accept the actual layer graph emitted by the owner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("contract", ["artifact_ref", "publication_receipt"])
def test_current_publisher_schema_accepts_its_real_graph_and_rejects_wrong_layer_counts(contract):
    program = """
import copy, sys
from pathlib import Path
from jsonschema import Draft202012Validator
root = Path(sys.argv[1]) / 'packages/custos-strategy-publisher'
sys.path[:0] = [str(root / 'src'), str(root / 'tests')]
from test_strategy_artifact_oci_publication_v1 import MemoryRegistryV1, _candidate
from custos_strategy_publisher.oci_publication import (
    publication_receipt_v1_json_schema, publish_verified_candidate,
    strategy_oci_artifact_ref_v1_json_schema,
)
bundle = publish_verified_candidate(_candidate(), MemoryRegistryV1())
if sys.argv[2] == 'artifact_ref':
    value = bundle.artifact_ref.to_mapping()
    schema = strategy_oci_artifact_ref_v1_json_schema()
    def layers(item): return item['layers']
else:
    value = bundle.receipt.to_mapping()
    schema = publication_receipt_v1_json_schema()
    def layers(item): return item['artifact_ref']['layers']
Draft202012Validator.check_schema(schema)
validator = Draft202012Validator(schema)
validator.validate(value)
for extra, expected in [(False, 'minItems'), (True, 'maxItems')]:
    changed = copy.deepcopy(value)
    actual = layers(changed)
    if extra: actual.append(copy.deepcopy(actual[-1]))
    else: actual.pop()
    failures = list(validator.iter_errors(changed))
    assert any(error.validator == expected for error in failures), failures
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(ROOT), contract],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

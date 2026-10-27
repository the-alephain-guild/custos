import hashlib
import json
import os
import subprocess
import sys
import zipfile
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
from custos_toolkit.contracts.strategy_execution import (
    LocalStrategyExecutionContextV1,
    StrategyExecutionContextV1,
    StrategyLocalContextSupportV1,
    canonical_json_bytes,
)
from pydantic import ValidationError

from custos.engines.nautilus.runtime_loader import (
    NautilusRuntimeEntryPointError,
    NautilusRuntimeEntryPointLoaderV1,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "local_strategy_execution_context_v1.schema.json"


def local_context(**changes):
    value = dict(
        control_source="local",
        engine="nautilus",
        trading_mode="live",
        run_id=UUID("11000000-0000-4000-8000-000000000001"),
        run_manifest_digest="a" * 64,
        account_scope="b" * 64,
        effective_config_digest="c" * 64,
        configuration_revision=1,
        authorization_epoch=1,
        process_generation=2,
    )
    value.update(changes)
    return LocalStrategyExecutionContextV1.model_validate(value)


def test_local_provenance_contains_no_platform_identity_or_generation_alias():
    context = local_context()
    fields = context.model_dump(mode="json")
    assert context.authorization_epoch == 1 and context.process_generation == 2
    assert (
        not {"tenant_id", "deployment_instance_id", "deployment_spec_id", "generation"}
        & fields.keys()
    )
    assert set(StrategyExecutionContextV1.model_fields) == {
        "schema_version",
        "engine",
        "trading_mode",
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "effective_config_digest",
        "generation",
    }
    with pytest.raises(ValidationError):
        context.process_generation = 3


@pytest.mark.parametrize(
    "field", ["configuration_revision", "authorization_epoch", "process_generation"]
)
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", 2**63])
def test_local_fences_are_strict_positive_durable_integers(field, value):
    with pytest.raises(ValidationError):
        local_context(**{field: value})


@pytest.mark.parametrize(
    "extra", ["deployment_instance_id", "deployment_spec_id", "generation", "tenant_id"]
)
def test_platform_fields_are_rejected_in_local_context(extra):
    with pytest.raises(ValidationError):
        local_context(**{extra: "not-local"})


def test_nil_local_run_is_rejected():
    with pytest.raises(ValidationError):
        local_context(run_id=UUID(int=0))


def test_local_context_is_not_import_authority_without_verified_compatibility(tmp_path):
    marker = tmp_path / "imported.txt"
    (tmp_path / "local_context_probe.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "class Runtime: pass\n"
    )
    with pytest.raises(NautilusRuntimeEntryPointError, match="verified local compatibility"):
        NautilusRuntimeEntryPointLoaderV1().load(
            activation_root=tmp_path,
            entry_point="local_context_probe:Runtime",
            effective_config={},
            execution_context=local_context(),
        )
    assert not marker.exists()


def test_generated_local_assets_bind_the_same_model_and_canonical_schema():
    schema_path = ROOT / "docs/gateway-contract/v1" / SCHEMA
    schema = schema_path.read_bytes()
    assert schema == canonical_json_bytes(
        LocalStrategyExecutionContextV1.model_json_schema(mode="validation")
    )
    packaged = files("custos_toolkit.contracts").joinpath("assets", SCHEMA).read_bytes()
    assert packaged == schema
    jsonschema.Draft202012Validator(
        json.loads(schema), format_checker=jsonschema.FormatChecker()
    ).validate(local_context().model_dump(mode="json"))
    support = StrategyLocalContextSupportV1.model_validate_json(
        (ROOT / "docs/authority/local-strategy-context-support-v1.golden.json").read_bytes()
    )
    assert support.context_schema_id == json.loads(schema)["$id"]
    assert support.context_schema_sha256 == hashlib.sha256(schema).hexdigest()


@pytest.mark.parametrize(
    "vector",
    json.loads(
        (ROOT / "docs/authority/local-strategy-execution-context-v1.negative.json").read_bytes()
    )["vectors"],
    ids=lambda vector: vector["name"],
)
def test_published_negative_vectors_are_rejected_by_model_and_schema(vector):
    value = local_context().model_dump(mode="json")
    value.update(vector["changes"])
    with pytest.raises(ValidationError):
        LocalStrategyExecutionContextV1.model_validate(value)
    schema = json.loads((ROOT / "docs/gateway-contract/v1" / SCHEMA).read_bytes())
    assert not jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).is_valid(value)


def test_the_base_wheel_contains_the_canonical_local_context_schema(tmp_path):
    from tests.test_toolkit_distribution import _build_wheel

    wheel = _build_wheel("custos-strategy-toolkit", tmp_path / "base")
    with zipfile.ZipFile(wheel) as archive:
        schema = archive.read(f"custos_toolkit/contracts/assets/{SCHEMA}")
    assert schema == (ROOT / "docs/gateway-contract/v1" / SCHEMA).read_bytes()


def test_existing_platform_adapters_still_satisfy_the_original_protocol(tmp_path):
    probe = tmp_path / "platform_adapter_typing.py"
    probe.write_text("""from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject, StrategyExecutionContextV1, StrategyRuntimeAdapterV1,
    LocalStrategyExecutionContextV1, LocalStrategyRuntimeAdapterV1,
    StrategyRuntimeExecutionContextV1,
)

class PlatformAdapter:
    def build_config(self, effective_config: FrozenJsonObject,
                     execution_context: StrategyExecutionContextV1) -> int:
        return execution_context.generation
    def build_strategy(self, config: int) -> str:
        return str(config)

adapter: StrategyRuntimeAdapterV1[int, str] = PlatformAdapter()

class LocalAdapter:
    def build_config(self, effective_config: FrozenJsonObject,
                     execution_context: LocalStrategyExecutionContextV1) -> int:
        return execution_context.process_generation
    def build_strategy(self, config: int) -> str:
        return str(config)

local_adapter: LocalStrategyRuntimeAdapterV1[int, str] = LocalAdapter()

class DualAdapter:
    def build_config(self, effective_config: FrozenJsonObject,
                     execution_context: StrategyRuntimeExecutionContextV1) -> int:
        return 1
    def build_strategy(self, config: int) -> str:
        return str(config)

dual_platform: StrategyRuntimeAdapterV1[int, str] = DualAdapter()
dual_local: LocalStrategyRuntimeAdapterV1[int, str] = DualAdapter()
""")
    environment = os.environ.copy()
    environment["MYPYPATH"] = str(ROOT / "packages/custos-strategy-toolkit/src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "mypy"),
            str(probe),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""Build-selected engine contracts stay exact and isolated from released imports."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _candidate_tree(tmp_path):
    from scripts.native_candidate_profile import render_engine_definitions

    for relative in (
        "packages/custos-strategy-toolkit/src/custos_toolkit",
        "packages/custos-strategy-publisher/src/custos_strategy_publisher",
    ):
        shutil.copytree(
            ROOT / relative, tmp_path / relative, ignore=shutil.ignore_patterns("__pycache__")
        )
    for relative, content in render_engine_definitions("native-capture-darwin7-v1").items():
        (tmp_path / relative).write_bytes(content)


def test_rendered_candidate_uses_existing_v1_models_without_accepting_released_engine(tmp_path):
    _candidate_tree(tmp_path)
    program = """
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path[:0]=[str(root/'packages/custos-strategy-toolkit/src'),str(root/'packages/custos-strategy-publisher/src')]
from pydantic import ValidationError
from custos_toolkit.contracts.strategy_execution import StrategyManifestV1,StrategyArtifactRefV1
from custos_strategy_publisher.model import ENGINE_VERSION
manifest={'execution_abi':'alephain.strategy_runtime.v1','entry_point_group':'alephain.strategy_runtime.v1','entry_point':'fixture:Runtime','engine':'nautilus','engine_version':'2.0.0rc5+papertrade.7','requires_python':'>=3.12,<3.13','base_contracts_version':'1.0.0rc1','engine_toolkit_version':'1.0.0rc1','config_schema_sha256':'a'*64,'trading_scope':{'connector':'binance','pairs':['BTC/USDT'],'leverage':1}}
accepted=StrategyManifestV1.model_validate(manifest)
assert accepted.engine_version==ENGINE_VERSION=='2.0.0rc5+papertrade.7'
assert accepted.base_contracts_version==accepted.engine_toolkit_version=='1.0.0rc1'
for version in ['2.0.0rc5+sodex.3','arbitrary','2.0.0rc5+papertrade.6']:
    try: StrategyManifestV1.model_validate({**manifest,'engine_version':version})
    except ValidationError as error: assert any(e['loc']==('engine_version',) for e in error.errors())
    else: raise AssertionError(version)
for model in [StrategyManifestV1,StrategyArtifactRefV1]:
    assert model.model_json_schema()['properties']['engine_version']['const']==ENGINE_VERSION
print(json.dumps({'engine_version':ENGINE_VERSION,'base_contracts_version':accepted.base_contracts_version}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "engine_version": "2.0.0rc5+papertrade.7",
        "base_contracts_version": "1.0.0rc1",
    }
    from custos_toolkit.contracts.strategy_execution import StrategyManifestV1

    assert StrategyManifestV1.model_json_schema()["properties"]["engine_version"]["const"] == (
        "2.0.0rc5+sodex.3"
    )


def test_candidate_owned_assets_preserve_fixture_versions_without_rewriting_peer_goldens(tmp_path):
    _candidate_tree(tmp_path)
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts/generate_strategy_contract_assets.py",
        tmp_path / "scripts/generate_strategy_contract_assets.py",
    )
    program = """
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path[:0]=[str(root/'packages/custos-strategy-toolkit/src'),str(root)]
from scripts.generate_strategy_contract_assets import build_standalone_contract_assets
assets=build_standalone_contract_assets()
ref=json.loads(assets['docs/authority/strategy-artifact-ref-v1.golden.json'])['artifact_ref']
assert ref['engine_version']=='2.0.0rc5+papertrade.7'
assert ref['base_contracts_version']==ref['engine_toolkit_version']=='1.0.0rc1'
for key in ['strategy_manifest_v1.schema.json','strategy_artifact_ref_v1.schema.json']:
    assert json.loads(assets['docs/gateway-contract/v1/'+key])['properties']['engine_version']['const']=='2.0.0rc5+papertrade.7'
assert not any('/receipts/' in key or '/vendor/' in key for key in assets)
assert 'docs/authority/strategy-artifact-pre-import-verification-v1.golden.json' not in assets
assert not (root/'docs').exists()
print(len(assets))
"""
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )


def test_standalone_default_assets_are_identical_to_current_owner_outputs():
    from scripts.generate_strategy_contract_assets import (
        build_standalone_contract_assets,
        build_v1_contract_assets,
    )

    current = build_v1_contract_assets()
    for relative, content in build_standalone_contract_assets().items():
        assert content == current[relative], relative


@pytest.mark.parametrize(
    "profile",
    [None, {}, [], "", "2.0.0rc5+papertrade.7", "native-capture-darwin7-v2", "../released"],
)
def test_unknown_build_profiles_have_no_version_or_path_fallback(profile):
    from scripts.native_candidate_profile import render_engine_definitions

    with pytest.raises(ValueError, match="unknown engine build profile"):
        render_engine_definitions(profile)


def test_released_definitions_are_reproducible_and_ignore_runtime_environment(monkeypatch):
    from scripts.native_candidate_profile import render_engine_definitions

    monkeypatch.setenv("CUSTOS_ENGINE_VERSION", "2.0.0rc5+papertrade.7")
    monkeypatch.setenv("CUSTOS_RUNTIME_PROFILE", "native-capture-darwin7-v1")
    for relative, content in render_engine_definitions("released-sodex3-v1").items():
        assert (ROOT / relative).read_bytes() == content

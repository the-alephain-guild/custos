from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest
from custos_toolkit.contracts.strategy_execution import (
    StrategyExecutionContextV1,
    deep_freeze_json,
)

from custos.engines.nautilus.runtime_loader import (
    NautilusRuntimeEntryPointError,
    NautilusRuntimeEntryPointLoaderV1,
)


def _context() -> StrategyExecutionContextV1:
    return StrategyExecutionContextV1(
        engine="nautilus",
        trading_mode="sandbox",
        deployment_instance_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002"),
        deployment_spec_digest="d" * 64,
        effective_config_digest="e" * 64,
        generation=1,
    )


def test_loader_builds_strategy_only_through_runtime_adapter_v1(tmp_path: Path) -> None:
    package = tmp_path / "team_runtime"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "adapter.py").write_text(
        """
class Runtime:
    def build_config(self, effective_config, execution_context):
        return {"period": effective_config["period"], "generation": execution_context.generation}

    def build_strategy(self, config):
        return ("verified-strategy", config)
""".lstrip(),
        encoding="utf-8",
    )

    strategy = NautilusRuntimeEntryPointLoaderV1().load(
        activation_root=tmp_path,
        entry_point="team_runtime.adapter:Runtime",
        effective_config=deep_freeze_json({"period": 20}),
        execution_context=_context(),
    )

    assert strategy == ("verified-strategy", {"period": 20, "generation": 1})


def test_loader_replaces_module_cached_from_another_activation(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "collision.py").write_text(
        """
class Runtime:
    def build_config(self, effective_config, execution_context):
        return "outside"
    def build_strategy(self, config):
        return config
""".lstrip(),
        encoding="utf-8",
    )
    sys.path.insert(0, str(outside))
    try:
        __import__("collision")
    finally:
        sys.path.remove(str(outside))

    activation = tmp_path / "activation"
    activation.mkdir()
    (activation / "collision.py").write_text(
        """
class Runtime:
    def build_config(self, effective_config, execution_context):
        return "current-activation"
    def build_strategy(self, config):
        return config
""".lstrip(),
        encoding="utf-8",
    )
    try:
        strategy = NautilusRuntimeEntryPointLoaderV1().load(
            activation_root=activation,
            entry_point="collision:Runtime",
            effective_config=deep_freeze_json({}),
            execution_context=_context(),
        )
        assert strategy == "current-activation"
        assert Path(sys.modules["collision"].__file__).is_relative_to(activation)
    finally:
        sys.modules.pop("collision", None)


def test_loader_rejects_legacy_factory_shape(tmp_path: Path) -> None:
    (tmp_path / "legacy.py").write_text(
        "def create_strategy(config):\n    return object()\n",
        encoding="utf-8",
    )

    with pytest.raises(NautilusRuntimeEntryPointError, match="StrategyRuntimeAdapterV1"):
        NautilusRuntimeEntryPointLoaderV1().load(
            activation_root=tmp_path,
            entry_point="legacy:create_strategy",
            effective_config=deep_freeze_json({}),
            execution_context=_context(),
        )
    sys.modules.pop("legacy", None)

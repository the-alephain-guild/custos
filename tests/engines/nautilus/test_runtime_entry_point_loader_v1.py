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


_SAME_NAME = "c1-same-name-strategy"


def _write_activation(root: Path, *, marker: str) -> Path:
    """One verified activation: the same package and registered name as every
    other activation built by this helper, with its own strategy source."""

    package = root / "strategy_same_name"
    nautilus = package / "refinement" / "nautilus"
    nautilus.mkdir(parents=True)
    for directory in (package, package / "refinement", nautilus):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (nautilus / "strategy.py").write_text(
        f"""
from custos_toolkit_nautilus.adapter.registry import register_strategy

MARKER = {marker!r}


class Strategy:
    def __init__(self, config):
        self.config = config
        self.marker = MARKER


class Config:
    pass


def build_parameters(wrapper):
    return MARKER


register_strategy({_SAME_NAME!r}, Strategy, Config, build_parameters)
""".lstrip(),
        encoding="utf-8",
    )
    (package / "runtime.py").write_text(
        f"""
from custos_toolkit_nautilus.adapter.registry import create_strategy

from .refinement.nautilus import strategy as _registration


class Runtime:
    def build_config(self, effective_config, execution_context):
        return _registration.Config()

    def build_strategy(self, config):
        return create_strategy({_SAME_NAME!r}, config=config)
""".lstrip(),
        encoding="utf-8",
    )
    return root


def _load_same_name(root: Path) -> object:
    return NautilusRuntimeEntryPointLoaderV1().load(
        activation_root=root,
        entry_point="strategy_same_name.runtime:Runtime",
        effective_config=deep_freeze_json({}),
        execution_context=_context(),
    )


def _forget_same_name() -> None:
    from custos_toolkit_nautilus.adapter import registry

    registry._STRATEGY_REGISTRY.pop(_SAME_NAME, None)
    for name in tuple(sys.modules):
        if name == "strategy_same_name" or name.startswith("strategy_same_name."):
            sys.modules.pop(name, None)


def test_one_process_activates_two_sources_that_register_one_name(tmp_path: Path) -> None:
    pytest.importorskip("custos_toolkit_nautilus")
    first = _write_activation(tmp_path / "first", marker="first")
    second = _write_activation(tmp_path / "second", marker="second")
    try:
        assert _load_same_name(first).marker == "first"
        assert _load_same_name(second).marker == "second"
        # The first activation's provider rebuilds its strategy on an engine
        # restart, after the second activation has loaded.
        assert _load_same_name(first).marker == "first"

        from custos_toolkit_nautilus.adapter import registry

        assert _SAME_NAME not in registry._STRATEGY_REGISTRY
    finally:
        _forget_same_name()


def test_a_released_activation_does_not_block_its_successor(tmp_path: Path) -> None:
    pytest.importorskip("custos_toolkit_nautilus")
    first = _write_activation(tmp_path / "first", marker="first")
    second = _write_activation(tmp_path / "second", marker="second")
    try:
        released = _load_same_name(first)
        del released

        assert _load_same_name(second).marker == "second"
    finally:
        _forget_same_name()

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit_nautilus.adapter.registry import (  # noqa: E402
    get_strategy_info,
    register_strategy,
    unregister_strategy,
)


def _load_registration_module(path: Path, *, marker: str) -> ModuleType:
    path.write_text(
        f"""
class Strategy:
    marker = {marker!r}

class Config:
    pass

def build_parameters(config):
    return {marker!r}
""".lstrip(),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("verified_strategy_registration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_source_registration_is_idempotent_across_immutable_roots(
    tmp_path: Path,
) -> None:
    name = "exact-source-reload"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _load_registration_module(first_root / "strategy.py", marker="same")
    register_strategy(name, first.Strategy, first.Config, first.build_parameters)
    second = _load_registration_module(second_root / "strategy.py", marker="same")
    try:
        register_strategy(name, second.Strategy, second.Config, second.build_parameters)
        assert get_strategy_info(name)["strategy_class"] is second.Strategy
    finally:
        unregister_strategy(name)
        sys.modules.pop("verified_strategy_registration", None)


def test_different_source_registration_remains_fail_closed(tmp_path: Path) -> None:
    name = "different-source-reload"
    first = _load_registration_module(tmp_path / "first.py", marker="first")
    register_strategy(name, first.Strategy, first.Config, first.build_parameters)
    second = _load_registration_module(tmp_path / "second.py", marker="second")
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_strategy(name, second.Strategy, second.Config, second.build_parameters)
    finally:
        unregister_strategy(name)
        sys.modules.pop("verified_strategy_registration", None)


def test_a_scope_registers_a_name_the_process_registry_holds_from_another_source(
    tmp_path: Path,
) -> None:
    from custos_toolkit_nautilus.adapter.registry import (
        is_registered,
        strategy_registration_scope,
    )

    name = "scoped-shadow"
    outside = _load_registration_module(tmp_path / "outside.py", marker="outside")
    register_strategy(name, outside.Strategy, outside.Config, outside.build_parameters)
    inside = _load_registration_module(tmp_path / "inside.py", marker="inside")
    try:
        with strategy_registration_scope():
            assert not is_registered(name)
            register_strategy(name, inside.Strategy, inside.Config, inside.build_parameters)
            assert get_strategy_info(name)["strategy_class"] is inside.Strategy
        assert get_strategy_info(name)["strategy_class"] is outside.Strategy
    finally:
        unregister_strategy(name)
        sys.modules.pop("verified_strategy_registration", None)


def test_a_scope_still_refuses_one_name_from_two_sources(tmp_path: Path) -> None:
    from custos_toolkit_nautilus.adapter.registry import strategy_registration_scope

    name = "scoped-different-source"
    first = _load_registration_module(tmp_path / "first.py", marker="first")
    second = _load_registration_module(tmp_path / "second.py", marker="second")
    try:
        with strategy_registration_scope():
            register_strategy(name, first.Strategy, first.Config, first.build_parameters)
            with pytest.raises(ValueError, match="already registered"):
                register_strategy(name, second.Strategy, second.Config, second.build_parameters)
    finally:
        sys.modules.pop("verified_strategy_registration", None)


def test_a_scope_that_raises_leaves_nothing_in_the_process_registry(tmp_path: Path) -> None:
    from custos_toolkit_nautilus.adapter import registry

    name = "scoped-then-raised"
    module = _load_registration_module(tmp_path / "strategy.py", marker="raised")
    try:
        with pytest.raises(RuntimeError, match="fixture failure"):
            with registry.strategy_registration_scope():
                register_strategy(name, module.Strategy, module.Config, module.build_parameters)
                raise RuntimeError("fixture failure")
        assert name not in registry._STRATEGY_REGISTRY
        assert registry._SCOPED_REGISTRY.get() is None
    finally:
        sys.modules.pop("verified_strategy_registration", None)


def test_a_scope_never_triggers_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from custos_toolkit_nautilus.adapter import registry

    def discovery_is_forbidden() -> int:
        raise AssertionError("discovery must not run inside a registration scope")

    monkeypatch.setattr(registry, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(registry, "discover_strategies", discovery_is_forbidden)
    with registry.strategy_registration_scope():
        assert registry.list_strategies() == []
        assert registry.is_registered("anything") is False
        with pytest.raises(ValueError, match="Unknown strategy"):
            registry.create_strategy("anything", config=object())  # type: ignore[arg-type]


def test_registration_scopes_do_not_nest() -> None:
    from custos_toolkit_nautilus.adapter.registry import strategy_registration_scope

    with strategy_registration_scope():
        with pytest.raises(RuntimeError, match="cannot be nested"):
            with strategy_registration_scope():
                pass

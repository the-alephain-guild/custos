from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from custos_toolkit_nautilus.adapter.registry import (
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

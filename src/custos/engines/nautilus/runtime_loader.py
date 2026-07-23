"""Sole first-production V1 loader for verified Nautilus strategy artifacts."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from pathlib import Path

from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject,
    StrategyExecutionContextV1,
)


class NautilusRuntimeEntryPointError(RuntimeError):
    """A verified artifact could not satisfy the Nautilus runtime ABI."""


class NautilusRuntimeEntryPointLoaderV1:
    """Load one verified adapter without any source-path compatibility fallback.

    The archive verifier has already authenticated and inspected the entry-point
    declaration.  This loader still proves that Python resolved the module from
    the immutable activation root and rejects cross-activation module reuse.
    """

    def load(
        self,
        *,
        activation_root: Path,
        entry_point: str,
        effective_config: FrozenJsonObject,
        execution_context: StrategyExecutionContextV1,
    ) -> object:
        module_name, separator, attribute_name = entry_point.partition(":")
        if not separator or not module_name or not attribute_name or ":" in attribute_name:
            raise NautilusRuntimeEntryPointError(
                "verified entry point must have exact module:attribute form"
            )
        root = activation_root.resolve(strict=True)
        original_path = tuple(sys.path)
        top_level_package = module_name.partition(".")[0]
        previous_modules = {
            name: module
            for name, module in tuple(sys.modules.items())
            if name == top_level_package or name.startswith(f"{top_level_package}.")
        }
        for name in previous_modules:
            sys.modules.pop(name, None)
        loaded = False
        try:
            sys.path.insert(0, str(root))
            importlib.invalidate_caches()
            module = importlib.import_module(module_name)
            module_file = getattr(module, "__file__", None)
            if module_file is None:
                raise NautilusRuntimeEntryPointError("runtime adapter module has no file origin")
            try:
                Path(module_file).resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as error:
                raise NautilusRuntimeEntryPointError(
                    "runtime adapter module did not resolve from the immutable activation"
                ) from error

            try:
                exported = getattr(module, attribute_name)
            except AttributeError as error:
                raise NautilusRuntimeEntryPointError(
                    "verified runtime adapter attribute is absent"
                ) from error
            adapter = exported() if isinstance(exported, type) else exported
            build_config = getattr(adapter, "build_config", None)
            build_strategy = getattr(adapter, "build_strategy", None)
            if not callable(build_config) or not callable(build_strategy):
                raise NautilusRuntimeEntryPointError(
                    "runtime entry point does not implement StrategyRuntimeAdapterV1"
                )
            if not isinstance(effective_config, Mapping):
                raise NautilusRuntimeEntryPointError("effective strategy config is not an object")
            config = build_config(effective_config, execution_context)
            strategy = build_strategy(config)
            if strategy is None:
                raise NautilusRuntimeEntryPointError("runtime adapter returned no strategy")
            loaded = True
            return strategy
        finally:
            sys.path[:] = original_path
            if not loaded:
                for name in tuple(sys.modules):
                    if name == top_level_package or name.startswith(f"{top_level_package}."):
                        sys.modules.pop(name, None)
                sys.modules.update(previous_modules)


__all__ = [
    "NautilusRuntimeEntryPointError",
    "NautilusRuntimeEntryPointLoaderV1",
]

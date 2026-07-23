"""
NautilusTrader strategy registry.

Provides a generic factory pattern for creating strategies by name.
Strategies register themselves and can be instantiated via:
- Pre-built config object (for Speculum backtesting)
- Config file path (for runner)
- ConfigWrapper (for programmatic parameter sweeps)

Auto-discovery:
When this module is imported, it automatically scans for strategy modules
in known locations and imports them to trigger registration.
"""

import logging
import os
import sys
from collections.abc import Callable
from hashlib import sha256
from inspect import getsourcefile
from pathlib import Path
from typing import Protocol, Unpack, cast

from custos_toolkit.config import ConfigWrapper, load_config

from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore
from custos_toolkit_nautilus.adapter.trading_config import (
    NautilusBaseConfigSections,
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)

# Registry: strategy_name -> (StrategyClass, ConfigClass, parameters_builder)
# Note: Uses NautilusStrategyCore (minimal interface) to support both:
#   - NautilusBaseStrategy subclasses (feature-rich)
#   - Custom strategies extending NautilusStrategyCore directly (maximum flexibility)
_STRATEGY_REGISTRY: dict[
    str,
    tuple[
        type[NautilusStrategyCore],
        type[NautilusTradingStrategyConfig],
        Callable[[ConfigWrapper], object],  # parameters_builder
        tuple[str, str, str],
    ],
] = {}


class _RegisteredConfigFactory(Protocol):
    def __call__(
        self,
        *,
        parameters: object,
        **base_sections: Unpack[NautilusBaseConfigSections],
    ) -> NautilusTradingStrategyConfig: ...


logger = logging.getLogger(__name__)

# Track discovered paths to avoid duplicate discovery
_DISCOVERED_PATHS: set[str] = set()
_DISCOVERY_DONE: bool = False

# Discovery paths - checked in order
DISCOVERY_PATHS = [
    Path("/app/strategy"),  # Docker (Dockerfile.image): strategy at /app/strategy/
    Path("/app/scripts"),  # Docker (docker-compose): strategy at /app/scripts/
    Path(__file__).parent.parent.parent,  # Local: philosophers-stone root
]
# engine+inject mode: Crucible sets STRATEGY_INJECT_PATH to the extracted location
_inject_path = os.environ.get("STRATEGY_INJECT_PATH")
if _inject_path:
    DISCOVERY_PATHS.insert(0, Path(_inject_path))

STRATEGY_CATEGORIES = ["trend", "market_making", "arbitrage", "momentum", "portfolio"]


def _import_strategy_module(module_path: Path, base_path: Path) -> bool:
    """Dynamically import a strategy module to trigger registration.

    Args:
        module_path: Full path to strategy.py file
        base_path: Base path that should be in sys.path
    """
    try:
        # Ensure base_path is in sys.path for relative imports to work
        base_str = str(base_path)
        if base_str not in sys.path:
            sys.path.insert(0, base_str)

        # Compute module name from path relative to base
        # e.g., trend/supertrend/refinement/nautilus/strategy.py
        #    -> trend.supertrend.refinement.nautilus.strategy
        rel_path = module_path.relative_to(base_path)
        module_name = str(rel_path.with_suffix("")).replace("/", ".").replace("\\", ".")

        # Use importlib.import_module for proper package handling
        import importlib

        importlib.import_module(module_name)
        logger.debug(f"Imported strategy module: {module_name}")
        return True
    except Exception as e:
        logger.warning(f"Failed to import strategy from {module_path}: {e}")
        return False


def _discover_from_path(base_path: Path) -> int:
    """Discover strategies from a base path."""
    if not base_path.exists():
        return 0

    path_key = str(base_path.resolve())
    if path_key in _DISCOVERED_PATHS:
        return 0
    _DISCOVERED_PATHS.add(path_key)

    discovered = 0

    # Pattern 1: Flat structure (Dockerfile.image production)
    # /app/strategy/strategy.py
    flat_path = base_path / "strategy.py"
    if flat_path.exists():
        if _import_strategy_module(flat_path, base_path):
            discovered += 1

    # Pattern 2: Nested structure (legacy)
    # /app/strategy/refinement/nautilus/strategy.py
    docker_path = base_path / "refinement" / "nautilus" / "strategy.py"
    if docker_path.exists():
        if _import_strategy_module(docker_path, base_path):
            discovered += 1

    # Pattern 3: Scripts structure (docker-compose)
    # /app/scripts/{strategy_name}/strategy.py
    if base_path.exists() and base_path.is_dir():
        for strategy_dir in base_path.iterdir():
            if not strategy_dir.is_dir():
                continue
            # Skip category directories (handled in Pattern 4)
            if strategy_dir.name in STRATEGY_CATEGORIES:
                continue
            strategy_path = strategy_dir / "strategy.py"
            if strategy_path.exists():
                if _import_strategy_module(strategy_path, base_path):
                    discovered += 1

    # Pattern 4: Category structure (local development)
    # {base}/{category}/{name}/refinement/nautilus/strategy.py
    for category in STRATEGY_CATEGORIES:
        category_path = base_path / category
        if not category_path.exists():
            continue
        for strategy_dir in category_path.iterdir():
            if not strategy_dir.is_dir():
                continue
            strategy_path = strategy_dir / "refinement" / "nautilus" / "strategy.py"
            if strategy_path.exists():
                if _import_strategy_module(strategy_path, base_path):
                    discovered += 1

    return discovered


def discover_strategies() -> int:
    """Discover and import all strategy modules from known paths.

    This function is called lazily on first create_strategy() call to avoid
    circular imports during module initialization.
    """
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return 0
    _DISCOVERY_DONE = True

    total = 0
    for path in DISCOVERY_PATHS:
        total += _discover_from_path(path)
    if total > 0:
        logger.info(f"Discovered {total} strategies: {list(_STRATEGY_REGISTRY.keys())}")
    return total


def _ensure_discovery() -> None:
    """Ensure strategies have been discovered. Called lazily."""
    if not _DISCOVERY_DONE:
        discover_strategies()


def register_strategy(
    name: str,
    strategy_class: type[NautilusStrategyCore],
    config_class: type[NautilusTradingStrategyConfig],
    parameters_builder: Callable[[ConfigWrapper], object],
) -> None:
    """
    Register a strategy for factory creation.

    Args:
        name: Unique strategy name (e.g., "supertrend")
        strategy_class: Strategy implementation class
        config_class: Strategy configuration class
        parameters_builder: Function to build strategy-specific parameters
                           from ConfigWrapper

    Example:
        register_strategy(
            name="supertrend",
            strategy_class=SuperTrendStrategy,
            config_class=SuperTrendStrategyConfig,
            parameters_builder=build_parameters_config,
        )
    """
    fingerprint = (
        _component_fingerprint(strategy_class),
        _component_fingerprint(config_class),
        _component_fingerprint(parameters_builder),
    )
    if name in _STRATEGY_REGISTRY:
        if _STRATEGY_REGISTRY[name][3] == fingerprint:
            _STRATEGY_REGISTRY[name] = (
                strategy_class,
                config_class,
                parameters_builder,
                fingerprint,
            )
            return
        raise ValueError(f"Strategy '{name}' is already registered")
    _STRATEGY_REGISTRY[name] = (
        strategy_class,
        config_class,
        parameters_builder,
        fingerprint,
    )
    logger.debug(f"Registered strategy: {name}")


def _component_fingerprint(component: type[object] | Callable[..., object]) -> str:
    module_name = str(getattr(component, "__module__", "")).strip()
    qualified_name = str(getattr(component, "__qualname__", "")).strip()
    source_file = getsourcefile(component)
    if not module_name or not qualified_name or source_file is None:
        raise ValueError("strategy registration component lacks source authority")
    try:
        source_digest = sha256(Path(source_file).resolve(strict=True).read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError("strategy registration source authority is unavailable") from error
    return f"{module_name}:{qualified_name}:{source_digest}"


def unregister_strategy(name: str) -> None:
    """
    Unregister a strategy.

    Args:
        name: Strategy name to unregister

    Raises:
        KeyError: If strategy is not registered
    """
    if name not in _STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{name}' is not registered")
    del _STRATEGY_REGISTRY[name]


def create_strategy(
    name: str,
    *,
    config: NautilusTradingStrategyConfig | None = None,
    config_path: str | Path | None = None,
    config_wrapper: ConfigWrapper | None = None,
) -> NautilusStrategyCore:
    """
    Generic factory to create any registered strategy.

    Three ways to provide configuration (mutually exclusive):
    1. config: Pre-built strategy config object (for Speculum backtesting)
    2. config_path: Path to config.yaml file (for runner)
    3. config_wrapper: Pre-loaded ConfigWrapper (for programmatic use)

    Args:
        name: Registered strategy name (e.g., "supertrend")
        config: Pre-built strategy configuration object
        config_path: Path to strategy config.yaml file
        config_wrapper: Pre-loaded ConfigWrapper for building config

    Returns:
        Configured strategy instance

    Raises:
        ValueError: If strategy is not registered or no config provided
        TypeError: If config is wrong type for the strategy

    Examples:
        # From pre-built config (Speculum backtesting)
        config = SuperTrendStrategyConfig(parameters=..., **base_sections)
        strategy = create_strategy("supertrend", config=config)

        # From config file (runner)
        strategy = create_strategy("supertrend", config_path="trend/supertrend/config.yaml")

        # From ConfigWrapper (parameter sweeps)
        wrapper = load_config("trend/supertrend/config.yaml")
        strategy = create_strategy("supertrend", config_wrapper=wrapper)
    """
    # Lazy discovery on first call
    _ensure_discovery()

    if name not in _STRATEGY_REGISTRY:
        available = list(_STRATEGY_REGISTRY.keys())
        raise ValueError(f"Unknown strategy: '{name}'. Available: {available}")

    strategy_class, config_class, parameters_builder, _fingerprint = _STRATEGY_REGISTRY[name]

    # Option 1: Pre-built config - use directly
    if config is not None:
        if not isinstance(config, config_class):
            raise TypeError(f"Expected {config_class.__name__}, got {type(config).__name__}")
        return strategy_class(config=config)

    # Option 2: Config path - load and build
    if config_path is not None:
        config_wrapper = load_config(config_path)

    # Option 3: ConfigWrapper - build config
    if config_wrapper is not None:
        base_sections = build_nautilus_base_config(config_wrapper)
        parameters = parameters_builder(config_wrapper)
        config_factory = cast(_RegisteredConfigFactory, config_class)
        built_config = config_factory(parameters=parameters, **base_sections)
        return strategy_class(config=built_config)

    raise ValueError("Must provide one of: config, config_path, or config_wrapper")


def get_strategy_info(name: str) -> dict[str, object]:
    """
    Get information about a registered strategy.

    Args:
        name: Strategy name

    Returns:
        Dictionary with strategy_class, config_class, parameters_builder

    Raises:
        KeyError: If strategy is not registered
    """
    if name not in _STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{name}' is not registered")

    strategy_class, config_class, parameters_builder, _fingerprint = _STRATEGY_REGISTRY[name]
    return {
        "name": name,
        "strategy_class": strategy_class,
        "config_class": config_class,
        "parameters_builder": parameters_builder,
    }


def list_strategies() -> list[str]:
    """
    List all registered strategy names.

    Returns:
        List of strategy names
    """
    _ensure_discovery()
    return list(_STRATEGY_REGISTRY.keys())


def is_registered(name: str) -> bool:
    """
    Check if a strategy is registered.

    Args:
        name: Strategy name to check

    Returns:
        True if registered
    """
    _ensure_discovery()
    return name in _STRATEGY_REGISTRY

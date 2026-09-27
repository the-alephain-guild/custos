"""The release names and source inventory of a strategy made from the strategy template.

A template strategy lives at strategies/<category>/<name>/ with a pyproject.toml,
a config.yaml and its code under refinement/, and registers itself under <name>.
Everything a release needs to call it is derived from that layout and from the
repository that publishes it, so a producer writes no release configuration.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path, PurePosixPath

from .artifact_build import (
    ENTRY_POINT_GROUP,
    ArtifactBuildError,
    ProducerIdentity,
    StrategyReleaseSpec,
)
from .model import ENGINE_VERSION, PYTHON_REQUIRES
from .source_inventory import StrategySourceInventory, StrategySourceSpec

_TRANSIENT = frozenset({"__pycache__", ".DS_Store"})
TOOLKIT_IMPORT_PREFIXES = ("custos_toolkit", "custos_toolkit_nautilus")


def _strategy_path(strategy: str) -> PurePosixPath:
    path = PurePosixPath(strategy)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 3:
        raise ArtifactBuildError("a template strategy is strategies/<category>/<name>")
    if path.parts[0] != "strategies":
        raise ArtifactBuildError("a template strategy is strategies/<category>/<name>")
    return path


def _project(repo_root: Path, path: PurePosixPath) -> tuple[str, str]:
    pyproject = repo_root.joinpath(*path.parts, "pyproject.toml")
    if not pyproject.is_file():
        raise ArtifactBuildError(f"{path}/pyproject.toml is missing")
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    name, version = project.get("name"), project.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise ArtifactBuildError(f"{path}/pyproject.toml needs [project] name and version")
    return name, version


def _registered_name(repo_root: Path, path: PurePosixPath) -> str:
    """The name the strategy registers itself under, read from its source."""

    module = repo_root.joinpath(*path.parts, "refinement", "nautilus", "strategy.py")
    if not module.is_file():
        raise ArtifactBuildError(f"{path}/refinement/nautilus/strategy.py is missing")
    tree = ast.parse(module.read_bytes(), filename=str(module))
    names = [
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_strategy"
        for keyword in node.keywords
        if keyword.arg == "name"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    if len(names) != 1:
        raise ArtifactBuildError(
            f"{path} must register exactly one strategy with a literal name, found {len(names)}"
        )
    return names[0]


def template_strategy_spec(
    repo_root: Path,
    strategy: str,
    producer: ProducerIdentity,
) -> StrategyReleaseSpec:
    """Release names for strategies/<category>/<name>, published by `producer`."""

    path = _strategy_path(strategy)
    _, category, name = path.parts
    distribution, version = _project(repo_root, path)
    registered = _registered_name(repo_root, path)
    if registered != name:
        raise ArtifactBuildError(
            f"{path} registers itself as {registered!r}; it must match its directory name"
        )
    home = f"github.com/{producer.repository}"
    camel = "".join(part.capitalize() for part in name.replace("-", "_").split("_"))
    return StrategyReleaseSpec(
        registered_name=name,
        display_name=name,
        distribution=distribution,
        version=version,
        package=f"strategy_{name.replace('-', '_')}",
        coordinate=f"strategy://{home}/{category}/{name}@{version}",
        adapter_class="RuntimeAdapterV1",
        catalog_alias=name,
        member_coordinate_prefix=f"artifact://{home}/strategy/{category}/{name}/{version}",
        source_tree_name=f"{name}-source-tree-v1.bin",
        source_tree_fragment=f"{name}-source-tree",
        sbom_namespace=f"https://{home}/spdx/{category}/{name}",
        config_schema_id=f"https://{home}/strategies/{category}/{name}/config-v1.schema.json",
        config_schema_title=f"{camel}EffectiveConfigV1",
    )


def template_source_inventory(
    repo_root: Path,
    strategy: str,
    spec: StrategyReleaseSpec,
) -> StrategySourceInventory:
    """Everything under refinement/ is classified: Python is selected, the rest excluded."""

    path = _strategy_path(strategy)
    root = repo_root.joinpath(*path.parts)
    refinement = root / "refinement"
    if not refinement.is_dir():
        raise ArtifactBuildError(f"{path}/refinement is missing")
    selected = [f"{path}/pyproject.toml", f"{path}/config.yaml"]
    excluded: list[str] = []
    for file in sorted(refinement.rglob("*")):
        relative = file.relative_to(repo_root)
        if not file.is_file() or _TRANSIENT & set(relative.parts):
            continue
        if relative.suffix in {".pyc", ".pyo"}:
            continue
        (selected if relative.suffix == ".py" else excluded).append(relative.as_posix())
    source = StrategySourceSpec(
        strategy_coordinate=spec.coordinate,
        source_root=str(path),
        engine="nautilus",
        current_requires_python=PYTHON_REQUIRES,
        target_requires_python=PYTHON_REQUIRES,
        current_engine_version=ENGINE_VERSION,
        target_engine_version=ENGINE_VERSION,
        current_entry_point_group=ENTRY_POINT_GROUP,
        current_entry_point=spec.entry_point,
        target_entry_point_group=ENTRY_POINT_GROUP,
        target_execution_abi=ENTRY_POINT_GROUP,
        classification_roots=(f"{path}/refinement",),
        selected_sources=tuple(selected),
        deferred_sources=(),
        excluded_sources=tuple(excluded),
        externalized_import_prefixes=TOOLKIT_IMPORT_PREFIXES,
        blocking_gaps=(),
    )
    return StrategySourceInventory(root, source)


__all__ = ["template_source_inventory", "template_strategy_spec"]

from __future__ import annotations

import ast
import hashlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SOURCE_TREE_SCHEMA_VERSION = "alephain.team-source-tree.v1"
_SOURCE_TREE_DOMAIN = b"alephain.team-source-tree.v1\0"


class SourceInventoryError(ValueError):
    """A team source root or inventory cannot produce an immutable source tree."""


@dataclass(frozen=True, slots=True)
class StrategySourceSpec:
    strategy_coordinate: str
    source_root: str
    engine: str
    current_requires_python: str
    target_requires_python: str
    current_engine_version: str
    target_engine_version: str
    current_entry_point_group: str
    current_entry_point: str
    target_entry_point_group: str
    target_execution_abi: str
    classification_roots: tuple[str, ...]
    selected_sources: tuple[str, ...]
    deferred_sources: tuple[str, ...]
    excluded_sources: tuple[str, ...]
    externalized_import_prefixes: tuple[str, ...]
    blocking_gaps: tuple[str, ...]


_TOP_LEVEL_KEYS = frozenset({"schema_version", "inventory_status", "strategies"})
_SPEC_KEYS = frozenset(StrategySourceSpec.__dataclass_fields__)
_INVENTORY_STATUSES = frozenset({"PENDING_CONTRACT_ADAPTER", "READY_TEAM_SOURCE_ADAPTER"})


def load_source_specs(path: Path) -> tuple[StrategySourceSpec, ...]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if frozenset(raw) != _TOP_LEVEL_KEYS:
        raise ValueError("team strategy inventory has missing or unknown top-level keys")
    if raw["schema_version"] != 1:
        raise ValueError("team strategy inventory schema_version must be 1")
    inventory_status = raw["inventory_status"]
    if inventory_status not in _INVENTORY_STATUSES:
        raise ValueError("team strategy inventory has an unknown inventory_status")
    strategies = raw["strategies"]
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("inventory must contain at least one strategy")
    parsed: list[StrategySourceSpec] = []
    for item in strategies:
        if not isinstance(item, dict) or frozenset(item) != _SPEC_KEYS:
            raise ValueError("strategy inventory entry has missing or unknown keys")
        normalized = dict(item)
        for key in (
            "classification_roots",
            "selected_sources",
            "deferred_sources",
            "excluded_sources",
            "externalized_import_prefixes",
            "blocking_gaps",
        ):
            values = normalized[key]
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise TypeError(f"{key} must be an array of strings")
            normalized[key] = tuple(values)
        parsed.append(StrategySourceSpec(**normalized))
    if inventory_status == "READY_TEAM_SOURCE_ADAPTER" and any(
        "CUSTOS_EXECUTION_CONTRACT_RECEIPT_NOT_HANDOFF_READY" in spec.blocking_gaps
        or "SOURCE_GIT_CLEANLINESS_NOT_PROVEN" in spec.blocking_gaps
        for spec in parsed
    ):
        raise ValueError("ready team inventory retains a closed source-authority gap")
    coordinates = {spec.strategy_coordinate for spec in parsed}
    if len(coordinates) != len(parsed):
        raise ValueError("strategy coordinates must be unique")
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class NormalizedSourceMember:
    path: str
    sha256: str
    size_bytes: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class StrategySourceSnapshot:
    schema_version: str
    strategy_coordinate: str
    logical_source_root: str
    source_tree_sha256: str
    members: tuple[NormalizedSourceMember, ...]
    deferred_sources: tuple[str, ...]
    excluded_sources: tuple[str, ...]
    externalized_imports: tuple[str, ...]
    blocking_gaps: tuple[str, ...]
    canonical_bytes: bytes
    member_contents: tuple[bytes, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_coordinate": self.strategy_coordinate,
            "logical_source_root": self.logical_source_root,
            "source_tree_sha256": self.source_tree_sha256,
            "members": [member.to_mapping() for member in self.members],
            "deferred_sources": list(self.deferred_sources),
            "excluded_sources": list(self.excluded_sources),
            "externalized_imports": list(self.externalized_imports),
            "blocking_gaps": list(self.blocking_gaps),
        }

    def content(self, path: str) -> bytes:
        matches = [
            content
            for member, content in zip(self.members, self.member_contents, strict=True)
            if member.path == path
        ]
        if len(matches) != 1:
            raise SourceInventoryError(f"expected exactly one source member: {path}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class StrategySourceInventory:
    source_root: Path
    spec: StrategySourceSpec

    def freeze(self) -> StrategySourceSnapshot:
        return freeze_strategy_source_inventory(self.source_root, self.spec)


def _logical_path(value: str, label: str) -> PurePosixPath:
    if "\\" in value:
        raise SourceInventoryError(f"{label} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceInventoryError(f"{label} must be a normalized relative path: {value!r}")
    return path


def _relative_to_source_root(value: str, logical_root: PurePosixPath, label: str) -> PurePosixPath:
    path = _logical_path(value, label)
    try:
        relative = path.relative_to(logical_root)
    except ValueError as error:
        raise SourceInventoryError(
            f"{label} must be below source root {logical_root.as_posix()!r}: {value!r}"
        ) from error
    if not relative.parts:
        raise SourceInventoryError(f"{label} cannot name the source root itself")
    return relative


def _reject_symlink_path(root: Path, relative: PurePosixPath, label: str) -> Path:
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise SourceInventoryError(f"{label} cannot be a symlink: {relative.as_posix()}")
    return candidate


def _stable_read(path: Path, logical_path: str) -> bytes:
    try:
        before = path.stat(follow_symlinks=False)
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise SourceInventoryError(f"cannot read source member {logical_path!r}") from error
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or len(payload) != after.st_size:
        raise SourceInventoryError(f"unstable source metadata while reading {logical_path!r}")
    return payload


def _source_tree_bytes(members: tuple[tuple[NormalizedSourceMember, bytes], ...]) -> bytes:
    canonical = bytearray(_SOURCE_TREE_DOMAIN)
    for member, payload in members:
        path_bytes = member.path.encode("utf-8")
        canonical.extend(len(path_bytes).to_bytes(8, "big"))
        canonical.extend(path_bytes)
        canonical.extend(len(payload).to_bytes(8, "big"))
        canonical.extend(payload)
    return bytes(canonical)


def _externalized_imports(tree: ast.AST, prefixes: tuple[str, ...]) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        names: Iterable[str]
        if isinstance(node, ast.Import):
            names = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names = (node.module,)
        else:
            continue
        for name in names:
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                imports.add(name)
    return imports


def _is_sys_path(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "path"
        and isinstance(value.value, ast.Name)
        and value.value.id == "sys"
    )


def _contains_sys_path_mutation(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets: Iterable[ast.AST]
            if isinstance(node, ast.Assign):
                targets = node.targets
            else:
                targets = (node.target,)
            if any(_is_sys_path(target) for target in targets):
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "extend", "insert", "remove", "pop", "clear"}
            and _is_sys_path(node.func.value)
        ):
            return True
    return False


def _parse_python(path: str, payload: bytes) -> ast.AST:
    try:
        return ast.parse(payload, filename=path)
    except (SyntaxError, ValueError) as error:
        raise SourceInventoryError(f"invalid Python source member {path!r}") from error


def _classified_paths(
    spec: StrategySourceSpec,
    logical_root: PurePosixPath,
) -> tuple[
    tuple[PurePosixPath, ...],
    tuple[PurePosixPath, ...],
    tuple[PurePosixPath, ...],
]:
    selected = tuple(
        _relative_to_source_root(path, logical_root, "source member")
        for path in spec.selected_sources
    )
    deferred = tuple(
        _relative_to_source_root(path, logical_root, "deferred source")
        for path in spec.deferred_sources
    )
    excluded = tuple(
        _relative_to_source_root(path, logical_root, "excluded source")
        for path in spec.excluded_sources
    )
    all_paths = (*selected, *deferred, *excluded)
    if len(set(all_paths)) != len(all_paths):
        raise SourceInventoryError("selected, deferred, and excluded sources must be disjoint")
    return selected, deferred, excluded


def _verify_classification_closure(
    source_root: Path,
    spec: StrategySourceSpec,
    logical_root: PurePosixPath,
    classified: set[PurePosixPath],
) -> None:
    for configured_root in spec.classification_roots:
        relative_root = _relative_to_source_root(
            configured_root,
            logical_root,
            "classification root",
        )
        physical_root = _reject_symlink_path(
            source_root,
            relative_root,
            "classification root",
        )
        if not physical_root.is_dir():
            raise SourceInventoryError(
                f"classification root is not a directory: {relative_root.as_posix()}"
            )
        discovered: set[PurePosixPath] = set()
        for path in physical_root.rglob("*"):
            relative = PurePosixPath(path.relative_to(source_root).as_posix())
            if path.is_symlink():
                raise SourceInventoryError(f"classified source cannot be a symlink: {relative}")
            is_transient = (
                "__pycache__" in relative.parts
                or relative.suffix in {".pyc", ".pyo"}
                or relative.name == ".DS_Store"
            )
            if path.is_file() and not is_transient:
                discovered.add(relative)
        classified_below_root = {
            path for path in classified if path == relative_root or relative_root in path.parents
        }
        unclassified = sorted(path.as_posix() for path in discovered - classified_below_root)
        missing = sorted(path.as_posix() for path in classified_below_root - discovered)
        if unclassified:
            raise SourceInventoryError(
                f"unclassified source files below {relative_root}: {unclassified}"
            )
        if missing:
            raise SourceInventoryError(
                f"classified source files are missing below {relative_root}: {missing}"
            )


def freeze_strategy_source_inventory(
    source_root: Path,
    spec: StrategySourceSpec,
) -> StrategySourceSnapshot:
    """Freeze one explicit team source root without workspace or timestamp identity."""

    logical_root = _logical_path(spec.source_root, "source root")
    if source_root.is_symlink():
        raise SourceInventoryError("source root cannot be a symlink")
    if not source_root.is_dir():
        raise SourceInventoryError(f"source root is not a directory: {source_root}")

    selected, deferred, excluded = _classified_paths(spec, logical_root)
    _verify_classification_closure(
        source_root,
        spec,
        logical_root,
        {*selected, *deferred, *excluded},
    )

    frozen_members: list[tuple[NormalizedSourceMember, bytes]] = []
    externalized_imports: set[str] = set()
    for relative in sorted(selected, key=lambda path: path.as_posix().encode("utf-8")):
        logical_path = relative.as_posix()
        physical_path = _reject_symlink_path(source_root, relative, "source member")
        if not physical_path.is_file():
            raise SourceInventoryError(f"source member is not a regular file: {logical_path}")
        payload = _stable_read(physical_path, logical_path)
        member = NormalizedSourceMember(
            path=logical_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        frozen_members.append((member, payload))
        if physical_path.suffix == ".py":
            tree = _parse_python(logical_path, payload)
            if _contains_sys_path_mutation(tree):
                raise SourceInventoryError(f"source member mutates sys.path: {logical_path}")
            externalized_imports.update(
                _externalized_imports(tree, spec.externalized_import_prefixes)
            )

    members_with_bytes = tuple(frozen_members)
    canonical_bytes = _source_tree_bytes(members_with_bytes)
    return StrategySourceSnapshot(
        schema_version=SOURCE_TREE_SCHEMA_VERSION,
        strategy_coordinate=spec.strategy_coordinate,
        logical_source_root=logical_root.as_posix(),
        source_tree_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        members=tuple(member for member, _ in members_with_bytes),
        deferred_sources=tuple(sorted(path.as_posix() for path in deferred)),
        excluded_sources=tuple(sorted(path.as_posix() for path in excluded)),
        externalized_imports=tuple(sorted(externalized_imports)),
        blocking_gaps=tuple(sorted(set(spec.blocking_gaps))),
        canonical_bytes=canonical_bytes,
        member_contents=tuple(payload for _, payload in members_with_bytes),
    )


def load_strategy_source_inventories(
    repo_root: Path,
    inventory_path: Path,
) -> tuple[StrategySourceInventory, ...]:
    """Load the producer's inventory, which must be a regular file inside the repository."""

    root = repo_root.resolve()
    if inventory_path.is_symlink():
        raise SourceInventoryError("the source inventory cannot be a symlink")
    expected_inventory = inventory_path.resolve()
    if not expected_inventory.is_relative_to(root):
        raise SourceInventoryError("the source inventory must be inside the repository")

    specs = load_source_specs(expected_inventory)
    adapters: list[StrategySourceInventory] = []
    for spec in specs:
        logical_root = _logical_path(spec.source_root, "source root")
        physical_root = _reject_symlink_path(root, logical_root, "source root")
        adapters.append(StrategySourceInventory(physical_root, spec))
    return tuple(adapters)

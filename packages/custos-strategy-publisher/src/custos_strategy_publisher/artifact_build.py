"""Deterministic isolated builder for signed strategy release artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import yaml

from .build_input import (
    BUILD_INPUT_FILENAME,
    BUILD_INPUT_SCHEMA_VERSION,
    PRODUCER_GATE_SCHEMA_VERSION,
)
from .model import (
    BOM_SCHEMA_VERSION,
    CANONICALIZATION,
    ENGINE_VERSION,
    IN_TOTO_STATEMENT_TYPE,
    PYTHON_REQUIRES,
    STRATEGY_RELEASE_PREDICATE_SCHEMA_VERSION,
    STRATEGY_RELEASE_PREDICATE_TYPE,
    ArtifactMemberRole,
    BomMemberV1,
    StatementSubjectV1,
    StrategyReleaseBomV1,
    StrategyReleasePredicateV1,
    StrategyReleaseStatementV1,
    canonical_json_bytes,
)
from .oci_primitives import RELEASE_LAYER_MEDIA_TYPES, require_ghcr_repository
from .producer import CleanProducerEvidence, produce_canonical_strategy_release_bom
from .source_inventory import StrategySourceInventory, StrategySourceSnapshot
from .toolkit_authority import CustosToolkitAuthorityV1, bind_contract_authority
from .toolkit_registry import ToolkitRegistrySnapshotV1
from .wheel_archive import VerifiedWheelV1, build_wheel, inspect_wheel

ENTRY_POINT_GROUP = "alephain.strategy_runtime.v1"
PYTHON_VERSION = "3.12.13"
TYPING_RECEIPT_SCHEMA_VERSION = "alephain.strategy-artifact-typing-receipt.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class StrategyReleaseSpec:
    """What one strategy release is called and how its members are addressed.

    Every name the release carries comes from here, so the same builder serves
    any producer; see `template_strategy_spec` for the names a strategy made
    from the strategy template gets.
    """

    registered_name: str
    display_name: str
    distribution: str
    version: str
    package: str
    coordinate: str
    adapter_class: str
    catalog_alias: str
    member_coordinate_prefix: str
    source_tree_name: str
    source_tree_fragment: str
    sbom_namespace: str
    config_schema_id: str
    config_schema_title: str
    # The parameters whose values the characterization records; None records all.
    characterization_parameters: tuple[str, ...] | None = None
    extra_dependencies: tuple[str, ...] = ("msgspec>=0.18,<1",)
    wheel_generator: str = "custos-strategy-publisher-v1"

    def __post_init__(self) -> None:
        for field in ("package", "adapter_class"):
            if _IDENTIFIER.fullmatch(getattr(self, field)) is None:
                raise ArtifactBuildError(f"{field} must be a Python identifier")
        if not self.coordinate.endswith(f"@{self.version}"):
            raise ArtifactBuildError("strategy coordinate must end with the release version")

    @property
    def entry_point(self) -> str:
        return f"{self.package}.runtime:{self.adapter_class}"

    @property
    def wheel_name(self) -> str:
        return f"{self.package}-{self.version}-py3-none-any.whl"


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    """The repository a release is built from and where and as what it is published."""

    repository: str
    ghcr_repository: str
    workflow_identity: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", self.repository) is None:
            raise ArtifactBuildError("producer repository must be <owner>/<name>")
        require_ghcr_repository(self.ghcr_repository)
        if not self.workflow_identity.startswith("https://github.com/"):
            raise ArtifactBuildError("workflow identity must be a github.com workflow")

    @property
    def repository_url(self) -> str:
        return f"https://github.com/{self.repository}"


_IMPORT_REWRITES = {
    "shared.nautilus.indicators": "custos_toolkit_nautilus.adapter.indicators",
    "shared.nautilus": "custos_toolkit_nautilus.adapter",
    "shared.config": "custos_toolkit.config",
    "shared.signals": "custos_toolkit.signals",
}
_FIXED_LAYER_NAMES = {
    "strategy_manifest": "strategy-manifest-v1.json",
    "strategy_artifact_ref": "strategy-artifact-ref-v1.json",
    "strategy_release_bom": "strategy-release-bom-v1.json",
    "strategy_sbom": "strategy.spdx.json",
    "strategy_release_statement": "strategy-release-statement-v1.json",
}


def release_layer_names(spec: StrategyReleaseSpec) -> dict[str, str]:
    """The file name of each release layer; only the wheel's carries the strategy's name."""

    return {"strategy_artifact": spec.wheel_name, **_FIXED_LAYER_NAMES}


class ArtifactBuildError(ValueError):
    """The strategy artifact could not be built from closed verified inputs."""


@dataclass(frozen=True, slots=True)
class StrategyTypingRequestV1:
    package: str
    entry_point: str
    package_files: Mapping[str, bytes]
    source_tree_sha256: str
    base_contracts_wheel_sha256: str
    nautilus_wheel_sha256: str
    base_contracts_wheel: VerifiedWheelV1
    nautilus_wheel: VerifiedWheelV1


class StrategyTypingVerifier(Protocol):
    def verify(self, request: StrategyTypingRequestV1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StrategyArtifactBuildResultV1:
    output: Path
    strategy_wheel_sha256: str
    strategy_manifest_sha256: str
    strategy_artifact_ref_sha256: str
    strategy_release_bom_sha256: str
    strategy_sbom_sha256: str
    strategy_release_statement_sha256: str
    build_lock_sha256: str
    zero_rewrite_semantic_diff_sha256: str
    zero_rewrite_characterization_sha256: str


class _ImportNormalizer(ast.NodeTransformer):
    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._mapping = mapping

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        node = cast(ast.ImportFrom, self.generic_visit(node))
        if node.module in self._mapping:
            node.module = self._mapping[node.module]
        return node

    def visit_Import(self, node: ast.Import) -> ast.AST:
        node = cast(ast.Import, self.generic_visit(node))
        for alias in node.names:
            if alias.name in self._mapping:
                alias.name = self._mapping[alias.name]
        return node


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _strict_json(content: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ArtifactBuildError(f"{label} contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        parsed = json.loads(content, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactBuildError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise ArtifactBuildError(f"{label} must be an object")
    return cast(dict[str, object], parsed)


def _rewrite_imports(path: str, content: bytes) -> tuple[bytes, dict[str, int]]:
    try:
        source = content.decode("utf-8")
        before = ast.parse(source, filename=path)
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ArtifactBuildError(f"strategy source is not valid UTF-8 Python: {path}") from error
    counts = dict.fromkeys(_IMPORT_REWRITES, 0)
    lines: list[str] = []
    for line in source.splitlines(keepends=True):
        stripped = line.lstrip()
        prefix = line[: len(line) - len(stripped)]
        rewritten = stripped
        for old, new in _IMPORT_REWRITES.items():
            from_prefix = f"from {old} import "
            import_prefix = f"import {old}"
            if rewritten.startswith(from_prefix):
                rewritten = f"from {new} import {rewritten[len(from_prefix) :]}"
                counts[old] += 1
                break
            if rewritten == f"import {old}\n" or rewritten.startswith(f"{import_prefix} as "):
                rewritten = rewritten.replace(import_prefix, f"import {new}", 1)
                counts[old] += 1
                break
        lines.append(prefix + rewritten)
    output = "".join(lines).encode("utf-8")
    try:
        after = ast.parse(output.decode("utf-8"), filename=path)
    except SyntaxError as error:
        raise ArtifactBuildError(f"rewritten strategy source is invalid: {path}") from error
    # Both spellings collapse onto one placeholder on both sides. Normalizing the
    # source side by the legacy name alone assumes the strategy still uses it; a
    # strategy that already names the toolkit would then differ from its own
    # unrewritten output and be rejected as a semantic change.
    normalizer = _ImportNormalizer(
        {
            name: f"alephain_external.{index}"
            for index, (old, new) in enumerate(_IMPORT_REWRITES.items())
            for name in (old, new)
        }
    )
    before_normalized = normalizer.visit(before)
    after_normalized = normalizer.visit(after)
    if ast.dump(before_normalized, include_attributes=False) != ast.dump(
        after_normalized,
        include_attributes=False,
    ):
        raise ArtifactBuildError(f"strategy rewrite changed non-import semantics: {path}")
    for node in ast.walk(after):
        module: str | None = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "shared" or alias.name.startswith("shared."):
                    raise ArtifactBuildError(f"legacy import remains after rewrite: {path}")
        if module == "shared" or (module is not None and module.startswith("shared.")):
            raise ArtifactBuildError(f"legacy import remains after rewrite: {path}")
    return output, {old: count for old, count in counts.items() if count}


def _runtime_module(spec: StrategyReleaseSpec) -> bytes:
    return f'''"""Artifact-local Custos execution ABI adapter for {spec.display_name}."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import cast
from custos_toolkit.config import ConfigWrapper, deep_merge, load_yaml_file
from custos_toolkit.config import loader as _config_loader
from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject,
    StrategyExecutionContextV1,
)
from custos_toolkit_nautilus.adapter import create_strategy

from .refinement.nautilus import strategy as _strategy_registration


type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


def _materialize(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {{str(key): _materialize(item) for key, item in value.items()}}
    if isinstance(value, tuple):
        return [_materialize(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported effective config value: {{type(value).__name__}}")


class {spec.adapter_class}:
    """Build the registered strategy only through the Custos V1 execution ABI."""

    def build_config(
        self,
        effective_config: FrozenJsonObject,
        execution_context: StrategyExecutionContextV1,
    ) -> ConfigWrapper:
        if execution_context.engine != "nautilus":
            raise ValueError("{spec.display_name} requires the Nautilus engine")
        base_path = Path(_config_loader.__file__).with_name("base_config.yaml")
        strategy_path = Path(__file__).with_name("config.yaml")
        base = load_yaml_file(base_path)
        defaults = load_yaml_file(strategy_path)
        effective = _materialize(effective_config)
        if not isinstance(effective, dict):
            raise TypeError("effective strategy config must materialize to an object")
        effective_mapping = cast(dict[str, object], effective)
        return ConfigWrapper(deep_merge(deep_merge(base, defaults), effective_mapping))

    def build_strategy(self, config: ConfigWrapper) -> object:
        _ = _strategy_registration
        return create_strategy("{spec.registered_name}", config_wrapper=config)
'''.encode()


def _package_files(
    snapshot: StrategySourceSnapshot,
    spec: StrategyReleaseSpec,
) -> tuple[dict[str, bytes], bytes, bytes]:
    package: dict[str, bytes] = {
        f"{spec.package}/__init__.py": f'__version__ = "{spec.version}"\n'.encode(),
        f"{spec.package}/runtime.py": _runtime_module(spec),
        f"{spec.package}/py.typed": b"",
    }
    diff_files: list[dict[str, object]] = []
    characterization_files: list[dict[str, object]] = []
    for member in snapshot.members:
        if member.path == "pyproject.toml":
            continue
        content = snapshot.content(member.path)
        artifact_path = f"{spec.package}/{member.path}"
        rewrites: dict[str, int] = {}
        if member.path.endswith(".py"):
            transformed, rewrites = _rewrite_imports(member.path, content)
            tree = ast.parse(transformed.decode("utf-8"), filename=artifact_path)
            characterization_files.append(
                {
                    "artifact_path": artifact_path,
                    "classes": sorted(
                        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                    ),
                    "functions": sorted(
                        node.name
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ),
                }
            )
        else:
            transformed = content
        package[artifact_path] = transformed
        diff_files.append(
            {
                "artifact_path": artifact_path,
                "artifact_sha256": _sha256(transformed),
                "ast_equivalent_except_import_namespace": member.path.endswith(".py"),
                "donor_path": member.path,
                "donor_sha256": member.sha256,
                "namespace_rewrites": rewrites,
            }
        )
    semantic_diff = canonical_json_bytes(
        {
            "allowed_changes": [
                "external_import_namespace_rewrite",
                "artifact_local_runtime_adapter",
                "artifact_packaging_metadata",
            ],
            "files": diff_files,
            "legacy_imports_remaining": False,
            "schema_version": "alephain.strategy-artifact-zero-rewrite-semantic-diff.v1",
            "source_tree_sha256": snapshot.source_tree_sha256,
            "strategy_coordinate": snapshot.strategy_coordinate,
        }
    )
    strategy_config = yaml.safe_load(snapshot.content("config.yaml"))
    parameters = strategy_config.get("parameters", {}) if isinstance(strategy_config, dict) else {}
    characterization = canonical_json_bytes(
        {
            "custos_extraction_authority": "receipt-pinned-toolkit-rc",
            "files": characterization_files,
            "fixed_input": {
                name: _config_leaf(parameters, name)
                for name in _characterized(parameters, spec.characterization_parameters)
            },
            "schema_version": "alephain.strategy-artifact-zero-rewrite-characterization.v1",
            "source_tree_sha256": snapshot.source_tree_sha256,
            "strategy_coordinate": snapshot.strategy_coordinate,
        }
    )
    return package, semantic_diff, characterization


def _characterized(parameters: object, named: tuple[str, ...] | None) -> tuple[str, ...]:
    if named is not None:
        return named
    if not isinstance(parameters, Mapping):
        raise ArtifactBuildError("strategy parameters must be an object")
    return tuple(sorted(str(name) for name in parameters))


def _config_leaf(parameters: object, name: str) -> object:
    if not isinstance(parameters, Mapping):
        raise ArtifactBuildError("strategy parameters must be an object")
    value = parameters.get(name)
    if not isinstance(value, Mapping) or "value" not in value:
        raise ArtifactBuildError(f"strategy parameter is absent: {name}")
    leaf = value["value"]
    if isinstance(leaf, float):
        return str(leaf)
    if isinstance(leaf, (str, int, bool)) or leaf is None:
        return leaf
    raise ArtifactBuildError(f"strategy parameter value is unsupported: {name}")


def _config_schema(base_config: bytes, strategy_config: bytes, spec: StrategyReleaseSpec) -> bytes:
    try:
        base = yaml.safe_load(base_config)
        strategy = yaml.safe_load(strategy_config)
    except yaml.YAMLError as error:
        raise ArtifactBuildError("toolkit or strategy config is invalid YAML") from error
    if not isinstance(base, Mapping) or not isinstance(strategy, Mapping):
        raise ArtifactBuildError("toolkit and strategy config roots must be objects")
    keys = sorted({str(key) for key in base} | {str(key) for key in strategy})
    return canonical_json_bytes(
        {
            "$id": spec.config_schema_id,
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "properties": {key: {"type": "object"} for key in keys},
            "title": spec.config_schema_title,
            "type": "object",
        }
    )


def typing_receipt_bytes(
    request: StrategyTypingRequestV1,
    *,
    verifier: str,
    verified: bool,
) -> bytes:
    """Create the closed typing receipt used by production and test verifiers."""

    return canonical_json_bytes(
        {
            "base_contracts_wheel_sha256": request.base_contracts_wheel_sha256,
            "checked_modules": [request.package],
            "engine": "nautilus",
            "engine_version": ENGINE_VERSION,
            "entry_point": request.entry_point,
            "nautilus_wheel_sha256": request.nautilus_wheel_sha256,
            "python_requires": PYTHON_REQUIRES,
            "schema_version": TYPING_RECEIPT_SCHEMA_VERSION,
            "source_tree_sha256": request.source_tree_sha256,
            "verified": verified,
            "verifier": verifier,
        }
    )


def _verify_typing_receipt(content: bytes, request: StrategyTypingRequestV1) -> None:
    value = _strict_json(content, "strategy typing receipt")
    expected = _strict_json(
        typing_receipt_bytes(request, verifier=str(value.get("verifier", "")), verified=True),
        "expected strategy typing receipt",
    )
    if not value.get("verifier") or value != expected or canonical_json_bytes(value) != content:
        raise ArtifactBuildError("strategy typing receipt is not verified or exact")


def _member(
    role: ArtifactMemberRole,
    name: str,
    media_type: str,
    content: bytes,
    coordinate_prefix: str,
) -> BomMemberV1:
    digest = _sha256(content)
    return BomMemberV1(
        role=role,
        coordinate=f"{coordinate_prefix}@sha256:{digest}",
        name=name,
        media_type=media_type,
        size_bytes=len(content),
        sha256=digest,
    )


def _runtime_artifact(config_schema: bytes) -> dict[str, object]:
    return {
        "media_type": "application/schema+json",
        "name": "resources/strategy-config-v1.schema.json",
        "role": "runtime_artifact",
        "sha256": _sha256(config_schema),
        "size_bytes": len(config_schema),
    }


_CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_]{1,63}")


def _trading_scope(strategy_config: bytes) -> dict[str, object]:
    """Read the connector, pairs and leverage the strategy's own config trades.

    The manifest declares this scope so a deployment can be refused when it
    authorizes a different one; a missing or unusable value fails the build
    rather than publishing a release without a scope.
    """
    config = yaml.safe_load(strategy_config)
    trading = config.get("trading") if isinstance(config, Mapping) else None
    if not isinstance(trading, Mapping):
        raise ArtifactBuildError("strategy config has no trading section")

    def leaf(name: str) -> object:
        node = trading.get(name)
        if not isinstance(node, Mapping) or "value" not in node:
            raise ArtifactBuildError(f"strategy trading value is absent: {name}")
        return node["value"]

    connector = leaf("connector")
    if not isinstance(connector, str) or _CONNECTOR_NAME.fullmatch(connector) is None:
        raise ArtifactBuildError("strategy trading connector must be a connector name")
    pairs = leaf("pairs")
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
        or len(set(pairs)) != len(pairs)
    ):
        raise ArtifactBuildError("strategy trading pairs must be unique non-empty names")
    leverage = leaf("leverage")
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise ArtifactBuildError("strategy trading leverage must be an integer of at least 1")
    return {"connector": connector, "pairs": list(pairs), "leverage": leverage}


def _strategy_manifest(
    config_schema: bytes,
    toolkit_version: str,
    trading_scope: Mapping[str, object],
    spec: StrategyReleaseSpec,
) -> bytes:
    return canonical_json_bytes(
        {
            "base_contracts_version": toolkit_version,
            "catalog_alias": spec.catalog_alias,
            "config_schema_sha256": _sha256(config_schema),
            "engine": "nautilus",
            "engine_toolkit_version": toolkit_version,
            "engine_version": ENGINE_VERSION,
            "entry_point": spec.entry_point,
            "entry_point_group": ENTRY_POINT_GROUP,
            "execution_abi": ENTRY_POINT_GROUP,
            "requires_python": PYTHON_REQUIRES,
            "runtime_artifacts": [_runtime_artifact(config_schema)],
            "schema_version": 1,
            "trading_scope": dict(trading_scope),
        }
    )


def _strategy_artifact_ref(
    *,
    artifact_coordinate: str,
    strategy_wheel: bytes,
    strategy_manifest: bytes,
    strategy_sbom: bytes,
    config_schema: bytes,
    contract_schema_sha256: str,
    producer_commit: str,
    source_tree_sha256: str,
    toolkit_version: str,
    build_lock: bytes,
    source_repository: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_coordinate": artifact_coordinate,
            "artifact_kind": "wheel",
            "artifact_sha256": _sha256(strategy_wheel),
            "artifact_size_bytes": len(strategy_wheel),
            "base_contracts_version": toolkit_version,
            "build_inputs": [
                {
                    "name": "resources/build-lock-v1.json",
                    "sha256": _sha256(build_lock),
                }
            ],
            "contract_schema_sha256": contract_schema_sha256,
            "engine": "nautilus",
            "engine_toolkit_version": toolkit_version,
            "engine_version": ENGINE_VERSION,
            "manifest_sha256": _sha256(strategy_manifest),
            "manifest_size_bytes": len(strategy_manifest),
            "normalized_source_tree_sha256": source_tree_sha256,
            "python_version": PYTHON_VERSION,
            "required_runtime_artifacts": [_runtime_artifact(config_schema)],
            "sbom_sha256": _sha256(strategy_sbom),
            "schema_version": 1,
            "source_commit": producer_commit,
            "source_repository": source_repository,
        }
    )


def _strategy_sbom(
    *,
    producer_commit: str,
    source_date_epoch: int,
    wheel: bytes,
    authority: CustosToolkitAuthorityV1,
    spec: StrategyReleaseSpec,
) -> bytes:
    return canonical_json_bytes(
        {
            "SPDXID": "SPDXRef-DOCUMENT",
            "creationInfo": {
                "created": datetime.fromtimestamp(source_date_epoch, tz=UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "creators": ["Organization: The Alephain Guild"],
            },
            "dataLicense": "CC0-1.0",
            "documentNamespace": (f"{spec.sbom_namespace}/{producer_commit}"),
            "name": f"{spec.distribution}-{spec.version}",
            "packages": [
                _spdx_package(
                    "SPDXRef-Strategy",
                    spec.distribution,
                    spec.version,
                    _sha256(wheel),
                ),
                _spdx_package(
                    "SPDXRef-CustosBase",
                    "custos-strategy-toolkit",
                    authority.candidate_version,
                    authority.base_contracts_wheel.sha256,
                ),
                _spdx_package(
                    "SPDXRef-CustosNautilus",
                    "custos-strategy-toolkit-nautilus",
                    authority.candidate_version,
                    authority.nautilus_wheel.sha256,
                ),
            ],
            "relationships": [
                {
                    "relatedSpdxElement": "SPDXRef-CustosBase",
                    "relationshipType": "DEPENDS_ON",
                    "spdxElementId": "SPDXRef-Strategy",
                },
                {
                    "relatedSpdxElement": "SPDXRef-CustosNautilus",
                    "relationshipType": "DEPENDS_ON",
                    "spdxElementId": "SPDXRef-Strategy",
                },
            ],
            "spdxVersion": "SPDX-2.3",
        }
    )


def _spdx_package(spdx_id: str, name: str, version: str, digest: str) -> dict[str, object]:
    return {
        "SPDXID": spdx_id,
        "checksums": [{"algorithm": "SHA256", "checksumValue": digest}],
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "name": name,
        "versionInfo": version,
    }


def _file_spec(path: str, content: bytes) -> dict[str, object]:
    return {"path": path, "sha256": _sha256(content), "size_bytes": len(content)}


def _atomic_materialize(output: Path, files: Mapping[str, bytes]) -> None:
    output = output.resolve()
    if output.exists():
        raise ArtifactBuildError("strategy artifact output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        for relative, content in sorted(files.items()):
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ArtifactBuildError("strategy artifact output path is unsafe")
            target = temporary.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_strategy_artifact_tree(
    *,
    spec: StrategyReleaseSpec,
    producer: ProducerIdentity,
    inventory: StrategySourceInventory,
    authority: CustosToolkitAuthorityV1,
    toolkit: ToolkitRegistrySnapshotV1,
    producer_commit: str,
    source_date_epoch: int,
    discovery_tag: str,
    output: Path,
    typing_verifier: StrategyTypingVerifier,
    producer_worktree_clean: bool,
) -> StrategyArtifactBuildResultV1:
    """Build and atomically materialize one complete unsigned build input tree."""

    if not producer_worktree_clean:
        raise ArtifactBuildError("clean producer worktree evidence is required")
    if source_date_epoch <= 0:
        raise ArtifactBuildError("source date epoch must be positive")
    try:
        datetime.fromtimestamp(source_date_epoch, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ArtifactBuildError("source date epoch is outside the supported range") from error
    if len(producer_commit) != 40 or any(
        character not in "0123456789abcdef" for character in producer_commit
    ):
        raise ArtifactBuildError("producer commit must be full lowercase hex")
    if (
        _sha256(toolkit.manifest) != authority.manifest_digest.removeprefix("sha256:")
        or len(toolkit.manifest) != authority.manifest_size_bytes
    ):
        raise ArtifactBuildError("toolkit registry manifest differs from authority")
    contract_authority = bind_contract_authority(authority, toolkit.contract_asset_index)
    base_wheel = inspect_wheel(
        toolkit.base_contracts_wheel,
        distribution="custos-strategy-toolkit",
        version=authority.candidate_version,
        requires_python=">=3.11",
        required_modules=("custos_toolkit",),
    )
    nautilus_wheel = inspect_wheel(
        toolkit.nautilus_wheel,
        distribution="custos-strategy-toolkit-nautilus",
        version=authority.candidate_version,
        requires_python=PYTHON_REQUIRES,
        required_modules=("custos_toolkit_nautilus",),
    )
    if _sha256(toolkit.base_contracts_wheel) != authority.base_contracts_wheel.sha256:
        raise ArtifactBuildError("base contracts wheel bytes differ")
    if _sha256(toolkit.nautilus_wheel) != authority.nautilus_wheel.sha256:
        raise ArtifactBuildError("Nautilus toolkit wheel bytes differ")
    if _sha256(toolkit.base_contracts_sbom) != authority.base_contracts_sbom.sha256:
        raise ArtifactBuildError("base contracts SBOM bytes differ")
    if _sha256(toolkit.nautilus_sbom) != authority.nautilus_sbom.sha256:
        raise ArtifactBuildError("Nautilus toolkit SBOM bytes differ")

    if inventory.spec.strategy_coordinate != spec.coordinate:
        raise ArtifactBuildError("the source inventory is for another strategy coordinate")
    snapshot = inventory.freeze()
    if snapshot.blocking_gaps:
        raise ArtifactBuildError(
            f"strategy source inventory retains blockers: {snapshot.blocking_gaps}"
        )
    package_files, semantic_diff, characterization = _package_files(snapshot, spec)
    base_config = base_wheel.content("custos_toolkit/config/base_config.yaml")
    config_schema = _config_schema(base_config, snapshot.content("config.yaml"), spec)
    package_files[f"{spec.package}/resources/strategy-config-v1.schema.json"] = config_schema
    typing_request = StrategyTypingRequestV1(
        package=spec.package,
        entry_point=spec.entry_point,
        package_files=package_files,
        source_tree_sha256=snapshot.source_tree_sha256,
        base_contracts_wheel_sha256=authority.base_contracts_wheel.sha256,
        nautilus_wheel_sha256=authority.nautilus_wheel.sha256,
        base_contracts_wheel=base_wheel,
        nautilus_wheel=nautilus_wheel,
    )
    typing_receipt = typing_verifier.verify(typing_request)
    _verify_typing_receipt(typing_receipt, typing_request)
    package_files[f"{spec.package}/resources/typing-receipt-v1.json"] = typing_receipt
    build_lock = canonical_json_bytes(
        {
            "base_contracts_sbom_sha256": authority.base_contracts_sbom.sha256,
            "base_contracts_wheel_sha256": authority.base_contracts_wheel.sha256,
            "contract_asset_index_sha256": authority.contract_asset_index.sha256,
            "custos_authority_receipt_sha256": authority.authority_receipt.sha256,
            "custos_manifest_digest": authority.manifest_digest,
            "engine": "nautilus",
            "engine_version": ENGINE_VERSION,
            "nautilus_toolkit_sbom_sha256": authority.nautilus_sbom.sha256,
            "nautilus_toolkit_wheel_sha256": authority.nautilus_wheel.sha256,
            "producer_commit": producer_commit,
            "python_requires": PYTHON_REQUIRES,
            "python_version": PYTHON_VERSION,
            "schema_version": "alephain.strategy-artifact-build-lock.v1",
            "source_date_epoch": source_date_epoch,
            "source_tree_sha256": snapshot.source_tree_sha256,
            "strategy_coordinate": spec.coordinate,
            "typing_receipt_sha256": _sha256(typing_receipt),
        }
    )
    package_files[f"{spec.package}/resources/build-lock-v1.json"] = build_lock
    package_files[f"{spec.package}/resources/zero-rewrite-semantic-diff-v1.json"] = semantic_diff
    package_files[f"{spec.package}/resources/zero-rewrite-characterization-v1.json"] = (
        characterization
    )
    strategy_wheel = build_wheel(
        package_files,
        distribution=spec.distribution,
        version=spec.version,
        requires_python=PYTHON_REQUIRES,
        dependencies=(
            f"custos-strategy-toolkit=={authority.candidate_version}",
            f"custos-strategy-toolkit-nautilus=={authority.candidate_version}",
            f"nautilus-trader=={ENGINE_VERSION}",
            *spec.extra_dependencies,
        ),
        entry_points={ENTRY_POINT_GROUP: {spec.registered_name: spec.entry_point}},
        generator=spec.wheel_generator,
    )
    inspect_wheel(
        strategy_wheel,
        distribution=spec.distribution,
        version=spec.version,
        requires_python=PYTHON_REQUIRES,
        required_modules=(spec.package,),
    )
    strategy_manifest = _strategy_manifest(
        config_schema,
        authority.candidate_version,
        _trading_scope(snapshot.content("config.yaml")),
        spec,
    )
    strategy_sbom = _strategy_sbom(
        producer_commit=producer_commit,
        source_date_epoch=source_date_epoch,
        wheel=strategy_wheel,
        authority=authority,
        spec=spec,
    )
    layer_names = release_layer_names(spec)
    members = (
        _member(
            ArtifactMemberRole.BASE_CONTRACTS_WHEEL,
            authority.base_contracts_wheel.title,
            authority.base_contracts_wheel.media_type,
            toolkit.base_contracts_wheel,
            f"{authority.oci_coordinate}#{authority.base_contracts_wheel.title}",
        ),
        _member(
            ArtifactMemberRole.NAUTILUS_WHEEL,
            authority.nautilus_wheel.title,
            authority.nautilus_wheel.media_type,
            toolkit.nautilus_wheel,
            f"{authority.oci_coordinate}#{authority.nautilus_wheel.title}",
        ),
        _member(
            ArtifactMemberRole.STRATEGY_WHEEL,
            layer_names["strategy_artifact"],
            "application/vnd.pypa.wheel",
            strategy_wheel,
            f"{spec.member_coordinate_prefix}/strategy-wheel",
        ),
        _member(
            ArtifactMemberRole.STRATEGY_MANIFEST,
            layer_names["strategy_manifest"],
            "application/vnd.alephain.strategy-manifest.v1+json",
            strategy_manifest,
            f"{spec.member_coordinate_prefix}/strategy-manifest",
        ),
        _member(
            ArtifactMemberRole.RUNTIME_ARTIFACT,
            "resources/strategy-config-v1.schema.json",
            "application/schema+json",
            config_schema,
            f"{spec.member_coordinate_prefix}/config-schema",
        ),
        _member(
            ArtifactMemberRole.TOOLKIT_SBOM,
            authority.nautilus_sbom.title,
            authority.nautilus_sbom.media_type,
            toolkit.nautilus_sbom,
            f"{authority.oci_coordinate}#{authority.nautilus_sbom.title}",
        ),
        _member(
            ArtifactMemberRole.STRATEGY_SBOM,
            layer_names["strategy_sbom"],
            "application/spdx+json",
            strategy_sbom,
            f"{spec.member_coordinate_prefix}/strategy-sbom",
        ),
        _member(
            ArtifactMemberRole.CONTRACT_SCHEMA,
            authority.contract_asset_index.title,
            authority.contract_asset_index.media_type,
            toolkit.contract_asset_index,
            f"{authority.oci_coordinate}#{authority.contract_asset_index.title}",
        ),
        _member(
            ArtifactMemberRole.SOURCE_TREE,
            spec.source_tree_name,
            "application/vnd.alephain.source-tree.v1",
            snapshot.canonical_bytes,
            f"git+{producer.repository_url}@{producer_commit}#{spec.source_tree_fragment}",
        ),
    )
    by_role = {member.role: member for member in members}
    bom = StrategyReleaseBomV1(
        schema_version=BOM_SCHEMA_VERSION,
        canonicalization=CANONICALIZATION,
        producer_repository=producer.repository_url,
        producer_commit=producer_commit,
        strategy_coordinate=spec.coordinate,
        engine="nautilus",
        engine_version=ENGINE_VERSION,
        python_requires=PYTHON_REQUIRES,
        entry_point_group=ENTRY_POINT_GROUP,
        entry_point_name=spec.entry_point,
        execution_abi_schema_coordinate=(
            f"{authority.authority_repository}/blob/{authority.authority_commit}/"
            "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json"
        ),
        execution_abi_schema_sha256=contract_authority.artifact_ref_schema_sha256,
        execution_abi_golden_coordinate=(
            f"{authority.authority_repository}/blob/{authority.authority_commit}/"
            "docs/authority/strategy-artifact-ref-v1.golden.json"
        ),
        execution_abi_golden_sha256=contract_authority.artifact_ref_golden_sha256,
        contract_asset_index_coordinate=by_role[ArtifactMemberRole.CONTRACT_SCHEMA].coordinate,
        contract_asset_index_sha256=contract_authority.asset_index_sha256,
        toolkit_coordinate=authority.oci_coordinate,
        toolkit_wheel_sha256=authority.nautilus_wheel.sha256,
        toolkit_sbom_sha256=authority.nautilus_sbom.sha256,
        strategy_sbom_sha256=by_role[ArtifactMemberRole.STRATEGY_SBOM].sha256,
        strategy_source_commit=producer_commit,
        strategy_source_tree_sha256=snapshot.source_tree_sha256,
        strategy_artifact_coordinate=by_role[ArtifactMemberRole.STRATEGY_WHEEL].coordinate,
        strategy_artifact_sha256=by_role[ArtifactMemberRole.STRATEGY_WHEEL].sha256,
        strategy_manifest_sha256=by_role[ArtifactMemberRole.STRATEGY_MANIFEST].sha256,
        build_lock_sha256=_sha256(build_lock),
        zero_rewrite_semantic_diff_sha256=_sha256(semantic_diff),
        zero_rewrite_characterization_sha256=_sha256(characterization),
        members=members,
    )
    product = produce_canonical_strategy_release_bom(
        bom,
        {
            ArtifactMemberRole.BASE_CONTRACTS_WHEEL: toolkit.base_contracts_wheel,
            ArtifactMemberRole.NAUTILUS_WHEEL: toolkit.nautilus_wheel,
            ArtifactMemberRole.STRATEGY_WHEEL: strategy_wheel,
            ArtifactMemberRole.STRATEGY_MANIFEST: strategy_manifest,
            ArtifactMemberRole.RUNTIME_ARTIFACT: config_schema,
            ArtifactMemberRole.TOOLKIT_SBOM: toolkit.nautilus_sbom,
            ArtifactMemberRole.STRATEGY_SBOM: strategy_sbom,
            ArtifactMemberRole.CONTRACT_SCHEMA: toolkit.contract_asset_index,
            ArtifactMemberRole.SOURCE_TREE: snapshot.canonical_bytes,
        },
        producer=CleanProducerEvidence(
            repository=producer.repository_url,
            commit=producer_commit,
            worktree_clean=True,
        ),
        custos_authority=contract_authority,
    )
    strategy_artifact_ref = _strategy_artifact_ref(
        artifact_coordinate=bom.strategy_artifact_coordinate,
        strategy_wheel=strategy_wheel,
        strategy_manifest=strategy_manifest,
        strategy_sbom=strategy_sbom,
        config_schema=config_schema,
        contract_schema_sha256=bom.execution_abi_schema_sha256,
        producer_commit=producer_commit,
        source_tree_sha256=bom.strategy_source_tree_sha256,
        toolkit_version=authority.candidate_version,
        build_lock=build_lock,
        source_repository=producer.repository_url,
    )
    predicate = StrategyReleasePredicateV1(
        schema_version=STRATEGY_RELEASE_PREDICATE_SCHEMA_VERSION,
        producer_repository=bom.producer_repository,
        producer_commit=producer_commit,
        workflow_identity=producer.workflow_identity,
        source_date_epoch=source_date_epoch,
        strategy_source_tree_sha256=bom.strategy_source_tree_sha256,
        execution_abi_schema_sha256=bom.execution_abi_schema_sha256,
        contract_asset_index_sha256=bom.contract_asset_index_sha256,
        toolkit_wheel_sha256=bom.toolkit_wheel_sha256,
        toolkit_sbom_sha256=bom.toolkit_sbom_sha256,
        strategy_sbom_sha256=bom.strategy_sbom_sha256,
        build_lock_sha256=bom.build_lock_sha256,
        zero_rewrite_semantic_diff_sha256=bom.zero_rewrite_semantic_diff_sha256,
        zero_rewrite_characterization_sha256=bom.zero_rewrite_characterization_sha256,
        engine=bom.engine,
        engine_version=bom.engine_version,
        python_requires=bom.python_requires,
        entry_point_group=bom.entry_point_group,
        entry_point_name=bom.entry_point_name,
    )
    statement = StrategyReleaseStatementV1(
        statement_type=IN_TOTO_STATEMENT_TYPE,
        subjects=(
            StatementSubjectV1("strategy-release-bom-v1", product.bom_sha256),
            StatementSubjectV1("strategy-artifact", bom.strategy_artifact_sha256),
            StatementSubjectV1("strategy-manifest-v1", bom.strategy_manifest_sha256),
            StatementSubjectV1("strategy-artifact-ref-v1", _sha256(strategy_artifact_ref)),
        ),
        predicate_type=STRATEGY_RELEASE_PREDICATE_TYPE,
        predicate=predicate,
    )
    statement_bytes = canonical_json_bytes(statement.to_mapping())
    release_layers = {
        "strategy_artifact": strategy_wheel,
        "strategy_manifest": strategy_manifest,
        "strategy_artifact_ref": strategy_artifact_ref,
        "strategy_release_bom": product.canonical_bytes,
        "strategy_sbom": strategy_sbom,
        "strategy_release_statement": statement_bytes,
    }
    gate_receipt = canonical_json_bytes(
        {
            "artifact_build_verified": True,
            "external_publication_completed": False,
            "manifest_verified": True,
            "producer_commit": producer_commit,
            "schema_version": PRODUCER_GATE_SCHEMA_VERSION,
            "statement_pre_sign_verified": True,
            "verify_before_publish": True,
            "zero_rewrite_characterization_sha256": _sha256(characterization),
            "zero_rewrite_semantic_diff_sha256": _sha256(semantic_diff),
            "zero_rewrite_verified": True,
        }
    )
    output_files: dict[str, bytes] = {"producer-gates-v1.json": gate_receipt}
    layer_specs: list[dict[str, object]] = []
    for role, media_type in RELEASE_LAYER_MEDIA_TYPES.items():
        content = release_layers[role]
        path = f"layers/{layer_names[role]}"
        output_files[path] = content
        layer_specs.append(
            {
                "media_type": media_type,
                "name": layer_names[role],
                "path": path,
                "role": role,
                "sha256": _sha256(content),
                "size_bytes": len(content),
            }
        )
    build_input = canonical_json_bytes(
        {
            "discovery_tag": discovery_tag,
            "producer_commit": producer_commit,
            "producer_gate_receipt": _file_spec("producer-gates-v1.json", gate_receipt),
            "producer_repository": producer.repository,
            "release_layers": layer_specs,
            "repository": producer.ghcr_repository,
            "schema_version": BUILD_INPUT_SCHEMA_VERSION,
            "strategy_coordinate": spec.coordinate,
        }
    )
    output_files[BUILD_INPUT_FILENAME] = build_input
    _atomic_materialize(output, output_files)
    return StrategyArtifactBuildResultV1(
        output=output.resolve(),
        strategy_wheel_sha256=_sha256(strategy_wheel),
        strategy_manifest_sha256=_sha256(strategy_manifest),
        strategy_artifact_ref_sha256=_sha256(strategy_artifact_ref),
        strategy_release_bom_sha256=product.bom_sha256,
        strategy_sbom_sha256=_sha256(strategy_sbom),
        strategy_release_statement_sha256=_sha256(statement_bytes),
        build_lock_sha256=_sha256(build_lock),
        zero_rewrite_semantic_diff_sha256=_sha256(semantic_diff),
        zero_rewrite_characterization_sha256=_sha256(characterization),
    )


__all__ = [
    "ArtifactBuildError",
    "ProducerIdentity",
    "StrategyReleaseSpec",
    "release_layer_names",
    "StrategyArtifactBuildResultV1",
    "StrategyTypingRequestV1",
    "StrategyTypingVerifier",
    "build_strategy_artifact_tree",
    "typing_receipt_bytes",
]

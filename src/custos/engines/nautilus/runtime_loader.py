"""Sole first-production V1 loader for verified Nautilus strategy artifacts."""

from __future__ import annotations

import hashlib
import importlib
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject,
    LocalStrategyExecutionContextV1,
    StrategyExecutionContextV1,
    StrategyRuntimeExecutionContextV1,
)

from custos.artifacts.archive import QuarantinedWheel, quarantine_wheel
from custos.artifacts.errors import ArtifactVerificationError
from custos.artifacts.local_context import LocalStrategyMaterialV1, bind_local_strategy_material
from custos.artifacts.policy import ArchiveLimitsV1
from custos.core.local_runtime_state import RuntimeLease
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.core.toolkit_identity import RuntimeToolkitSnapshot
from custos.local.authorization import VerifiedLocalRunAuthorization, _utc_instant


class NautilusRuntimeEntryPointError(RuntimeError):
    """A verified artifact could not satisfy the Nautilus runtime ABI."""


@dataclass(frozen=True, slots=True)
class LocalStrategyActivation:
    """An imported strategy and its private files, retained by the host until disposal."""

    strategy: object
    quarantine: QuarantinedWheel


def _strategy_registration_scope() -> AbstractContextManager[None]:
    """A strategy registry private to one load.

    A runner process loads one verified artifact after another, and two of them
    may register the same strategy name from different sources: a new version of
    a strategy, or two producers' strategies that share a name. Each load
    therefore registers into, and builds from, a registry of its own. Without
    the Nautilus toolkit installed no artifact can register a strategy, so there
    is no registry to scope.
    """

    try:
        from custos_toolkit_nautilus.adapter.registry import strategy_registration_scope
    except ModuleNotFoundError as error:
        if error.name != "custos_toolkit_nautilus":
            raise
        return nullcontext()
    return strategy_registration_scope()


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
        execution_context: StrategyRuntimeExecutionContextV1,
    ) -> object:
        if isinstance(execution_context, LocalStrategyExecutionContextV1):
            raise NautilusRuntimeEntryPointError(
                "execution context requires verified local compatibility before import"
            )
        if not isinstance(execution_context, StrategyExecutionContextV1):
            raise NautilusRuntimeEntryPointError("unsupported execution context type")
        return self._load(
            activation_root=activation_root,
            entry_point=entry_point,
            effective_config=effective_config,
            execution_context=execution_context,
        )

    def load_local(
        self,
        *,
        material: LocalStrategyMaterialV1,
        authorization: VerifiedLocalRunAuthorization,
        lease: RuntimeLease,
        effective_config_bytes: bytes,
        toolkit: RuntimeToolkitSnapshot,
        activation_parent: Path,
        limits: ArchiveLimitsV1,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        configure: Callable[[object], object] | None = None,
    ) -> LocalStrategyActivation:
        """Import local code from fresh verified bytes while the matching lease is held."""
        if not isinstance(authorization, VerifiedLocalRunAuthorization) or not isinstance(
            lease, RuntimeLease
        ):
            raise NautilusRuntimeEntryPointError(
                "local activation requires verified lease authority"
            )
        if not activation_parent.is_absolute() or activation_parent.resolve() != activation_parent:
            raise NautilusRuntimeEntryPointError("local activation parent must be canonical")
        run, permit = authorization.manifest, authorization.permit
        fence = lease.fence
        try:
            with lease.control_action():
                if (
                    fence.control_source != "local"
                    or run.run_id != fence.run_id
                    or permit.run_id != fence.run_id
                    or permit.manifest_digest != run.digest
                    or permit.control_registry_id != lease.control_registry_id
                    or authorization.permit_digest != lease.permit_digest
                    or permit.authorization_epoch != fence.authorization_epoch
                    or run.account_identity.scope_digest != fence.account_scope
                    or permit.account_scope != fence.account_scope
                ):
                    raise NautilusRuntimeEntryPointError(
                        "local permit differs from the active lease"
                    )
                config = bind_local_strategy_material(
                    material=material,
                    run=run,
                    effective_config_bytes=effective_config_bytes,
                    toolkit=toolkit,
                )
                content = material.strategy_wheel.verified_content
                if (
                    len(content) != run.artifact.artifact_size_bytes
                    or hashlib.sha256(content).hexdigest() != run.artifact.artifact_sha256
                ):
                    raise NautilusRuntimeEntryPointError("local wheel snapshot differs")
                isolated = quarantine_wheel(
                    content,
                    entry_point_group=material.manifest.entry_point_group,
                    entry_point=run.entry_point,
                    limits=limits,
                    quarantine_parent=activation_parent,
                    required_runtime_artifacts=tuple(
                        member.model_dump(mode="json")
                        for member in run.artifact.required_runtime_artifacts
                    ),
                )
                try:
                    lease.assert_current()
                    now = clock()
                    if now.tzinfo is None or not (
                        _utc_instant(permit.not_before_utc)
                        <= now
                        < _utc_instant(permit.expires_at_utc)
                    ):
                        raise NautilusRuntimeEntryPointError("local permit is not currently valid")
                    context = LocalStrategyExecutionContextV1(
                        control_source="local",
                        engine="nautilus",
                        trading_mode=run.account.trading_mode,
                        run_id=fence.run_id,
                        run_manifest_digest=run.digest,
                        account_scope=fence.account_scope,
                        effective_config_digest=run.effective_config_digest,
                        configuration_revision=run.configuration_revision,
                        authorization_epoch=fence.authorization_epoch,
                        process_generation=fence.process_generation,
                    )
                    strategy = self._load(
                        activation_root=isolated.root,
                        entry_point=isolated.verified_entry_point,
                        effective_config=config,
                        execution_context=context,
                        configure=configure,
                    )
                except BaseException:
                    isolated.destroy()
                    raise
                return LocalStrategyActivation(strategy, isolated)
        except (ArtifactVerificationError, RuntimeAuthorityError) as error:
            raise NautilusRuntimeEntryPointError("local activation evidence was refused") from error
        except Exception as error:
            if isinstance(error, NautilusRuntimeEntryPointError):
                raise
            raise NautilusRuntimeEntryPointError("local strategy activation failed") from error

    def _load(
        self,
        *,
        activation_root: Path,
        entry_point: str,
        effective_config: FrozenJsonObject,
        execution_context: StrategyRuntimeExecutionContextV1,
        configure: Callable[[object], object] | None = None,
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
            with _strategy_registration_scope():
                strategy = self._import_and_build(
                    root=root,
                    module_name=module_name,
                    attribute_name=attribute_name,
                    effective_config=effective_config,
                    execution_context=execution_context,
                    **({"configure": configure} if configure is not None else {}),
                )
            loaded = True
            return strategy
        finally:
            sys.path[:] = original_path
            if not loaded:
                for name in tuple(sys.modules):
                    if name == top_level_package or name.startswith(f"{top_level_package}."):
                        sys.modules.pop(name, None)
                sys.modules.update(previous_modules)

    @staticmethod
    def _import_and_build(
        *,
        root: Path,
        module_name: str,
        attribute_name: str,
        effective_config: FrozenJsonObject,
        execution_context: StrategyRuntimeExecutionContextV1,
        configure: Callable[[object], object] | None = None,
    ) -> object:
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
        if configure is not None:
            config = configure(config)
        strategy = build_strategy(config)
        if strategy is None:
            raise NautilusRuntimeEntryPointError("runtime adapter returned no strategy")
        return strategy


__all__ = [
    "NautilusRuntimeEntryPointError",
    "NautilusRuntimeEntryPointLoaderV1",
]

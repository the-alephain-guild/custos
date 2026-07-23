"""Shared durable activation boundary for verified strategy artifact bytes."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject,
    StrategyExecutionContextV1,
)

from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1


class DurableArtifactRuntimeState(Protocol):
    async def load_durable_desired_command(self, deployment_instance_id: UUID) -> Any: ...

    async def load_artifact_activation(self, **kwargs: Any) -> Mapping[str, Any] | None: ...

    async def stage_artifact_activation(self, **kwargs: Any) -> None: ...

    async def mark_artifact_activation_active(self, **kwargs: Any) -> None: ...

    async def quarantine_artifact_activation(self, **kwargs: Any) -> None: ...


class RuntimeEntryPointLoader(Protocol):
    def load(
        self,
        *,
        activation_root: Path,
        entry_point: str,
        effective_config: FrozenJsonObject,
        execution_context: StrategyExecutionContextV1,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ArtifactActivationCandidateV1:
    command: CrucibleRunnerDeploymentCommandV1
    activation_id: str
    quarantine_root: Path
    entry_point: str
    effective_config: FrozenJsonObject
    execution_context: StrategyExecutionContextV1
    artifact_identity_digest: str
    artifact_authority_digest: str


@dataclass(frozen=True, slots=True)
class ActivatedArtifactMaterializationV1:
    activation_root: Path
    strategy: object


class DurableArtifactActivatorV1:
    def __init__(self, *, state: DurableArtifactRuntimeState, activation_parent: Path) -> None:
        if not activation_parent.is_absolute():
            raise ValueError("runner-local activation parent must be absolute")
        self._state = state
        self._activation_parent = activation_parent

    async def activate(
        self,
        candidate: ArtifactActivationCandidateV1,
        *,
        loader: RuntimeEntryPointLoader,
    ) -> ActivatedArtifactMaterializationV1:
        command = candidate.command
        self._activation_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        activation_root = self._activation_parent / candidate.activation_id
        durable = await self._state.load_artifact_activation(
            command=command,
            activation_id=candidate.activation_id,
            artifact_identity_digest=candidate.artifact_identity_digest,
            artifact_authority_digest=candidate.artifact_authority_digest,
        )
        replay_active = durable is not None and durable["state"] == "active"
        if durable is not None and durable["state"] == "quarantined":
            raise RuntimeError("artifact activation is durably quarantined")
        if durable is None and activation_root.exists():
            raise RuntimeError("immutable activation path exists without matching durability")
        if replay_active and not activation_root.exists():
            await self._state.quarantine_artifact_activation(
                command=command,
                activation_id=candidate.activation_id,
                reason="active_activation_root_missing",
            )
            raise RuntimeError("durable active artifact has no immutable activation directory")
        if replay_active:
            shutil.rmtree(candidate.quarantine_root, ignore_errors=True)
        else:
            if durable is None:
                await self._state.stage_artifact_activation(
                    command=command,
                    activation_id=candidate.activation_id,
                    artifact_identity_digest=candidate.artifact_identity_digest,
                    artifact_authority_digest=candidate.artifact_authority_digest,
                )
            elif durable["state"] != "staged":
                raise RuntimeError("artifact activation has an invalid durable state")
            try:
                if not activation_root.exists():
                    os.replace(candidate.quarantine_root, activation_root)
                await self._state.mark_artifact_activation_active(
                    command=command,
                    activation_id=candidate.activation_id,
                )
                shutil.rmtree(candidate.quarantine_root, ignore_errors=True)
            except Exception as error:
                recovery_root = activation_root.with_name(
                    f"activation-failed-{candidate.activation_id}"
                )
                try:
                    if activation_root.exists() and not recovery_root.exists():
                        os.replace(activation_root, recovery_root)
                finally:
                    await self._state.quarantine_artifact_activation(
                        command=command,
                        activation_id=candidate.activation_id,
                        reason="durable_activation_commit_failed",
                    )
                raise RuntimeError("durable activation failed before Python import") from error

        try:
            strategy = loader.load(
                activation_root=activation_root,
                entry_point=candidate.entry_point,
                effective_config=candidate.effective_config,
                execution_context=candidate.execution_context,
            )
        except Exception as error:
            await self._state.quarantine_artifact_activation(
                command=command,
                activation_id=candidate.activation_id,
                reason="verified_entry_point_load_failed",
            )
            raise RuntimeError("verified entry point failed after durable activation") from error
        return ActivatedArtifactMaterializationV1(
            activation_root=activation_root,
            strategy=strategy,
        )


__all__ = [
    "ActivatedArtifactMaterializationV1",
    "ArtifactActivationCandidateV1",
    "DurableArtifactActivatorV1",
    "DurableArtifactRuntimeState",
    "RuntimeEntryPointLoader",
]

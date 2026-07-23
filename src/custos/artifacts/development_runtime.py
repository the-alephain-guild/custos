"""Sandbox-only runtime for pathless, Crucible-signed development snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from custos_toolkit.contracts.strategy_execution import (
    DevelopmentSourceRefV1,
    FrozenJsonObject,
    StrategyExecutionContextV1,
    canonical_json_digest,
    deep_freeze_json,
)

from custos.artifacts.activation import (
    ArtifactActivationCandidateV1,
    DurableArtifactActivatorV1,
    DurableArtifactRuntimeState,
    RuntimeEntryPointLoader,
)
from custos.artifacts.archive import quarantine_wheel
from custos.artifacts.development_source import (
    VerifiedDevelopmentArtifactV1,
    verify_development_artifact,
)
from custos.artifacts.policy import ArchiveLimitsV1
from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import DevelopmentArtifactSourceV1


class DevelopmentArtifactRuntimeBlocked(RuntimeError):
    """The configured local artifact store is not currently available."""


@dataclass(frozen=True, slots=True)
class DevelopmentArtifactRuntimeConfigV1:
    artifact_root: Path
    quarantine_parent: Path
    activation_parent: Path
    archive_limits: ArchiveLimitsV1

    def __post_init__(self) -> None:
        if any(
            not path.is_absolute()
            for path in (self.artifact_root, self.quarantine_parent, self.activation_parent)
        ):
            raise ValueError("development artifact runtime paths must be absolute")


@dataclass(frozen=True, slots=True)
class DevelopmentArtifactVerificationReceiptV1:
    source_sha256: str
    publication_receipt_digest: str
    snapshot_digest: str
    verified_at: datetime
    artifact_policy_id: str = "custos-sandbox-development-source-v1"
    promotable: bool = False


@dataclass(frozen=True, slots=True)
class PreparedDevelopmentStrategyArtifact:
    command: CrucibleRunnerDeploymentCommandV1
    activation_id: str
    receipt: DevelopmentArtifactVerificationReceiptV1
    verified_artifact: VerifiedDevelopmentArtifactV1
    quarantine_root: Path
    verified_entry_point: str
    effective_config: FrozenJsonObject
    execution_context: StrategyExecutionContextV1


@dataclass(frozen=True, slots=True)
class ActivatedDevelopmentStrategyArtifact:
    prepared: PreparedDevelopmentStrategyArtifact
    activation_root: Path
    strategy: object

    @property
    def activation_id(self) -> str:
        return self.prepared.activation_id


class DevelopmentStrategyArtifactRuntimeV1:
    def __init__(
        self,
        *,
        state: DurableArtifactRuntimeState,
        config: DevelopmentArtifactRuntimeConfigV1,
    ) -> None:
        self._state = state
        self._config = config
        self._activator = DurableArtifactActivatorV1(
            state=state,
            activation_parent=config.activation_parent,
        )

    async def prepare(self, *, deployment_instance_id: UUID) -> PreparedDevelopmentStrategyArtifact:
        if not self._config.artifact_root.is_dir():
            raise DevelopmentArtifactRuntimeBlocked("development artifact root is unavailable")
        durable = await self._state.load_durable_desired_command(deployment_instance_id)
        command = durable.command
        if not isinstance(command, CrucibleRunnerDeploymentCommandV1):
            raise ValueError("development runtime requires the durable V1 command")
        source = command.artifact_source
        if not isinstance(source, DevelopmentArtifactSourceV1):
            raise ValueError("development runtime received a StrategyRelease command")
        snapshot = source.snapshot
        source_path = self._config.artifact_root / "sha256" / snapshot.source_sha256
        source_ref = DevelopmentSourceRefV1(
            schema_version=1,
            source_path=str(source_path),
            source_sha256=snapshot.source_sha256,
            trading_mode="sandbox",
            promotable=False,
        )
        try:
            verified_artifact = verify_development_artifact(
                source_ref,
                publication_receipt_digest=snapshot.publication_receipt_digest,
                configured_root=self._config.artifact_root,
                runtime_mode=command.mode,
            )
        except FileNotFoundError as error:
            raise DevelopmentArtifactRuntimeBlocked(
                "development artifact material is unavailable"
            ) from error
        quarantined = quarantine_wheel(
            verified_artifact.strategy_wheel_path,
            entry_point_group=verified_artifact.entry_point_group,
            entry_point=verified_artifact.entry_point,
            limits=self._config.archive_limits,
            quarantine_parent=self._config.quarantine_parent,
        )
        runtime_spec = command.to_runtime_spec()
        frozen = deep_freeze_json(runtime_spec.strategy_config)
        if not isinstance(frozen, dict) and not hasattr(frozen, "items"):
            raise ValueError("effective development configuration is not an object")
        effective_config = cast(FrozenJsonObject, frozen)
        execution_context = StrategyExecutionContextV1(
            engine="nautilus",
            trading_mode=command.trading_mode,
            deployment_instance_id=command.deployment_instance_id,
            deployment_spec_id=command.deployment_spec_id,
            deployment_spec_digest=command.deployment_spec_digest,
            effective_config_digest=canonical_json_digest(runtime_spec.strategy_config),
            generation=command.generation,
        )
        activation_id = str(
            uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "custos-development-artifact-activation-v1",
                        str(command.deployment_instance_id),
                        str(command.deployment_spec_id),
                        str(command.generation),
                        snapshot.source_sha256,
                        snapshot.snapshot_digest,
                    )
                ),
            )
        )
        return PreparedDevelopmentStrategyArtifact(
            command=command,
            activation_id=activation_id,
            receipt=DevelopmentArtifactVerificationReceiptV1(
                source_sha256=snapshot.source_sha256,
                publication_receipt_digest=snapshot.publication_receipt_digest,
                snapshot_digest=snapshot.snapshot_digest,
                verified_at=datetime.now(UTC),
            ),
            verified_artifact=verified_artifact,
            quarantine_root=quarantined.root,
            verified_entry_point=quarantined.verified_entry_point,
            effective_config=effective_config,
            execution_context=execution_context,
        )

    async def activate(
        self,
        prepared: PreparedDevelopmentStrategyArtifact,
        *,
        loader: RuntimeEntryPointLoader,
    ) -> ActivatedDevelopmentStrategyArtifact:
        materialized = await self._activator.activate(
            ArtifactActivationCandidateV1(
                command=prepared.command,
                activation_id=prepared.activation_id,
                quarantine_root=prepared.quarantine_root,
                entry_point=prepared.verified_entry_point,
                effective_config=prepared.effective_config,
                execution_context=prepared.execution_context,
                artifact_identity_digest=prepared.receipt.source_sha256,
                artifact_authority_digest=prepared.receipt.snapshot_digest,
            ),
            loader=loader,
        )
        return ActivatedDevelopmentStrategyArtifact(
            prepared=prepared,
            activation_root=materialized.activation_root,
            strategy=materialized.strategy,
        )


__all__ = [
    "ActivatedDevelopmentStrategyArtifact",
    "DevelopmentArtifactRuntimeBlocked",
    "DevelopmentArtifactRuntimeConfigV1",
    "DevelopmentArtifactVerificationReceiptV1",
    "DevelopmentStrategyArtifactRuntimeV1",
    "PreparedDevelopmentStrategyArtifact",
]

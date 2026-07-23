"""Post-parse daemon runtime — reconciler + snapshot publisher + heartbeat.

Extracted from the legacy flat CLI ``_run`` coroutine so both the
``arx-runner start`` subcommand and any future embedding call site can
enter the runtime by handing in a compatible ``argparse.Namespace``.

Startup verifies the age-encrypted machine principal against Crucible
authority before connecting NATS.  Missing, expired, revoked, or mismatched
authority therefore cannot leave a stale ready file or start execution.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import logging
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custos.artifacts.development_runtime import (
    DevelopmentArtifactRuntimeConfigV1,
    DevelopmentStrategyArtifactRuntimeV1,
)
from custos.artifacts.immutable_material import (
    HttpOciBlobTransportV1,
    RegistryPullCredentialV1,
    RegistryStrategyReleaseMaterializerV1,
)
from custos.artifacts.policy import ArchiveLimitsV1
from custos.artifacts.release_resolver import (
    CrucibleStrategyReleaseArtifactResolverV1,
    StrategyReleaseArtifactResolverV1,
    UnavailableStrategyReleaseArtifactResolverV1,
)
from custos.artifacts.runtime import (
    ArtifactRuntimeCapabilityV1,
    ArtifactRuntimeConfigV1,
    StrategyArtifactRuntimeV1,
)
from custos.artifacts.sigstore_verifier import ProductionSigstoreVerifier
from custos.artifacts.verification_types import RunnerLocalArtifactVerificationConfig
from custos.contracts.crucible_runner_safety_policy import (
    CrucibleRunnerSafetyPolicyAuthenticator,
)
from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.engine_lifecycle import EngineLifecycleConfig, EngineLifecycleSupervisor
from custos.core.engine_protocol import ExecutionEngineProtocol
from custos.core.machine_credential_vault import (
    MachineCredentialError,
    MachineCredentialHttpClient,
    MachineCredentialRejectedError,
    MachineCredentialTransportError,
    MachineCredentialVault,
)
from custos.core.nats_client import CrucibleNatsClient
from custos.core.nats_transport import (
    DevelopmentLocalNatsConnectionProfile,
    RunnerNatsConnectionProfile,
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportError,
    RunnerNatsTransportSet,
    runner_nats_transport_domain,
)
from custos.core.per_key_vault import PerKeyVault
from custos.core.readiness import ReadinessFile
from custos.core.runner_command_intake import (
    CommandDeliveryPolicy,
    CommandIntakeCoordinator,
    CrucibleRunnerCommandAuthenticator,
    VerifiedRunnerCommand,
)
from custos.core.runner_command_runtime import RunnerCommandRuntimeCoordinator
from custos.core.runner_control_consumer import RunnerControlConsumerV1
from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
    RunnerStateStore,
)
from custos.core.runner_fact_producer import RunnerFactHost, RunnerFactProductionLoop
from custos.core.runner_material_authority import RunnerMaterialAuthorityClient
from custos.core.runner_safety_policy import (
    DurableRunnerSafetyPolicyResolver,
    RunnerSafetyPolicyResolver,
)
from custos.core.runner_toml import RunnerToml
from custos.engines.nautilus.runtime_loader import NautilusRuntimeEntryPointLoaderV1

log = logging.getLogger("custos")

_AVAILABLE_ENGINES = {"nautilus", "sandbox-sim"}


class RunnerExecutionHost(ExecutionEngineProtocol, RunnerFactHost, Protocol):
    """One host used by both the execution and signed-fact runtime paths."""


def _load_ed25519_public_key(path: Path, *, label: str) -> Ed25519PublicKey:
    """Load the sole V1 format: canonical base64 of 32 raw Ed25519 bytes."""

    encoded = path.expanduser().resolve(strict=True).read_text(encoding="ascii").strip()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} is not canonical base64") from exc
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError(f"{label} must encode exactly 32 Ed25519 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def _build_strategy_release_runtime(
    *,
    args: argparse.Namespace,
    state_store: RunnerStateStore,
    material_authority: RunnerMaterialAuthorityClient,
) -> tuple[StrategyReleaseArtifactResolverV1, StrategyArtifactRuntimeV1 | None]:
    required = {
        "release policy envelope": args.artifact_release_policy_envelope,
        "release policy key id": str(args.artifact_release_policy_key_id).strip(),
        "release policy public key": args.artifact_release_policy_public_key,
        "Sigstore trusted root": args.artifact_sigstore_trusted_root,
    }
    configured = {name: bool(value) for name, value in required.items()}
    if not any(configured.values()):
        return UnavailableStrategyReleaseArtifactResolverV1(), None
    missing = sorted(name for name, present in configured.items() if not present)
    if missing:
        raise ValueError(
            "production StrategyRelease configuration is incomplete: "
            + ", ".join(missing)
        )
    username = str(args.artifact_registry_username).strip()
    token = str(args.artifact_registry_token).strip()
    if bool(username) != bool(token):
        raise ValueError("artifact registry username and token must be configured together")
    registry = str(args.artifact_registry).strip().lower()
    credentials = (
        {registry: RegistryPullCredentialV1(username=username, token=token)}
        if username
        else {}
    )
    transport = HttpOciBlobTransportV1(
        allowed_registries=(registry,),
        credentials=credentials,
    )
    materializer = RegistryStrategyReleaseMaterializerV1(
        cache_root=args.artifact_cache_dir.expanduser().resolve(),
        transport=transport,
    )
    resolver = CrucibleStrategyReleaseArtifactResolverV1(
        authority=material_authority,
        materializer=materializer,
    )
    policy_envelope = args.artifact_release_policy_envelope.expanduser().resolve(strict=True)
    policy_public_key = args.artifact_release_policy_public_key.expanduser().resolve(strict=True)
    trusted_root = args.artifact_sigstore_trusted_root.expanduser().resolve(strict=True)
    runtime = StrategyArtifactRuntimeV1(
        state=state_store,
        config=ArtifactRuntimeConfigV1(
            local_verification=RunnerLocalArtifactVerificationConfig(
                signed_policy_envelope_bytes=policy_envelope.read_bytes(),
                policy_authority_key_id=str(args.artifact_release_policy_key_id).strip(),
                policy_authority_public_key=_load_ed25519_public_key(
                    policy_public_key,
                    label="artifact release policy public key",
                ),
                sigstore_trusted_root_bytes=trusted_root.read_bytes(),
                quarantine_parent=args.artifact_quarantine_dir.expanduser().resolve(),
            ),
            activation_parent=args.artifact_activation_dir.expanduser().resolve(),
            capability=ArtifactRuntimeCapabilityV1.production_ready(),
        ),
        sigstore_verifier=ProductionSigstoreVerifier(),
    )
    return resolver, runtime


def _runner_fact_authority(
    capability: RunnerCapabilityReceipt,
    verified: Any,
) -> RunnerFactAuthority:
    command = verified.command
    capability.require_scope_bindings(
        projectors=("deployment_lifecycle",),
        trading_mode=command.trading_mode,
        deployment_instance_id=command.deployment_instance_id,
        deployment_spec_id=command.deployment_spec_id,
        deployment_spec_digest=command.deployment_spec_digest,
        strategy_id=command.strategy_id,
    )
    return RunnerFactAuthority(
        tenant_id=command.tenant_id,
        trading_mode=command.trading_mode,
        runner_id=command.runner_id,
        deployment_instance_id=command.deployment_instance_id,
        deployment_spec_id=command.deployment_spec_id,
        deployment_spec_digest=command.deployment_spec_digest,
        generation=command.generation,
        strategy_id=command.strategy_id,
        capability_version_id=capability.capability_version_id,
        capability_version=capability.capability_version,
        capability_manifest_digest=capability.manifest_digest,
    )


async def _watch_machine_authority(
    stop: asyncio.Event,
    *,
    backend_url: str,
    machine_credential: object,
    local_check_secs: float = 1.0,
    remote_check_secs: float = 30.0,
) -> None:
    """Stop on explicit invalidation while tolerating transport outages."""
    authority = MachineCredentialHttpClient(backend_url, machine_credential)  # type: ignore[arg-type]
    elapsed = 0.0
    while not stop.is_set():
        try:
            machine_credential.assert_active()  # type: ignore[attr-defined]
        except MachineCredentialError as exc:
            log.error(
                "machine_authority_invalidated",
                extra={"error_type": type(exc).__name__},
            )
            stop.set()
            return
        elapsed += local_check_secs
        if elapsed >= remote_check_secs:
            elapsed = 0.0
            try:
                await asyncio.to_thread(authority.verify_active)
            except MachineCredentialTransportError as exc:
                log.warning(
                    "machine_authority_check_unavailable",
                    extra={"error_type": type(exc).__name__},
                )
            except (MachineCredentialRejectedError, MachineCredentialError) as exc:
                log.error(
                    "machine_authority_rejected",
                    extra={"error_type": type(exc).__name__},
                )
                stop.set()
                return
        try:
            await asyncio.wait_for(stop.wait(), timeout=local_check_secs)
        except TimeoutError:
            pass


async def _watch_nats_transport_authority(
    stop: asyncio.Event,
    profiles: dict[str, RunnerNatsConnectionProfile],
    *,
    check_secs: float = 1.0,
) -> None:
    """Stop execution on local expiry or broker authorization denial."""

    while not stop.is_set():
        for trading_mode, profile in profiles.items():
            try:
                profile.assert_active()
            except RunnerNatsTransportError as exc:
                log.error(
                    "nats_transport_authority_invalidated",
                    extra={
                        "trading_mode": trading_mode,
                        "error_type": type(exc).__name__,
                    },
                )
                stop.set()
                return
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_secs)
        except TimeoutError:
            pass


def _build_vault(args: argparse.Namespace) -> PerKeyVault:
    """Build the runtime vault reader.

    Always ``PerKeyVault``; no ``MockVault`` runtime fallback. Dev/paper
    users must provision at least one credential via ``arx-runner vault
    put`` before ``arx-runner start`` — the reconciler only ever calls
    ``decrypt`` for a spec that actually references a credential_id, so
    a runner that never runs a live spec never opens the vault.
    """
    return PerKeyVault(
        vault_dir=args.vault_dir,
        tenant_id=args.tenant_id,
        initiator=args.runner_id,
    )


def _build_host(
    args: argparse.Namespace,
    *,
    fact_emitter: RunnerFactEmitter | None = None,
    capability_receipt: RunnerCapabilityReceipt | None = None,
    runner_safety_boundary_factory=None,
) -> RunnerExecutionHost:
    """Pick the execution engine host from the clean-break ``--engine`` enum.

    ``nautilus`` selects the real ``NtTradingNodeHost`` and ``sandbox-sim``
    selects the deterministic sandbox-only simulator. Execution admission rejects
    testnet and live before the simulator can deploy.
    """
    engine = getattr(args, "engine", "nautilus")
    if engine not in _AVAILABLE_ENGINES:
        raise SystemExit(
            f"engine {engine!r} is not available "
            f"(available: {', '.join(sorted(_AVAILABLE_ENGINES))})"
        )
    if engine == "nautilus":
        from custos.engines.nautilus.host import NtTradingNodeHost

        return NtTradingNodeHost(
            tenant_id=args.tenant_id,
            runner_id=args.runner_id,
            runner_fact_emitter=fact_emitter,
            capability_receipt=capability_receipt,
            runner_safety_boundary_factory=runner_safety_boundary_factory,
        )
    if engine == "sandbox-sim":
        from custos.engines.nautilus.sandbox_runner_fact_host import (
            SandboxRunnerFactHost,
        )

        return SandboxRunnerFactHost(
            tenant_id=args.tenant_id,
            capability_receipt=capability_receipt,
        )
    raise SystemExit(f"unhandled engine {engine!r}")


async def _recover_durable_running_commands(
    *,
    state_store,
    command_runtime,
    capability,
) -> None:
    identities = await state_store.list_recoverable_desired_command_identities()
    for identity in identities:
        projectors = ["settlement", "risk", "health"]
        if identity.trading_mode in {"testnet", "live"}:
            projectors.append("reconciliation")
        try:
            capability.require_scope_bindings(
                projectors=projectors,
                trading_mode=identity.trading_mode,
                deployment_instance_id=identity.deployment_instance_id,
                deployment_spec_id=identity.deployment_spec_id,
                deployment_spec_digest=identity.deployment_spec_digest,
                strategy_id=identity.strategy_id,
            )
        except Exception as exc:  # noqa: BLE001 - stale local state is not current authority
            log.info(
                "durable_command_recovery_skipped",
                extra={
                    "deployment_instance_id": str(identity.deployment_instance_id),
                    "reason": "not_bound_by_current_capability",
                    "error_type": type(exc).__name__,
                },
            )
            continue
        durable = await state_store.load_durable_desired_command(identity.deployment_instance_id)
        command = durable.command
        verified = VerifiedRunnerCommand(
            command=command,
            command_fingerprint=durable.command_fingerprint,
            verification_receipt=durable.verification_receipt,
        )
        await command_runtime.recover(verified)
        log.info(
            "durable_command_recovered",
            extra={
                "deployment_instance_id": str(command.deployment_instance_id),
                "generation": command.generation,
            },
        )


def _build_runner_safety_boundary_factory(
    *,
    state_store,
    safety_policy_resolver: RunnerSafetyPolicyResolver,
):
    async def build(spec: dict):
        limits = await safety_policy_resolver.resolve(str(spec["trading_mode"]))
        if not limits.owner_policy or limits.policy_id is None:
            raise RuntimeError("runner safety execution requires a durable verified owner policy")
        from custos.core.fallback_breaker import FallbackBreaker
        from custos.engines.nautilus.runner_safety import RunnerReservationBoundary

        return RunnerReservationBoundary(
            store=state_store,
            deployment_instance_id=UUID(str(spec["deployment_instance_id"])),
            policy_id=limits.policy_id,
            fallback_breaker=FallbackBreaker(limits.breaker),
        )

    return build


async def _supervise_long_running_tasks(tasks: list[asyncio.Task], stop: asyncio.Event) -> None:
    """Fail the daemon when a long-running task exits before an intentional stop."""

    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    failure: BaseException | None = None
    failed_name = "unnamed"
    for task in done:
        failed_name = task.get_name()
        if task.cancelled():
            if not stop.is_set():
                failure = RuntimeError("long-running task was unexpectedly cancelled")
            continue
        task_error = task.exception()
        if task_error is not None:
            failure = task_error
            break
        if not stop.is_set():
            failure = RuntimeError("long-running task exited unexpectedly")
            break
    stop.set()
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if failure is not None:
        raise RuntimeError(f"long-running task {failed_name!r} failed: {failure}") from failure


async def _shutdown_in_order(
    *,
    stop: asyncio.Event,
    tasks: list[asyncio.Task],
    host: object | None,
    fact_outbox: object,
    fact_publisher: object,
    clients: Mapping[str, object],
) -> None:
    """Stop intake/tasks, stop deployments, flush facts, then close transports."""

    stop.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        close_host = getattr(host, "close", None)
        if callable(close_host):
            await close_host()
        try:
            await fact_publisher.drain_once()  # type: ignore[attr-defined]
            for _ in range(7):
                if not await fact_outbox.pending():  # type: ignore[attr-defined]
                    break
                if await fact_publisher.drain_once() == 0:  # type: ignore[attr-defined]
                    break
            if await fact_outbox.pending():  # type: ignore[attr-defined]
                log.warning("runner_fact_shutdown_flush_incomplete")
        except Exception as exc:  # noqa: BLE001 - durable rows remain for restart
            log.warning(
                "runner_fact_shutdown_flush_failed",
                extra={"error_type": type(exc).__name__},
            )
    finally:
        try:
            await fact_publisher.close()  # type: ignore[attr-defined]
        finally:
            await asyncio.gather(
                *(client.close() for client in clients.values()),  # type: ignore[attr-defined]
                return_exceptions=True,
            )


def _transport_profile_for_mode(
    args: argparse.Namespace,
    credential: object,
) -> RunnerNatsConnectionProfile:
    trading_mode = credential.trading_mode  # type: ignore[attr-defined]
    domain = runner_nats_transport_domain(trading_mode)
    prefix = "nats_live" if domain == "live" else "nats_sim"
    nats_url = str(getattr(args, f"{prefix}_url", "")).strip()
    server_name = str(getattr(args, f"{prefix}_server_name", "")).strip()
    issuer_public_key = str(getattr(args, f"{prefix}_issuer_public_key", "")).strip()
    if not nats_url or not server_name or not issuer_public_key:
        raise RunnerNatsTransportError(
            f"{domain.upper()} NATS endpoint, server name and issuer pin are required"
        )
    return RunnerNatsTransportConnectionProfile(
        credential=credential,  # type: ignore[arg-type]
        nats_url=nats_url,
        ca_path=getattr(args, f"{prefix}_ca"),
        server_name=server_name,
        pinned_issuer_public_key=issuer_public_key,
    )


async def _publish_runtime_readiness(
    stop: asyncio.Event,
    *,
    readiness: ReadinessFile,
    fact_outbox: RunnerFactOutbox,
    clients: dict[str, CrucibleNatsClient],
    fact_publisher: RunnerFactJetStreamPublisher,
    deployment_subscription: bool,
    interval_seconds: float = 10.0,
) -> None:
    """Refresh one fail-closed operational projection from owner state."""

    while not stop.is_set():
        metrics = await fact_outbox.runtime_metrics()
        transport_modes = {
            mode: client.is_connected and fact_publisher.is_mode_connected(mode)
            for mode, client in clients.items()
        }
        readiness.mark_ready(
            strategy_id=None,
            nats_connected=bool(transport_modes) and all(transport_modes.values()),
            deployment_subscription=deployment_subscription,
            transport_modes=transport_modes,
            runtime_metrics=metrics.to_dict(),
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def run_daemon(args: argparse.Namespace) -> int:
    """Start signed-command reconciliation and signed RunnerFact publication.

    Body is verbatim from the pre-Plan-11 flat CLI ``_run`` coroutine
    aside from the vault selection (see ``_build_vault``).
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.ready_file.expanduser().resolve().unlink(missing_ok=True)
    metadata = RunnerToml.read(args.runner_toml_path)
    machine_credential = MachineCredentialVault(args.machine_vault).load()
    machine_credential.assert_binding(metadata)
    MachineCredentialHttpClient(metadata.backend_url, machine_credential).verify_active()
    development_local_nats_url = str(getattr(args, "development_local_nats_url", "")).strip()
    if development_local_nats_url:
        if tuple(args.enabled_modes) != ("sandbox",):
            raise RunnerNatsTransportError(
                "development local NATS requires exactly one sandbox mode session"
            )
        if any(
            str(getattr(args, field, "")).strip()
            for field in (
                "nats_sim_url",
                "nats_sim_server_name",
                "nats_sim_issuer_public_key",
                "nats_live_url",
                "nats_live_server_name",
                "nats_live_issuer_public_key",
            )
        ):
            raise RunnerNatsTransportError(
                "development local NATS cannot be combined with production endpoints"
            )
        transport_profiles: dict[str, RunnerNatsConnectionProfile] = {
            "sandbox": DevelopmentLocalNatsConnectionProfile(
                tenant_id=args.tenant_id,
                runner_id=UUID(args.runner_id),
                nats_url=development_local_nats_url,
            )
        }
        log.warning(
            "development_local_nats_enabled",
            extra={"trading_mode": "sandbox", "promotable": False},
        )
    else:
        transport_set = RunnerNatsTransportSet.load(
            args.nats_transport_vault_dir,
            args.enabled_modes,
        )
        transport_profiles = {
            mode: _transport_profile_for_mode(args, transport_set.active(mode))
            for mode in args.enabled_modes
        }
    identity = RunnerFactIdentity.from_private_bytes(
        machine_credential.private_key_bytes,
        machine_credential.machine_key_id,
    )
    capability = RunnerCapabilityReceipt.load(args.runner_capability)
    runner_id = UUID(args.runner_id)
    if capability.tenant_id != args.tenant_id or capability.runner_id != runner_id:
        raise RuntimeError("Runner capability receipt identity does not match runner.toml")
    if capability.key_id != identity.key_id:
        raise RuntimeError("Runner capability receipt key_id does not match local identity")
    public_key_digest = hashlib.sha256(identity.public_key_bytes).hexdigest()
    if capability.public_key_digest != public_key_digest:
        raise RuntimeError("Runner capability receipt public key does not match local identity")
    if capability.binding_status != "validated":
        raise RuntimeError(
            "Runner capability bindings are not validated; restart after projection completes"
        )
    fact_outbox = RunnerFactOutbox(args.runner_fact_outbox)
    fact_publisher = RunnerFactJetStreamPublisher(
        connection_profiles=transport_profiles,
        outbox=fact_outbox,
        runner_id=runner_id,
        authority_guard=machine_credential.assert_active,
    )
    clients = {
        mode: CrucibleNatsClient(
            connection_profile=profile,
            tenant_id=args.tenant_id,
            runner_id=args.runner_id,
            machine_credential=machine_credential,
        )
        for mode, profile in transport_profiles.items()
    }
    readiness = ReadinessFile(
        args.ready_file,
        tenant_id=args.tenant_id,
        runner_id=args.runner_id,
        credential_id=str(machine_credential.credential_id),
        credential_version=machine_credential.credential_version,
        credential_valid_until=metadata.credential_valid_until,
        machine_key_id=machine_credential.machine_key_id,
    )
    readiness.clear()
    await asyncio.gather(
        *(client.connect() for client in clients.values()),
        *(fact_publisher.connect(mode) for mode in clients),
    )
    log.info(
        "runner_started",
        extra={"tenant_id": args.tenant_id, "runner_id": args.runner_id},
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks: list[asyncio.Task] = []
    host: object | None = None
    try:
        tasks.append(
            asyncio.create_task(
                _watch_machine_authority(
                    stop,
                    backend_url=metadata.backend_url,
                    machine_credential=machine_credential,
                ),
                name="runner-machine-authority-watch",
            )
        )
        tasks.append(
            asyncio.create_task(
                _watch_nats_transport_authority(stop, transport_profiles),
                name="runner-nats-transport-authority-watch",
            )
        )
        tasks.append(
            asyncio.create_task(
                fact_publisher.run(stop),
                name="crucible-runner-fact-publisher",
            )
        )
        deployment_subscription = False
        if args.reconcile:
            key_id = str(args.crucible_domain_key_id).strip()
            if not key_id:
                raise ValueError("Crucible domain key id is required for reconciliation")
            domain_public_key = _load_ed25519_public_key(
                args.crucible_domain_public_key,
                label="Crucible domain public key",
            )
            signature_keys = {key_id: domain_public_key}
            delivery_policy = CommandDeliveryPolicy()
            command_authenticator = CrucibleRunnerCommandAuthenticator(
                expected_tenant_id=args.tenant_id,
                expected_runner_id=runner_id,
                allowed_trading_modes=frozenset(args.enabled_modes),
                signature_keys=signature_keys,
            )
            state_store = RunnerStateStore(
                outbox=fact_outbox,
                identity=identity,
                tenant_id=args.tenant_id,
                runner_id=runner_id,
                authority_resolver=lambda verified: _runner_fact_authority(capability, verified),
            )
            intake = CommandIntakeCoordinator(
                authenticator=command_authenticator,
                durability=state_store,
                policy=delivery_policy,
            )
            fact_emitter = RunnerFactEmitter(
                fact_outbox,
                identity,
                machine_credential.assert_active,
            )
            safety_policy_resolver = DurableRunnerSafetyPolicyResolver(state_store)
            host = _build_host(
                args,
                fact_emitter=fact_emitter,
                capability_receipt=capability,
                runner_safety_boundary_factory=_build_runner_safety_boundary_factory(
                    state_store=state_store,
                    safety_policy_resolver=safety_policy_resolver,
                ),
            )
            artifact_capability = ArtifactRuntimeCapabilityV1.production_ready()
            lifecycle = EngineLifecycleSupervisor(
                engine=host,
                state_store=state_store,
                artifact_capability=artifact_capability,
                config=EngineLifecycleConfig(live_execution_enabled=False),
            )
            material_authority = RunnerMaterialAuthorityClient(
                metadata.backend_url,
                machine_credential,
            )
            development_runtime = DevelopmentStrategyArtifactRuntimeV1(
                state=state_store,
                material_resolver=material_authority,
                config=DevelopmentArtifactRuntimeConfigV1(
                    artifact_root=args.development_artifact_root.expanduser().resolve(),
                    quarantine_parent=args.artifact_quarantine_dir.expanduser().resolve(),
                    activation_parent=args.artifact_activation_dir.expanduser().resolve(),
                    archive_limits=ArchiveLimitsV1(),
                ),
            )
            release_resolver, strategy_artifact_runtime = _build_strategy_release_runtime(
                args=args,
                state_store=state_store,
                material_authority=material_authority,
            )
            command_runtime = RunnerCommandRuntimeCoordinator(
                intake=intake,
                durability=state_store,
                release_resolver=release_resolver,
                artifact_runtime=strategy_artifact_runtime,
                development_artifact_runtime=development_runtime,
                entry_point_loader=NautilusRuntimeEntryPointLoaderV1(),
                credential_resolver=VaultRunnerCredentialResolverV1(_build_vault(args)),
                engine_lifecycle=lifecycle,
                delivery_policy=delivery_policy,
            )
            await _recover_durable_running_commands(
                state_store=state_store,
                command_runtime=command_runtime,
                capability=capability,
            )
            fact_production = RunnerFactProductionLoop(
                host=host,
                emitter=fact_emitter,
                snapshot_interval_secs=args.runner_fact_snapshot_interval_secs,
                period_secs=args.runner_fact_period_secs,
                period_retry_secs=args.runner_fact_period_retry_secs,
            )
            tasks.extend(
                (
                    asyncio.create_task(
                        fact_production.run_observability(stop),
                        name="runner-fact-observability",
                    ),
                    asyncio.create_task(
                        fact_production.run_periods(stop),
                        name="runner-fact-periods",
                    ),
                )
            )
            control_consumer = RunnerControlConsumerV1(
                command_runtime=command_runtime,
                policy_authenticator=CrucibleRunnerSafetyPolicyAuthenticator(
                    expected_tenant_id=args.tenant_id,
                    expected_runner_id=runner_id,
                    allowed_trading_modes=frozenset(args.enabled_modes),
                    signature_keys=signature_keys,
                ),
                state_store=state_store,
            )
            subscriptions = {
                mode: await clients[mode].subscribe_control() for mode in args.enabled_modes
            }
            for mode, subscription in subscriptions.items():
                tasks.append(
                    asyncio.create_task(
                        control_consumer.run(
                            client=clients[mode],
                            subscription=subscription,
                            stop=stop,
                        ),
                        name=f"crucible-runner-control-{mode}",
                    )
                )
            deployment_subscription = True
        tasks.append(
            asyncio.create_task(
                _publish_runtime_readiness(
                    stop,
                    readiness=readiness,
                    fact_outbox=fact_outbox,
                    clients=clients,
                    fact_publisher=fact_publisher,
                    deployment_subscription=deployment_subscription,
                ),
                name="runner-runtime-readiness",
            )
        )

        await _supervise_long_running_tasks(tasks, stop)
    finally:
        readiness.clear()
        await _shutdown_in_order(
            stop=stop,
            tasks=tasks,
            host=host,
            fact_outbox=fact_outbox,
            fact_publisher=fact_publisher,
            clients=clients,
        )
        log.info("runner_stopped")
    return 0

"""Inspect material for a prospective local Custos run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from custos.core.toolkit_identity import TOOLKIT_IDENTITY_FILE

_MAX_LOCAL_INPUT_BYTES = 128 * 1024


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("local", help="Inspect material for an independent local run.")
    actions = parser.add_subparsers(dest="local_action", required=True)
    prepare = actions.add_parser(
        "prepare", help="Check pinned strategy material and configuration without running it."
    )
    _register_material(prepare)
    prepare.set_defaults(handler=_prepare)
    initialize = actions.add_parser(
        "simulation-init", help="Initialize a private state directory for a simulated venue."
    )
    initialize.add_argument("--state-root", type=Path, required=True)
    initialize.set_defaults(handler=_simulation_init)
    start = actions.add_parser("start", help="Run a signed local strategy on a loopback venue.")
    _register_material(start)
    for name in ("state-root", "permit", "operator-public-key", "simulation-session-file"):
        start.add_argument("--" + name, type=Path, required=True)
    start.add_argument("--operator-id", required=True)
    start.add_argument("--simulation-origin", required=True)
    start.set_defaults(handler=_start)


def _register_material(parser: argparse.ArgumentParser) -> None:
    for name in (
        "run-manifest",
        "strategy-wheel",
        "strategy-manifest",
        "release-bom",
        "effective-config",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--toolkit-identity", type=Path, default=TOOLKIT_IDENTITY_FILE)


def _read_input(path: Path, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LOCAL_INPUT_BYTES:
            raise ValueError("local prepare document must be a bounded regular file")
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("local simulation credential must be private")
        payload = stream.read(_MAX_LOCAL_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(payload) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("local prepare document changed while reading")
    return payload


def _simulation_init(args: argparse.Namespace) -> int:
    from custos.core.execution_events import ExecutionEventStore
    from custos.core.local_runtime_state import RuntimeAuthorityRegistry

    root = args.state_root
    try:
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("simulation state root must be canonical")
        root.mkdir(mode=0o700)
        registry = RuntimeAuthorityRegistry.initialize(
            root / "control", identity_probe=lambda path: ExecutionEventStore.open(path).identity()
        )
    except (OSError, ValueError) as error:
        print(f"local simulation initialization refused: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        json.dumps({"status": "simulation_initialized", "registry_id": str(registry.registry_id)})
    )
    return 0


async def _start(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydantic import SecretStr

    from custos.artifacts.errors import ArtifactVerificationError
    from custos.artifacts.local_context import prepare_local_strategy_material
    from custos.artifacts.policy import ArchiveLimitsV1
    from custos.core.toolkit_identity import observe_runtime_toolkit_snapshot
    from custos.engines.nautilus.local_host import LoopbackPapertrade, run_simulated_papertrade
    from custos.local.authorization import (
        LocalOperatorAuthority,
        LocalPermitAuthenticator,
        local_operator_key_id,
    )
    from custos.local.manifest import parse_local_manifest

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    installed = []
    try:
        venue = LoopbackPapertrade(args.simulation_origin)
        venue.check_environment()
        manifest = _read_input(args.run_manifest)
        permit = _read_input(args.permit)
        run = parse_local_manifest(manifest)
        public = Ed25519PublicKey.from_public_bytes(_read_input(args.operator_public_key))
        verifier = LocalPermitAuthenticator(
            {local_operator_key_id(public): LocalOperatorAuthority(args.operator_id, public)}
        )
        toolkit = observe_runtime_toolkit_snapshot(
            args.toolkit_identity,
            report=lambda event, _fields: print(event, file=sys.stderr),
        )
        with TemporaryDirectory(prefix="custos-local-start-") as temporary:
            material = prepare_local_strategy_material(
                wheel_path=args.strategy_wheel,
                artifact_ref=run.artifact,
                manifest_bytes=_read_input(args.strategy_manifest),
                release_bom_bytes=_read_input(args.release_bom),
                quarantine_parent=Path(temporary),
                limits=ArchiveLimitsV1(),
            )
            for sig in signals:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            clean = await run_simulated_papertrade(
                state_root=args.state_root,
                venue=venue,
                manifest_bytes=manifest,
                permit_bytes=permit,
                authenticator=verifier,
                material=material,
                toolkit=toolkit,
                stop_requested=stop,
                effective_config=_read_input(args.effective_config),
                session_key=SecretStr(
                    _read_input(args.simulation_session_file, private=True).decode().strip()
                ),
                emit=lambda status: print(json.dumps({"status": status}), flush=True),
            )
        return 0 if clean else 1
    except (OSError, ValueError, RuntimeError, ImportError, ArtifactVerificationError) as error:
        # Some Native and parsing errors contain caller-supplied material.
        print(f"local simulation failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _prepare(args: argparse.Namespace) -> int:
    from custos.artifacts.errors import ArtifactVerificationError
    from custos.artifacts.local_context import (
        bind_local_strategy_material,
        prepare_local_strategy_material,
    )
    from custos.artifacts.policy import ArchiveLimitsV1
    from custos.core.toolkit_identity import observe_runtime_toolkit_snapshot
    from custos.local.manifest import parse_local_manifest

    try:
        run = parse_local_manifest(_read_input(args.run_manifest))
        strategy_manifest = _read_input(args.strategy_manifest)
        bom = _read_input(args.release_bom)
        config = _read_input(args.effective_config)
        toolkit = observe_runtime_toolkit_snapshot(
            args.toolkit_identity,
            report=lambda event, _fields: print(event, file=sys.stderr),
        )
        with TemporaryDirectory(prefix="custos-local-prepare-") as temporary:
            material = prepare_local_strategy_material(
                wheel_path=args.strategy_wheel,
                manifest_bytes=strategy_manifest,
                artifact_ref=run.artifact,
                release_bom_bytes=bom,
                quarantine_parent=Path(temporary),
                limits=ArchiveLimitsV1(),
            )
            bind_local_strategy_material(
                material=material,
                run=run,
                effective_config_bytes=config,
                toolkit=toolkit,
            )
    except ArtifactVerificationError as error:
        print(f"local prepare refused: {error.code.value}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"local prepare refused: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "materials_prepared",
                "manifest_sha256": run.digest,
                "strategy_wheel_sha256": run.artifact.artifact_sha256,
                "release_bom_sha256": run.release_bom_sha256,
                "toolkit_identity_sha256": run.toolkit_identity_sha256,
                "effective_config_sha256": run.effective_config_digest,
            },
            sort_keys=True,
        )
    )
    return 0

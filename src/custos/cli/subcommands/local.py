"""Inspect material for a prospective local Custos run."""

from __future__ import annotations

import argparse
import json
import os
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
    for name in (
        "run-manifest",
        "strategy-wheel",
        "strategy-manifest",
        "release-bom",
        "effective-config",
    ):
        prepare.add_argument("--" + name, type=Path, required=True)
    prepare.add_argument("--toolkit-identity", type=Path, default=TOOLKIT_IDENTITY_FILE)
    prepare.set_defaults(handler=_prepare)


def _read_input(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LOCAL_INPUT_BYTES:
            raise ValueError("local prepare document must be a bounded regular file")
        payload = stream.read(_MAX_LOCAL_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(payload) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("local prepare document changed while reading")
    return payload


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

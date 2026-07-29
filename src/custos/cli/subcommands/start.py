"""Fail-closed ``arx-runner start`` composition."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from custos.core.machine_credential_vault import (
    MachineCredentialError,
    MachineCredentialVault,
)
from custos.core.nats_transport import RunnerNatsTransportError
from custos.core.runner_toml import RunnerToml

DEFAULT_RUNNER_TOML = Path.home() / ".arx" / "runner.toml"
DEFAULT_VAULT_DIR = Path.home() / ".arx" / "vault"
DEFAULT_READY_FILE = Path.home() / ".arx" / "state" / "runner-ready.json"
DEFAULT_RUNNER_CAPABILITY = Path.home() / ".arx" / "runner-capability.json"
DEFAULT_RUNNER_FACT_OUTBOX = Path.home() / ".arx" / "state" / "runner-fact-outbox.db"
DEFAULT_CRUCIBLE_DOMAIN_PUBLIC_KEY = Path.home() / ".arx" / "crucible-domain-event.pub"
DEFAULT_NATS_TRANSPORT_VAULT_DIR = Path.home() / ".arx" / "vault" / "runner-nats-transport"
DEFAULT_NATS_CA = Path.home() / ".arx" / "certs" / "crucible-nats-ca.pem"
DEFAULT_DEVELOPMENT_ARTIFACT_ROOT = Path.home() / ".alephain" / "v1-team" / "strategy-artifacts"
DEFAULT_ARTIFACT_QUARANTINE_DIR = Path.home() / ".arx" / "state" / "artifact-quarantine"
DEFAULT_ARTIFACT_ACTIVATION_DIR = Path.home() / ".arx" / "state" / "artifact-activations"
DEFAULT_ARTIFACT_CACHE_DIR = Path.home() / ".arx" / "state" / "artifact-cache"


def _optional_path_from_environment(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "start", help="Start only after machine authority passes fail-closed verification."
    )
    parser.add_argument(
        "--runner-toml", dest="runner_toml_path", type=Path, default=DEFAULT_RUNNER_TOML
    )
    parser.add_argument(
        "--machine-vault",
        type=Path,
        default=None,
        help="Optional exact override; must equal runner.toml machine_vault_path.",
    )
    parser.add_argument(
        "--nats-transport-vault-dir",
        type=Path,
        default=DEFAULT_NATS_TRANSPORT_VAULT_DIR,
    )
    parser.add_argument(
        "--enabled-mode",
        action="append",
        choices=("sandbox", "testnet", "live"),
        default=None,
        help=(
            "Repeat for every exact mode session hosted by this supervisor. "
            "Required by the signed lane; the offline lane takes its mode from the spec."
        ),
    )
    parser.add_argument(
        "--development-local-nats-url",
        default=os.environ.get("CUSTOS_DEVELOPMENT_LOCAL_NATS_URL", ""),
        help=(
            "Explicit non-promotable sandbox-only loopback NATS endpoint; "
            "production transport vault, TLS and issuer pin remain mandatory otherwise."
        ),
    )
    parser.add_argument("--nats-sim-url", default=os.environ.get("CRUCIBLE_NATS_SIM_URL", ""))
    parser.add_argument("--nats-sim-ca", type=Path, default=DEFAULT_NATS_CA)
    parser.add_argument(
        "--nats-sim-server-name",
        default=os.environ.get("CRUCIBLE_NATS_SIM_SERVER_NAME", ""),
    )
    parser.add_argument(
        "--nats-sim-issuer-public-key",
        default=os.environ.get("CRUCIBLE_NATS_SIM_ISSUER_PUBLIC_KEY", ""),
    )
    parser.add_argument("--nats-live-url", default=os.environ.get("CRUCIBLE_NATS_LIVE_URL", ""))
    parser.add_argument("--nats-live-ca", type=Path, default=DEFAULT_NATS_CA)
    parser.add_argument(
        "--nats-live-server-name",
        default=os.environ.get("CRUCIBLE_NATS_LIVE_SERVER_NAME", ""),
    )
    parser.add_argument(
        "--nats-live-issuer-public-key",
        default=os.environ.get("CRUCIBLE_NATS_LIVE_ISSUER_PUBLIC_KEY", ""),
    )
    parser.add_argument("--vault-dir", type=Path, default=DEFAULT_VAULT_DIR)
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument(
        "--reconcile-strategy-id",
        default=None,
        help=(
            "Run the offline lane for this strategy instead of the signed one. "
            "Sandbox and testnet only; live is refused."
        ),
    )
    parser.add_argument("--nats-url", default="nats://localhost:4222")
    parser.add_argument(
        "--offline-state", type=Path, default=Path.home() / ".arx" / "state" / "offline-lane.db"
    )
    parser.add_argument(
        "--crucible-domain-public-key",
        type=Path,
        default=DEFAULT_CRUCIBLE_DOMAIN_PUBLIC_KEY,
    )
    parser.add_argument(
        "--crucible-domain-key-id",
        default=os.environ.get("CRUCIBLE_DOMAIN_EVENT_KEY_ID", ""),
    )
    parser.add_argument("--engine", choices=["nautilus", "sandbox-sim"], default="nautilus")
    parser.add_argument("--ready-file", type=Path, default=DEFAULT_READY_FILE)
    parser.add_argument("--runner-capability", type=Path, default=DEFAULT_RUNNER_CAPABILITY)
    parser.add_argument("--runner-fact-outbox", type=Path, default=DEFAULT_RUNNER_FACT_OUTBOX)
    parser.add_argument(
        "--development-artifact-root",
        type=Path,
        default=Path(
            os.environ.get(
                "CUSTOS_DEVELOPMENT_ARTIFACT_ROOT",
                str(DEFAULT_DEVELOPMENT_ARTIFACT_ROOT),
            )
        ),
        help="Shared sandbox-only content-addressed artifact store.",
    )
    parser.add_argument(
        "--artifact-quarantine-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_QUARANTINE_DIR,
    )
    parser.add_argument(
        "--artifact-activation-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_ACTIVATION_DIR,
    )
    parser.add_argument(
        "--artifact-cache-dir",
        type=Path,
        default=Path(os.environ.get("CUSTOS_ARTIFACT_CACHE_DIR", str(DEFAULT_ARTIFACT_CACHE_DIR))),
    )
    parser.add_argument(
        "--artifact-registry",
        default=os.environ.get("CUSTOS_ARTIFACT_REGISTRY", "ghcr.io"),
    )
    parser.add_argument(
        "--artifact-registry-username",
        default=os.environ.get("CUSTOS_ARTIFACT_REGISTRY_USERNAME", ""),
    )
    parser.add_argument(
        "--artifact-release-policy-envelope",
        type=Path,
        default=_optional_path_from_environment("CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE"),
    )
    parser.add_argument(
        "--artifact-release-policy-key-id",
        default=os.environ.get("CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID", ""),
    )
    parser.add_argument(
        "--artifact-release-policy-public-key",
        type=Path,
        default=_optional_path_from_environment("CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY"),
    )
    parser.add_argument(
        "--artifact-sigstore-trusted-root",
        type=Path,
        default=_optional_path_from_environment("CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT"),
    )
    parser.add_argument("--runner-fact-snapshot-interval-secs", type=float, default=10.0)
    parser.add_argument("--runner-fact-period-secs", type=int, default=86_400)
    parser.add_argument("--runner-fact-period-retry-secs", type=float, default=30.0)
    parser.set_defaults(
        artifact_registry_token=os.environ.get("CUSTOS_ARTIFACT_REGISTRY_TOKEN", ""),
        handler=run,
    )


def _run_offline_lane(args: argparse.Namespace, metadata: Any, credential: Any) -> int:
    """Run the offline lane instead of the signed daemon.

    A separate composition, not a mode of the signed one: there is no control
    plane to verify against here, and stubbing that check to reuse the signed
    path would hollow out the check itself.
    """

    from custos.cli._daemon import _build_host
    from custos.core.readiness import ReadinessFile
    from custos.offline.daemon import run_offline_lane

    engine = _build_host(
        argparse.Namespace(
            engine=args.engine,
            tenant_id=metadata.tenant_id,
            runner_id=metadata.runner_id,
        )
    )
    readiness = ReadinessFile(
        args.ready_file,
        tenant_id=metadata.tenant_id,
        runner_id=metadata.runner_id,
        credential_id=str(credential.credential_id),
        credential_version=credential.credential_version,
        credential_valid_until=metadata.credential_valid_until,
        machine_key_id=credential.machine_key_id,
    )
    try:
        return asyncio.run(
            run_offline_lane(
                tenant_id=metadata.tenant_id,
                runner_id=metadata.runner_id,
                strategy_id=args.reconcile_strategy_id,
                nats_url=args.nats_url,
                vault_dir=args.vault_dir,
                engine=engine,
                ready_file=args.ready_file,
                state_path=args.offline_state,
                readiness=readiness,
            )
        )
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"offline lane failed: {exc}", file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    args.ready_file.expanduser().resolve().unlink(missing_ok=True)
    try:
        metadata = RunnerToml.read(args.runner_toml_path)
        bound_vault_path = Path(metadata.machine_vault_path).expanduser().resolve()
        if (
            args.machine_vault is not None
            and args.machine_vault.expanduser().resolve() != bound_vault_path
        ):
            raise MachineCredentialError(
                "--machine-vault differs from runner.toml authority binding"
            )
        credential = MachineCredentialVault(bound_vault_path).load()
        credential.assert_binding(metadata)
    except (OSError, ValueError, MachineCredentialError) as exc:
        print(f"Runner startup authority check failed: {exc}", file=sys.stderr)
        return 1

    if args.reconcile_strategy_id:
        return _run_offline_lane(args, metadata, credential)

    if not args.enabled_mode:
        print(
            "the signed lane requires at least one --enabled-mode; the offline lane "
            "is selected with --reconcile-strategy-id",
            file=sys.stderr,
        )
        return 2

    namespace = argparse.Namespace(
        tenant_id=metadata.tenant_id,
        runner_id=metadata.runner_id,
        runner_toml_path=args.runner_toml_path.expanduser().resolve(),
        machine_vault=bound_vault_path,
        enabled_modes=tuple(args.enabled_mode),
        development_local_nats_url=args.development_local_nats_url,
        nats_transport_vault_dir=args.nats_transport_vault_dir,
        nats_sim_url=args.nats_sim_url,
        nats_sim_ca=args.nats_sim_ca,
        nats_sim_server_name=args.nats_sim_server_name,
        nats_sim_issuer_public_key=args.nats_sim_issuer_public_key,
        nats_live_url=args.nats_live_url,
        nats_live_ca=args.nats_live_ca,
        nats_live_server_name=args.nats_live_server_name,
        nats_live_issuer_public_key=args.nats_live_issuer_public_key,
        vault_dir=args.vault_dir,
        reconcile=args.reconcile,
        crucible_domain_public_key=args.crucible_domain_public_key,
        crucible_domain_key_id=args.crucible_domain_key_id,
        engine=args.engine,
        ready_file=args.ready_file,
        runner_capability=args.runner_capability,
        runner_fact_outbox=args.runner_fact_outbox,
        development_artifact_root=args.development_artifact_root,
        artifact_quarantine_dir=args.artifact_quarantine_dir,
        artifact_activation_dir=args.artifact_activation_dir,
        artifact_cache_dir=args.artifact_cache_dir,
        artifact_registry=args.artifact_registry,
        artifact_registry_username=args.artifact_registry_username,
        artifact_registry_token=args.artifact_registry_token,
        artifact_release_policy_envelope=args.artifact_release_policy_envelope,
        artifact_release_policy_key_id=args.artifact_release_policy_key_id,
        artifact_release_policy_public_key=args.artifact_release_policy_public_key,
        artifact_sigstore_trusted_root=args.artifact_sigstore_trusted_root,
        runner_fact_snapshot_interval_secs=args.runner_fact_snapshot_interval_secs,
        runner_fact_period_secs=args.runner_fact_period_secs,
        runner_fact_period_retry_secs=args.runner_fact_period_retry_secs,
    )
    from custos.cli._daemon import run_daemon

    try:
        return asyncio.run(run_daemon(namespace))
    except (
        OSError,
        ValueError,
        MachineCredentialError,
        RunnerNatsTransportError,
        RuntimeError,
    ) as exc:
        args.ready_file.expanduser().resolve().unlink(missing_ok=True)
        print(f"Runner startup failed closed: {exc}", file=sys.stderr)
        return 1

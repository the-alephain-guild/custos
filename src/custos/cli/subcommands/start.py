"""Fail-closed ``arx-runner start`` composition."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

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
        required=True,
        help="Repeat for every exact mode session hosted by this supervisor.",
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
    parser.add_argument("--runner-fact-snapshot-interval-secs", type=float, default=10.0)
    parser.add_argument("--runner-fact-period-secs", type=int, default=86_400)
    parser.add_argument("--runner-fact-period-retry-secs", type=float, default=30.0)
    parser.set_defaults(handler=run)


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

"""custos-strategy-release: build, sign and publish a strategy release.

The steps run in this order, the first three without any credential beyond
read access to the toolkit package:

  identity   print the workflow identity this job would sign as (needs OIDC)
  build      build the release of one strategy into a directory
  candidate  compare two builds and freeze them into one unsigned candidate
  assemble   sign the candidate's statement and assemble the publication input
  preflight  check an assembled publication input without publishing it
  publish    push the release to GHCR and read it back by digest

The reusable workflow .github/workflows/publish-strategy-release.yml runs them
for a producer repository; nothing here is specific to one producer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from .oci_primitives import PRODUCER_REPOSITORY_PATTERN
from .toolkit_authority import AUTHORITY_RECEIPT_PATH

RELEASE_REPOSITORY_SUFFIX = "strategy-releases"
_TOOLKIT_ACTOR_VARIABLES = ("TOOLKIT_REGISTRY_ACTOR", "GITHUB_ACTOR")
_TOOLKIT_TOKEN_VARIABLES = ("TOOLKIT_REGISTRY_TOKEN", "GITHUB_TOKEN")


class ReleaseCommandError(RuntimeError):
    """A command's inputs are missing or disagree with each other."""


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseCommandError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _clean_head(repo: Path) -> str:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseCommandError("the producer worktree must be clean to build a release")
    return _git(repo, "rev-parse", "HEAD")


def _from_environment(names: Sequence[str], label: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise ReleaseCommandError(f"set {' or '.join(names)} to a GitHub {label}")


def _positive(value: str, label: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise ReleaseCommandError(f"{label} must be a positive integer")
    return int(value)


def default_release_repository(producer_repository: str) -> str:
    """ghcr.io/<owner>/<repo>/strategy-releases, lowercased as GHCR requires."""

    return f"ghcr.io/{producer_repository.lower()}/{RELEASE_REPOSITORY_SUFFIX}"


def _producer_repository(value: str | None) -> str:
    repository = value or os.environ.get("GITHUB_REPOSITORY", "")
    if re.fullmatch(PRODUCER_REPOSITORY_PATTERN, repository) is None:
        raise ReleaseCommandError(
            "give --producer-repository as <owner>/<name> (GITHUB_REPOSITORY is not set)"
        )
    return repository


def _locked_toolkit_version(repo_root: Path) -> str | None:
    lock = repo_root / "toolchain.lock.toml"
    if not lock.is_file():
        return None
    version = tomllib.loads(lock.read_text(encoding="utf-8")).get("toolkit", {}).get("version")
    return version if isinstance(version, str) else None


def _toolkit_version(repo_root: Path, requested: str | None) -> str:
    locked = _locked_toolkit_version(repo_root)
    if requested and locked and requested != locked:
        raise ReleaseCommandError(
            f"toolchain.lock.toml pins toolkit {locked}, but {requested} was requested"
        )
    version = requested or locked
    if not version:
        raise ReleaseCommandError("give --toolkit-version (no toolchain.lock.toml pins one)")
    return version


def _authority_commit(custos_root: Path, version: str, requested: str | None) -> str:
    if requested:
        return requested
    release = re.search(r"(rc[0-9]+)$", version)
    if release is None:
        raise ReleaseCommandError(f"toolkit {version} has no Custos authority receipt")
    receipt = AUTHORITY_RECEIPT_PATH.format(release=release.group(1))
    commit = _git(custos_root, "log", "-1", "--format=%H", "--", receipt)
    if not commit:
        raise ReleaseCommandError(f"Custos has no receipt at {receipt}")
    return commit


def _print(value: Mapping[str, object]) -> None:
    print(json.dumps(value, sort_keys=True))


def run_identity(_: argparse.Namespace) -> int:
    from .github_oidc import read_workflow_claims, request_oidc_token

    claims = read_workflow_claims(request_oidc_token(os.environ))
    _print(
        {
            "producer_repository": claims.repository,
            "source_ref": claims.ref,
            "workflow_identity": claims.workflow_identity,
        }
    )
    return 0


def run_build(args: argparse.Namespace) -> int:
    from .artifact_build import ProducerIdentity, build_strategy_artifact_tree
    from .artifact_typing import StrictStrategyTypingVerifier
    from .template import template_source_inventory, template_strategy_spec
    from .toolkit_authority import load_toolkit_authority
    from .toolkit_registry import ToolkitRegistryV1

    repo_root = args.repo_root.resolve()
    custos_root = args.custos_root.resolve()
    producer_commit = _clean_head(repo_root)
    repository = _producer_repository(args.producer_repository)
    producer = ProducerIdentity(
        repository=repository,
        ghcr_repository=args.ghcr_repository or default_release_repository(repository),
        workflow_identity=args.workflow_identity,
    )
    spec = template_strategy_spec(repo_root, args.strategy, producer)
    inventory = template_source_inventory(repo_root, args.strategy, spec)
    version = _toolkit_version(repo_root, args.toolkit_version)
    authority = load_toolkit_authority(
        custos_root,
        candidate_version=version,
        authority_commit=_authority_commit(custos_root, version, args.authority_commit),
    )
    toolkit = ToolkitRegistryV1(
        authority,
        actor=_from_environment(_TOOLKIT_ACTOR_VARIABLES, "actor"),
        token=_from_environment(_TOOLKIT_TOKEN_VARIABLES, "token"),
    ).fetch()
    _, category, name = Path(args.strategy).parts
    source_date_epoch = args.source_date_epoch or int(
        _git(repo_root, "show", "-s", "--format=%ct", producer_commit)
    )
    result = build_strategy_artifact_tree(
        spec=spec,
        producer=producer,
        inventory=inventory,
        authority=authority,
        toolkit=toolkit,
        producer_commit=producer_commit,
        source_date_epoch=source_date_epoch,
        discovery_tag=args.discovery_tag or f"{category}-{name}-{spec.version}",
        output=args.output,
        typing_verifier=StrictStrategyTypingVerifier(),
        producer_worktree_clean=True,
    )
    _print(
        {
            "output": str(result.output),
            "producer_commit": producer_commit,
            "repository": producer.ghcr_repository,
            "strategy_coordinate": spec.coordinate,
            "strategy_release_statement_sha256": result.strategy_release_statement_sha256,
            "strategy_wheel_sha256": result.strategy_wheel_sha256,
        }
    )
    return 0


def run_candidate(args: argparse.Namespace) -> int:
    from .candidate import finalize_unsigned_candidate

    repository = _producer_repository(args.producer_repository)
    digest = finalize_unsigned_candidate(
        args.first_build,
        args.second_build,
        args.output,
        repo_root=args.repo_root,
        producer_commit=args.producer_commit or _clean_head(args.repo_root),
        producer_repository=repository,
        repository=args.ghcr_repository or default_release_repository(repository),
        workflow_ref=args.workflow_ref or os.environ.get("GITHUB_WORKFLOW_REF", ""),
        run_id=_positive(args.run_id or os.environ.get("GITHUB_RUN_ID", ""), "run id"),
        run_attempt=_positive(
            args.run_attempt or os.environ.get("GITHUB_RUN_ATTEMPT", ""), "run attempt"
        ),
        artifact_name=args.artifact_name,
    )
    _print({"unsigned_candidate_sha256": digest})
    return 0


def run_assemble(args: argparse.Namespace) -> int:
    from .assembly import assemble_publication_input
    from .release import PublicationContextV1

    context = PublicationContextV1.from_environment(os.environ)
    repository = args.ghcr_repository or default_release_repository(context.producer_repository)
    candidate = assemble_publication_input(
        args.unsigned_input,
        args.output,
        repo_root=args.repo_root,
        producer_commit=context.producer_commit,
        producer_repository=context.producer_repository,
        repository=repository,
        candidate_workflow_ref=os.environ.get("GITHUB_WORKFLOW_REF", ""),
        workflow=context.workflow,
        candidate_run_id=context.workflow.workflow_run_id,
        candidate_artifact_name=args.candidate_artifact_name,
        candidate_artifact_digest=args.candidate_artifact_digest,
    )
    _print(
        {
            "discovery_tag": candidate.discovery_tag,
            "repository": candidate.repository,
            "workflow_identity": context.workflow.workflow_identity,
        }
    )
    return 0


def run_preflight_or_publish(args: argparse.Namespace) -> int:
    from .model import canonical_json_bytes
    from .release import (
        PUBLISH_INPUT_SCHEMA_VERSION,
        PublicationContextV1,
        RegistryPublicationCredentialsV1,
        atomic_write,
        load_candidate_v1,
    )

    context = PublicationContextV1.from_environment(os.environ)
    repository = args.ghcr_repository or default_release_repository(context.producer_repository)
    candidate = load_candidate_v1(args.input, context, repository=repository)
    if args.command == "preflight":
        _print(
            {
                "candidate_validated": True,
                "external_publication_completed": False,
                "producer_commit": candidate.producer_commit,
                "schema_version": PUBLISH_INPUT_SCHEMA_VERSION,
            }
        )
        return 0

    from .ghcr_registry import GhcrRegistryV1
    from .oci_publication import publish_verified_candidate

    credentials = RegistryPublicationCredentialsV1.from_environment(os.environ)
    registry = GhcrRegistryV1(
        actor=credentials.actor,
        token=credentials.token,
        repository=candidate.repository,
    )
    bundle = publish_verified_candidate(
        candidate,
        registry,
        external_publication_completed=True,
    )
    atomic_write(args.artifact_ref_output, bundle.artifact_ref.canonical_bytes())
    atomic_write(args.receipt_output, canonical_json_bytes(bundle.receipt.to_mapping()))
    _print(
        {
            "artifact_ref": str(args.artifact_ref_output),
            "receipt": str(args.receipt_output),
            "repository": candidate.repository,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custos-strategy-release",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    identity = commands.add_parser("identity", help="print the workflow identity to sign as")
    identity.set_defaults(run=run_identity)

    def producer_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument(
            "--producer-repository", help="<owner>/<name>; defaults to GITHUB_REPOSITORY"
        )
        command.add_argument(
            "--ghcr-repository",
            help=f"defaults to ghcr.io/<owner>/<name>/{RELEASE_REPOSITORY_SUFFIX}",
        )

    build = commands.add_parser("build", help="build one strategy's release into a directory")
    producer_options(build)
    build.add_argument("--strategy", required=True, help="strategies/<category>/<name>")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--custos-root", type=Path, required=True)
    build.add_argument("--workflow-identity", required=True)
    build.add_argument("--toolkit-version", help="defaults to toolchain.lock.toml")
    build.add_argument("--authority-commit", help="defaults to the receipt's last commit")
    build.add_argument("--source-date-epoch", type=int, help="defaults to the commit time")
    build.add_argument("--discovery-tag", help="defaults to <category>-<name>-<version>")
    build.set_defaults(run=run_build)

    candidate = commands.add_parser("candidate", help="compare two builds and freeze one")
    producer_options(candidate)
    candidate.add_argument("--first-build", type=Path, required=True)
    candidate.add_argument("--second-build", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.add_argument("--artifact-name", required=True)
    candidate.add_argument("--producer-commit")
    candidate.add_argument("--workflow-ref")
    candidate.add_argument("--run-id")
    candidate.add_argument("--run-attempt")
    candidate.set_defaults(run=run_candidate)

    assemble = commands.add_parser("assemble", help="sign the candidate and assemble it")
    producer_options(assemble)
    assemble.add_argument("--unsigned-input", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--candidate-artifact-name", required=True)
    assemble.add_argument("--candidate-artifact-digest", required=True)
    assemble.set_defaults(run=run_assemble)

    for name, help_text in (
        ("preflight", "check an assembled release without publishing"),
        ("publish", "publish an assembled release and read it back"),
    ):
        command = commands.add_parser(name, help=help_text)
        producer_options(command)
        command.add_argument("--input", type=Path, required=True)
        if name == "publish":
            command.add_argument("--artifact-ref-output", type=Path, required=True)
            command.add_argument("--receipt-output", type=Path, required=True)
        command.set_defaults(run=run_preflight_or_publish)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.run(args))
    except (ReleaseCommandError, ValueError, RuntimeError) as error:
        print(f"custos-strategy-release {args.command}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

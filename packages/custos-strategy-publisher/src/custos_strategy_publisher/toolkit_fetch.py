"""Fetch the receipt-pinned toolkit wheels so they can be installed as an index.

The authoritative toolkit lives in GHCR as a digest-pinned OCI artifact, which is
not something a lockfile can express. Installing it therefore happens in two
steps: pull the exact bytes Custos's receipt names, then install them out of
a directory.

The directory matters. Installing a wheel *by path* makes it a direct reference,
and PEP 610 has installers record that in ``direct_url.json`` — which is how
``toolkit_provenance`` tells a local build from a pinnable one. Handing pip or uv
a ``--find-links`` directory instead keeps the install index-shaped, so the
authoritative layer stays distinguishable from a developer's own build.

Credentials come from the environment because the same client runs in CI with
the workflow's own token. The package is public, but the client refuses empty
credentials by design and that guard is not worth loosening for one convenience.

Prints the candidate version on stdout so callers can pin the install to it
rather than writing a version down twice.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .toolkit_authority import CustosToolkitAuthorityV1, load_toolkit_authority
from .toolkit_registry import ToolkitRegistryV1

_ACTOR_VARIABLES = ("TOOLKIT_REGISTRY_ACTOR", "GHCR_ACTOR")
_TOKEN_VARIABLES = ("TOOLKIT_REGISTRY_TOKEN", "GHCR_TOKEN")


class ToolkitFetchError(RuntimeError):
    """The fetch could not run, or wrote nothing a caller could install."""


def _from_environment(names: Sequence[str], label: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    joined = " or ".join(names)
    raise ToolkitFetchError(
        f"set {joined} to a GitHub {label}; any token can read the public toolkit package"
    )


def fetch_pinned_wheels(authority: CustosToolkitAuthorityV1, output: Path) -> str:
    """Write the receipt-pinned wheels into ``output`` and return their version."""

    registry = ToolkitRegistryV1(
        authority,
        actor=_from_environment(_ACTOR_VARIABLES, "actor"),
        token=_from_environment(_TOKEN_VARIABLES, "token"),
    )
    snapshot = registry.fetch()

    output.mkdir(parents=True, exist_ok=True)
    for artifact, content in (
        (authority.base_contracts_wheel, snapshot.base_contracts_wheel),
        (authority.nautilus_wheel, snapshot.nautilus_wheel),
    ):
        name = artifact.title
        if not name.endswith(".whl") or "/" in name or "\\" in name:
            raise ToolkitFetchError(f"registry artifact title is not a wheel filename: {name}")
        (output / name).write_bytes(content)

    return authority.candidate_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--custos-root", type=Path, required=True)
    parser.add_argument("--toolkit-version", required=True)
    parser.add_argument("--authority-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        authority = load_toolkit_authority(
            args.custos_root,
            candidate_version=args.toolkit_version,
            authority_commit=args.authority_commit,
        )
        version = fetch_pinned_wheels(authority, args.output)
    except (ToolkitFetchError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

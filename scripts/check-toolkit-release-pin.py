#!/usr/bin/env python3
"""Refuse a runner release whose toolkit source differs from the authorized rc.

A runner image installs the authorized toolkit rc instead of building the
toolkit from the checkout. That only holds together if the toolkit the runner
was written against is that rc: the runner imports the base package, and a
strategy runs on whatever toolkit the image carries. So a release is allowed
only while the two toolkit directories are byte for byte the source commit of
the authorized rc. Any toolkit change, whichever plan it comes from, needs a new
rc published, promoted and registered before a runner can ship.

Without ``--release`` the check only reports, so ``make verify`` can show the
state on a branch that is legitimately ahead of the rc. With ``--release`` a
difference is a failure. ``--json`` prints the authority facts that the release
build reuses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM_AUTHORITY = Path("docs/authority/ecosystem-authority.json")
TOOLKIT_DIRECTORIES = (
    "packages/custos-strategy-toolkit",
    "packages/custos-strategy-toolkit-nautilus",
)
READY_STATUS = "READY_TOOLKIT_RC"


class ReleasePinError(RuntimeError):
    """The authority record is unusable, so nothing can be compared."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )


def authorized_rc(root: Path) -> dict[str, Any]:
    """Read and check the registered rc: status, receipt bytes and identity."""

    document = json.loads((root / ECOSYSTEM_AUTHORITY).read_text(encoding="utf-8"))
    release = document.get("toolkit_rc_release")
    if not isinstance(release, dict):
        raise ReleasePinError("ecosystem authority has no toolkit_rc_release record")
    if release.get("receipt_status") != READY_STATUS or release.get("receipt_present") is not True:
        raise ReleasePinError("the registered toolkit rc is not READY_TOOLKIT_RC")
    receipt_path = release.get("receipt_path")
    receipt_sha256 = release.get("receipt_sha256")
    if not isinstance(receipt_path, str) or not isinstance(receipt_sha256, str):
        raise ReleasePinError("the registered toolkit rc names no receipt")
    receipt_file = root / receipt_path
    if not receipt_file.is_file():
        raise ReleasePinError(f"the toolkit rc receipt {receipt_path} is missing")
    receipt_bytes = receipt_file.read_bytes()
    if hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256:
        raise ReleasePinError("the toolkit rc receipt differs from its registered digest")
    # The identity comes from the receipt, whose bytes were just checked; the record
    # beside it is not bound by that digest, so it must repeat the same values.
    receipt = json.loads(receipt_bytes)
    publication = receipt.get("publication_receipt") if isinstance(receipt, dict) else None
    if not isinstance(publication, dict):
        raise ReleasePinError("the toolkit rc receipt has no publication_receipt")
    identity = {
        "candidate_version": receipt.get("candidate_version"),
        "source_commit": receipt.get("source_commit"),
        "manifest_digest": publication.get("manifest_digest"),
    }
    for field, value in identity.items():
        if not isinstance(value, str) or not value:
            raise ReleasePinError(f"the toolkit rc receipt has no {field}")
        if release.get(field) != value:
            raise ReleasePinError(
                f"the registered toolkit rc {field} {release.get(field)!r} "
                f"differs from its receipt {value!r}"
            )
    return {
        **identity,
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha256,
    }


def differing_files(root: Path, source_commit: str) -> list[str]:
    """Toolkit files that differ between the rc's source commit and HEAD."""

    known = _git(root, "cat-file", "-e", f"{source_commit}^{{commit}}")
    if known.returncode != 0:
        raise ReleasePinError(f"the rc source commit {source_commit} is not in this history")
    diff = _git(root, "diff", "--name-only", source_commit, "HEAD", "--", *TOOLKIT_DIRECTORIES)
    if diff.returncode != 0:
        raise ReleasePinError(f"git diff failed: {diff.stderr.strip()}")
    return [line for line in diff.stdout.splitlines() if line]


def check(root: Path) -> dict[str, Any]:
    rc = authorized_rc(root)
    files = differing_files(root, rc["source_commit"])
    return {**rc, "toolkit_matches_rc": not files, "differing_files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--release", action="store_true", help="fail on any difference")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    try:
        result = check(args.root.resolve())
    except (ReleasePinError, OSError, ValueError) as error:
        print(f"toolkit release pin: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["toolkit_matches_rc"]:
        print(
            f"toolkit release pin: the toolkit is the authorized rc "
            f"{result['candidate_version']} ({result['source_commit'][:12]})"
        )
    else:
        stream = sys.stderr if args.release else sys.stdout
        print(
            f"toolkit release pin: {len(result['differing_files'])} toolkit file(s) differ "
            f"from the authorized rc {result['candidate_version']} "
            f"({result['source_commit'][:12]}); publish and promote a new rc, register "
            "its receipt, then release the runner",
            file=stream,
        )
        for name in result["differing_files"]:
            print(f"  {name}", file=stream)
    if args.release and not result["toolkit_matches_rc"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

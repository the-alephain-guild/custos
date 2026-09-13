#!/usr/bin/env python3
"""Read the NautilusTrader source pin out of uv.lock.

The runtime image compiles NautilusTrader from the Guild fork instead of
installing a published wheel, because a git requirement carries no hash and
`pip install --require-hashes` therefore cannot express it. The commit that
gets compiled has to be derived from `uv.lock`, which is the only place that
records what this tree actually resolves to. A sha copied into the Dockerfile
or the Makefile would be a second, unchecked claim about the same thing.

This is scaffolding for the git-source phase. Once NautilusTrader is consumed
as a published wheel, `read_pin` refuses the lock and this module, along with
the `nt-builder` stage, is deleted.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

PACKAGE = "nautilus-trader"
_SHA_LENGTH = 40
_HEX = frozenset("0123456789abcdef")


class NautilusSourcePinError(RuntimeError):
    """The lock does not describe a compilable NautilusTrader source."""


@dataclass(frozen=True, slots=True)
class NautilusSourcePin:
    url: str
    sha: str
    version: str
    subdirectory: str


def _require_sha(value: str, label: str) -> str:
    if len(value) != _SHA_LENGTH or not set(value) <= _HEX:
        raise NautilusSourcePinError(f"{label} is not a full 40-character commit sha: {value!r}")
    return value


def read_pin(lock_path: Path) -> NautilusSourcePin:
    document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    entries = [p for p in document.get("package", []) if p.get("name") == PACKAGE]

    if not entries:
        raise NautilusSourcePinError(f"no {PACKAGE} package in {lock_path}")
    if len(entries) != 1:
        raise NautilusSourcePinError(
            f"expected one {PACKAGE} package in {lock_path}, found {len(entries)}"
        )

    entry = entries[0]
    source = entry.get("source", {})
    git_url = source.get("git")
    if git_url is None:
        raise NautilusSourcePinError(
            f"{PACKAGE} in {lock_path} is not a git source: {source!r}. "
            "A published wheel needs no compilation stage; delete the nt-builder "
            "stage rather than teaching this script to guess a commit."
        )

    split = urlsplit(git_url)
    fragment = _require_sha(split.fragment, "the source fragment")
    query = parse_qs(split.query)

    revisions = query.get("rev", [])
    if len(revisions) != 1:
        raise NautilusSourcePinError(f"expected exactly one rev= in {git_url!r}")
    revision = _require_sha(revisions[0], "the rev= parameter")

    # uv records the resolved commit twice. Disagreement means the format is
    # not what this parser assumes, and picking either one would be a guess.
    if revision != fragment:
        raise NautilusSourcePinError(
            f"rev= and the fragment disagree in {git_url!r}: {revision} vs {fragment}"
        )

    subdirectories = query.get("subdirectory", [])
    if len(subdirectories) != 1:
        raise NautilusSourcePinError(f"expected exactly one subdirectory= in {git_url!r}")

    version = entry.get("version")
    if not version:
        raise NautilusSourcePinError(f"{PACKAGE} in {lock_path} has no version")

    return NautilusSourcePin(
        url=urlunsplit((split.scheme, split.netloc, split.path, "", "")),
        sha=fragment,
        version=version,
        subdirectory=subdirectories[0],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--field",
        required=True,
        choices=["url", "sha", "version", "subdirectory"],
        help="single field to print, for Makefile consumption",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "uv.lock",
    )
    arguments = parser.parse_args(argv)

    try:
        pin = read_pin(arguments.lock)
    except NautilusSourcePinError as error:
        print(str(error), file=sys.stderr)
        return 2

    print(getattr(pin, arguments.field))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

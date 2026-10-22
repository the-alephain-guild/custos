"""Write the image's toolkit identity from the toolkit bytes it actually installed.

A release build puts ``toolkit-authority.json`` beside the wheels (see
``scripts/release_dist.py``). This step hashes the two toolkit wheels the image
installed, refuses the build unless they are the bytes the authorized rc's receipt
registers and the installed distributions report the rc version, and writes
``toolkit-identity.json`` with the digests it computed. No value in the identity
comes from a build argument, so a wrong argument cannot make it claim other bytes.

A source build has no authority file. Then nothing is written, and a runner in
that image has no toolkit identity: it runs a toolkit built from the checkout.
A release build passes ``--require`` (the ``CUSTOS_RELEASE_BUILD=1`` build
argument), so a missing authority file fails it instead of producing an image
that silently carries no identity.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

AUTHORITY_FILE = "toolkit-authority.json"
IDENTITY_FILE = "toolkit-identity.json"
IDENTITY_SCHEMA_VERSION = 1
WHEELS = {
    "base_contracts_wheel": ("custos_strategy_toolkit-*.whl", "custos-strategy-toolkit"),
    "nautilus_wheel": (
        "custos_strategy_toolkit_nautilus-*.whl",
        "custos-strategy-toolkit-nautilus",
    ),
}


class ToolkitIdentityError(RuntimeError):
    """The installed toolkit is not the toolkit the authority file names."""


def _one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise ToolkitIdentityError(f"expected exactly one {pattern}, found {len(matches)}")
    return matches[0]


def _authority(directory: Path) -> dict[str, Any] | None:
    path = directory / AUTHORITY_FILE
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ToolkitIdentityError("toolkit-authority.json is not schema version 1")
    for field in ("candidate_version", "manifest_digest", "receipt_sha256", "source_commit"):
        if not isinstance(document.get(field), str) or not document[field]:
            raise ToolkitIdentityError(f"toolkit-authority.json has no {field}")
    wheels = document.get("wheels")
    if not isinstance(wheels, dict) or set(wheels) != set(WHEELS):
        raise ToolkitIdentityError(
            "toolkit-authority.json must name exactly the two toolkit wheels"
        )
    return document


def write_identity(
    wheels_directory: Path,
    output_directory: Path,
    installed_version: Callable[[str], str] = importlib.metadata.version,
    *,
    require: bool = False,
) -> dict[str, Any] | None:
    """Check the installed toolkit against the authority file and write the identity."""

    output_directory.mkdir(parents=True, exist_ok=True)
    authority = _authority(wheels_directory)
    if authority is None:
        if require:
            raise ToolkitIdentityError(
                f"a release build needs {AUTHORITY_FILE} beside the wheels; it is missing"
            )
        return None
    computed: dict[str, str] = {}
    for key, (pattern, distribution) in WHEELS.items():
        wheel = _one(wheels_directory, pattern)
        registered = authority["wheels"][key]
        if wheel.name != registered.get("filename"):
            raise ToolkitIdentityError(f"the installed {key} is {wheel.name}, not the rc's wheel")
        with wheel.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != registered.get("sha256"):
            raise ToolkitIdentityError(f"the installed {key} differs from the rc's receipt digest")
        version = installed_version(distribution)
        if version != authority["candidate_version"]:
            raise ToolkitIdentityError(
                f"{distribution} is installed as {version}, not {authority['candidate_version']}"
            )
        computed[key] = digest
    identity = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "candidate_version": authority["candidate_version"],
        "manifest_digest": authority["manifest_digest"],
        "receipt_sha256": authority["receipt_sha256"],
        "source_commit": authority["source_commit"],
        "base_contracts_wheel_sha256": computed["base_contracts_wheel"],
        "nautilus_wheel_sha256": computed["nautilus_wheel"],
    }
    path = output_directory / IDENTITY_FILE
    path.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o444)
    return identity


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="write-toolkit-identity.py")
    parser.add_argument("--require", action="store_true", help="fail without an authority file")
    parser.add_argument("wheels_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args(argv[1:])
    try:
        identity = write_identity(
            args.wheels_directory, args.output_directory, require=args.require
        )
    except (ToolkitIdentityError, OSError, ValueError) as error:
        print(f"toolkit identity: {error}", file=sys.stderr)
        return 1
    if identity is None:
        print("toolkit identity: source build, no authority file, nothing written")
    else:
        print(f"toolkit identity: {identity['candidate_version']} ({identity['manifest_digest']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

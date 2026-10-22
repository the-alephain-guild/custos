#!/usr/bin/env python3
"""Build the release inputs: the runner from source, the toolkit as the authorized rc.

``make dist`` builds all three wheels from the checkout, which makes the image
carry whatever toolkit the checkout holds under the version ``0.1.0``. A release
instead:

1. refuses unless the two toolkit directories are the authorized rc's source
   (``check-toolkit-release-pin.py``);
2. builds only the runner's sdist and wheel, from a copy of ``HEAD`` whose
   ``pyproject.toml`` pins both toolkit distributions to the rc version, so the
   image's ``pip check`` agrees with what the rc wheels declare. The sdist
   carries the same rewritten pins;
3. pulls the rc's two wheels from the registry by its receipt (the registry
   client checks every blob against the receipt digest);
4. checks the pulled wheels against the receipt once more and writes
   ``toolkit-authority.json`` beside them, which the image build reads to check
   the bytes it installs and to write its toolkit identity.

Registry credentials come from ``TOOLKIT_REGISTRY_ACTOR`` and
``TOOLKIT_REGISTRY_TOKEN`` (or the ``GHCR_`` names); any token can read the
public package.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from email.parser import BytesParser
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_FILE = "toolkit-authority.json"
AUTHORITY_SCHEMA_VERSION = 1
WHEEL_ROLES = {
    "base_contracts_wheel": "base_contracts_wheel.wheel",
    "nautilus_wheel": "nautilus_wheel.wheel",
}
PINS = (
    ('"custos-strategy-toolkit==0.1.0"', "custos-strategy-toolkit"),
    ('"custos-strategy-toolkit-nautilus==0.1.0"', "custos-strategy-toolkit-nautilus"),
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ReleaseDistError(RuntimeError):
    """The release inputs could not be built as the authority requires."""


def _load_pin_check() -> ModuleType:
    path = ROOT / "scripts" / "check-toolkit-release-pin.py"
    spec = importlib.util.spec_from_file_location("check_toolkit_release_pin", path)
    if spec is None or spec.loader is None:
        raise ReleaseDistError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rewrite_toolkit_pins(pyproject: str, candidate_version: str) -> str:
    """Pin both toolkit distributions to the rc; each pin must occur exactly once."""

    rewritten = pyproject
    for old, name in PINS:
        if rewritten.count(old) != 1:
            raise ReleaseDistError(f"pyproject.toml must pin {old} exactly once")
        rewritten = rewritten.replace(old, f'"{name}=={candidate_version}"')
    return rewritten


def receipt_wheels(receipt: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The two toolkit wheels the receipt registers: file name, digest and size."""

    publication = receipt.get("publication_receipt")
    layers = publication.get("layers") if isinstance(publication, Mapping) else None
    if not isinstance(layers, list):
        raise ReleaseDistError("the toolkit receipt lists no publication layers")
    wheels: dict[str, dict[str, Any]] = {}
    for key, role in WHEEL_ROLES.items():
        matches = [
            layer for layer in layers if isinstance(layer, Mapping) and layer.get("role") == role
        ]
        if len(matches) != 1:
            raise ReleaseDistError(f"the toolkit receipt must register exactly one {role}")
        layer = matches[0]
        digest = layer.get("digest")
        title = layer.get("title")
        size = layer.get("size_bytes")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != len("sha256:") + 64
            or not isinstance(title, str)
            or not title.endswith(".whl")
            or "/" in title
            or type(size) is not int
        ):
            raise ReleaseDistError(f"the toolkit receipt's {role} entry is malformed")
        wheels[key] = {"filename": title, "sha256": digest.removeprefix("sha256:"), "size": size}
    return wheels


def verify_fetched(directory: Path, wheels: Mapping[str, Mapping[str, Any]]) -> None:
    """Each fetched wheel must have the receipt's name, size and digest."""

    for key, wheel in wheels.items():
        path = directory / wheel["filename"]
        if not path.is_file():
            raise ReleaseDistError(f"the {key} {wheel['filename']} was not fetched")
        content = path.read_bytes()
        if len(content) != wheel["size"]:
            raise ReleaseDistError(f"the fetched {key} differs in size from the receipt")
        if hashlib.sha256(content).hexdigest() != wheel["sha256"]:
            raise ReleaseDistError(f"the fetched {key} differs from the receipt digest")


def _wheel_requirements(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ReleaseDistError(f"{path.name} must contain one METADATA entry")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    return [str(value) for value in metadata.get_all("Requires-Dist") or []]


def check_pins_agree(directory: Path, candidate_version: str) -> None:
    """The runner and the rc wheels must name the same toolkit version.

    This is what ``pip check`` in the image build enforces; doing it here fails the
    release before an image is built, without installing anything.
    """

    runners = sorted(directory.glob("custos_runner-*.whl"))
    nautilus = sorted(directory.glob("custos_strategy_toolkit_nautilus-*.whl"))
    if len(runners) != 1 or len(nautilus) != 1:
        raise ReleaseDistError("expected one runner wheel and one Nautilus toolkit wheel")
    expected = {
        "custos-strategy-toolkit": f"custos-strategy-toolkit=={candidate_version}",
        "custos-strategy-toolkit-nautilus": f"custos-strategy-toolkit-nautilus=={candidate_version}",
    }
    runner_pins = {
        requirement.split(";")[0].strip()
        for requirement in _wheel_requirements(runners[0])
        if requirement.startswith("custos-strategy-toolkit")
    }
    if runner_pins != set(expected.values()):
        raise ReleaseDistError(f"the runner wheel pins {sorted(runner_pins)}, not the rc")
    adapter_pins = {
        requirement.split(";")[0].strip()
        for requirement in _wheel_requirements(nautilus[0])
        if requirement.startswith("custos-strategy-toolkit")
    }
    if adapter_pins != {expected["custos-strategy-toolkit"]}:
        raise ReleaseDistError(f"the Nautilus toolkit wheel pins {sorted(adapter_pins)}")


def authority_document(
    rc: Mapping[str, Any], authority_commit: str, wheels: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "candidate_version": rc["candidate_version"],
        "manifest_digest": rc["manifest_digest"],
        "source_commit": rc["source_commit"],
        "receipt_path": rc["receipt_path"],
        "receipt_sha256": rc["receipt_sha256"],
        "authority_commit": authority_commit,
        "wheels": {
            key: {"filename": wheel["filename"], "sha256": wheel["sha256"]}
            for key, wheel in wheels.items()
        },
    }


def _run(runner: Runner, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    completed = runner(list(args), check=False, capture_output=True, text=True, **kwargs)
    if completed.returncode != 0:
        raise ReleaseDistError(
            f"{' '.join(args[:3])} failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    return completed


def authority_commit(root: Path, receipt_path: str, runner: Runner = subprocess.run) -> str:
    """The commit that added the receipt; the registry client records it."""

    added = _run(
        runner,
        ["git", "-C", str(root), "log", "--diff-filter=A", "--format=%H", "-1", "--", receipt_path],
    ).stdout.strip()
    if len(added) != 40:
        raise ReleaseDistError(f"no commit adds {receipt_path}")
    return added


def build_runner(
    root: Path, out: Path, candidate_version: str, runner: Runner = subprocess.run
) -> None:
    """Build the runner sdist and wheel from HEAD with the toolkit pinned to the rc."""

    with tempfile.TemporaryDirectory(prefix="custos-release-") as scratch:
        archive = Path(scratch) / "head.tar"
        _run(
            runner, ["git", "-C", str(root), "archive", "--format=tar", "-o", str(archive), "HEAD"]
        )
        stage = Path(scratch) / "source"
        stage.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(stage, filter="data")
        pyproject = stage / "pyproject.toml"
        pyproject.write_text(
            rewrite_toolkit_pins(pyproject.read_text(encoding="utf-8"), candidate_version),
            encoding="utf-8",
        )
        _run(runner, ["uv", "build", "--no-sources", "--out-dir", str(out)], cwd=stage)


def fetch_toolkit(
    root: Path, out: Path, candidate_version: str, commit: str, runner: Runner = subprocess.run
) -> None:
    """Pull the rc's wheels with the publisher's receipt-pinned registry client."""

    environment = dict(os.environ)
    for sources in (
        ("TOOLKIT_REGISTRY_ACTOR", "GHCR_ACTOR"),
        ("TOOLKIT_REGISTRY_TOKEN", "GHCR_TOKEN"),
    ):
        if not any(environment.get(name) for name in sources):
            raise ReleaseDistError(f"set {' or '.join(sources)} to read the toolkit rc")
    _run(
        runner,
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "custos_strategy_publisher.toolkit_fetch",
            "--output",
            str(out),
            "--custos-root",
            str(root),
            "--toolkit-version",
            candidate_version,
            "--authority-commit",
            commit,
        ],
        cwd=root / "packages" / "custos-strategy-publisher",
        env=environment,
    )


def build_release_dist(root: Path, out: Path, runner: Runner = subprocess.run) -> dict[str, Any]:
    pin = _load_pin_check()
    rc = pin.check(root)
    if not rc["toolkit_matches_rc"]:
        raise ReleaseDistError(
            "the toolkit differs from the authorized rc "
            f"{rc['candidate_version']}: {', '.join(rc['differing_files'])}"
        )
    receipt = json.loads((root / rc["receipt_path"]).read_text(encoding="utf-8"))
    if receipt.get("candidate_version") != rc["candidate_version"]:
        raise ReleaseDistError("the receipt names another candidate version")
    wheels = receipt_wheels(receipt)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ReleaseDistError(f"{out} must be empty")
    commit = authority_commit(root, rc["receipt_path"], runner)
    build_runner(root, out, rc["candidate_version"], runner)
    fetch_toolkit(root, out, rc["candidate_version"], commit, runner)
    verify_fetched(out, wheels)
    check_pins_agree(out, rc["candidate_version"])
    document = authority_document(rc, commit, wheels)
    (out / AUTHORITY_FILE).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        document = build_release_dist(args.root.resolve(), args.out.resolve())
    except (RuntimeError, OSError, ValueError) as error:
        print(f"release dist: {error}", file=sys.stderr)
        return 2
    print(
        f"release dist: runner from HEAD, toolkit {document['candidate_version']} "
        f"({document['manifest_digest']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

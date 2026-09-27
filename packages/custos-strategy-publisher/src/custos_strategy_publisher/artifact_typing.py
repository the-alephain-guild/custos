"""Fail-closed strict typing gate for a generated strategy artifact package."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tempfile
import venv
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath

from .artifact_build import StrategyTypingRequestV1, typing_receipt_bytes
from .model import ENGINE_VERSION

MYPY_VERSION = "1.19.1"


class StrategyTypingError(RuntimeError):
    """The generated strategy package did not satisfy the production typing gate."""


def _safe_target(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise StrategyTypingError(f"unsafe typing input path: {relative}")
    return root.joinpath(*path.parts)


def _materialize_files(root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    for relative, content in files:
        target = _safe_target(root, relative)
        if target.exists() and target.read_bytes() != content:
            raise StrategyTypingError(f"typing dependency collision: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@dataclass(frozen=True, slots=True)
class StrictStrategyTypingVerifier:
    """Run exact-version mypy against source plus receipt-pinned toolkit wheels."""

    timeout_seconds: int = 180

    def verify(self, request: StrategyTypingRequestV1) -> bytes:
        if sys.version_info[:2] != (3, 12):
            raise StrategyTypingError("strategy typing requires Python 3.12")
        installed_mypy = metadata.version("mypy")
        if installed_mypy != MYPY_VERSION:
            raise StrategyTypingError(
                f"strategy typing requires mypy {MYPY_VERSION}, found {installed_mypy}"
            )
        installed_engine = metadata.version("nautilus-trader")
        if installed_engine != ENGINE_VERSION:
            raise StrategyTypingError(
                f"strategy typing requires nautilus-trader {ENGINE_VERSION}, "
                f"found {installed_engine}"
            )
        if not any(path == f"{request.package}/py.typed" for path in request.package_files):
            raise StrategyTypingError("strategy package py.typed marker is absent")

        with tempfile.TemporaryDirectory(prefix="strategy-artifact-typing-") as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            runtime_root = workspace / "runtime"
            venv.EnvBuilder(with_pip=False, system_site_packages=True).create(runtime_root)
            scheme = "nt" if os.name == "nt" else "posix_prefix"
            dependency_root = Path(
                sysconfig.get_path(
                    "purelib",
                    scheme=scheme,
                    vars={"base": str(runtime_root), "platbase": str(runtime_root)},
                )
            )
            producer_site_packages = Path(sysconfig.get_path("purelib"))
            (dependency_root / "producer-environment.pth").write_text(
                f"{producer_site_packages}\n",
                encoding="utf-8",
            )
            runtime_python = runtime_root / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            _materialize_files(dependency_root, request.base_contracts_wheel.files)
            _materialize_files(dependency_root, request.nautilus_wheel.files)
            for relative, content in sorted(request.package_files.items()):
                target = _safe_target(source_root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            config = workspace / "mypy.ini"
            config.write_text(
                "\n".join(
                    (
                        "[mypy]",
                        "python_version = 3.12",
                        "strict = True",
                        "ignore_missing_imports = False",
                        "follow_imports = normal",
                        "namespace_packages = True",
                        "explicit_package_bases = True",
                        "warn_unused_configs = True",
                        "incremental = False",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("MYPYPATH", None)
            command = (
                sys.executable,
                "-m",
                "mypy",
                "--config-file",
                str(config),
                "--python-executable",
                str(runtime_python),
                "--strict",
                "--show-error-codes",
                str(source_root / request.package),
            )
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                diagnostics = "\n".join(
                    part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
                )
                raise StrategyTypingError(
                    "strict strategy typing failed"
                    + (f":\n{diagnostics}" if diagnostics else " without diagnostics")
                )

        return typing_receipt_bytes(
            request,
            verifier=(f"mypy-{installed_mypy}-python-3.12-nautilus-trader-{installed_engine}"),
            verified=True,
        )


__all__ = ["StrictStrategyTypingVerifier", "StrategyTypingError"]

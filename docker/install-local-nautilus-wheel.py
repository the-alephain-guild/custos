"""Install an explicitly selected local engine wheel in the image builder."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path


def install(directory: Path, name: str, digest: str) -> None:
    """Validate local wheel bytes and the existing version contract before installation."""
    if not name:
        if digest:
            raise ValueError("a local wheel digest requires a wheel name")
        return
    if (
        Path(name).name != name
        or not name.startswith("nautilus_trader-")
        or not name.endswith(".whl")
    ):
        raise ValueError("local NautilusTrader wheel must be a wheel basename")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("local NautilusTrader wheel requires a SHA-256 digest")
    wheel = directory / name
    with wheel.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("local NautilusTrader wheel digest does not match")
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("local NautilusTrader wheel must contain one METADATA entry")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    if metadata["Name"] != "nautilus-trader" or metadata["Version"] != importlib.metadata.version(
        "nautilus-trader"
    ):
        raise ValueError("local NautilusTrader wheel must preserve the locked engine version")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)


if __name__ == "__main__":
    install(Path(sys.argv[1]), sys.argv[2], sys.argv[3])

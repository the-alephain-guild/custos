"""Deterministic wheel construction and fail-closed pre-import inspection."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
from pathlib import PurePosixPath

from packaging.specifiers import InvalidSpecifier, SpecifierSet

_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_MAX_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 250
_FIXED_ZIP_TIMESTAMP = (2024, 1, 1, 0, 0, 0)


class WheelArchiveError(ValueError):
    """A wheel is unsafe, ambiguous, incompatible, or non-deterministic."""


@dataclass(frozen=True, slots=True)
class VerifiedWheelV1:
    distribution: str
    version: str
    requires_python: str
    files: tuple[tuple[str, bytes], ...]

    def content(self, path: str) -> bytes:
        matches = [content for name, content in self.files if name == path]
        if len(matches) != 1:
            raise WheelArchiveError(f"expected exactly one wheel member: {path}")
        return matches[0]


def _safe_member_name(value: str) -> str:
    if "\\" in value or value.endswith("/"):
        raise WheelArchiveError(f"wheel member path is unsafe: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise WheelArchiveError(f"wheel member path is unsafe: {value}")
    if any(not _SAFE_COMPONENT_RE.fullmatch(part) for part in path.parts):
        raise WheelArchiveError(f"wheel member path contains unsupported characters: {value}")
    return path.as_posix()


def _normalized_distribution(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if not normalized or not _SAFE_COMPONENT_RE.fullmatch(normalized):
        raise WheelArchiveError("wheel distribution name is invalid")
    return normalized


def inspect_wheel(
    content: bytes,
    *,
    distribution: str,
    version: str,
    requires_python: str,
    required_modules: tuple[str, ...],
) -> VerifiedWheelV1:
    """Inspect every archive member before any content can enter an import path."""

    expected_distribution = _normalized_distribution(distribution)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise WheelArchiveError("wheel is not a valid ZIP archive") from error
    files: list[tuple[str, bytes]] = []
    names: set[str] = set()
    folded_names: set[str] = set()
    total_bytes = 0
    metadata_paths: list[str] = []
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                raise WheelArchiveError("wheel contains an explicit directory entry")
            name = _safe_member_name(info.filename)
            folded = name.casefold()
            if name in names or folded in folded_names:
                raise WheelArchiveError(f"wheel contains duplicate member: {name}")
            names.add(name)
            folded_names.add(folded)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise WheelArchiveError(f"wheel contains symlink: {name}")
            if info.flag_bits & 0x1:
                raise WheelArchiveError(f"wheel contains encrypted member: {name}")
            if info.file_size > _MAX_MEMBER_BYTES:
                raise WheelArchiveError(f"wheel member exceeds size limit: {name}")
            total_bytes += info.file_size
            if total_bytes > _MAX_TOTAL_BYTES:
                raise WheelArchiveError("wheel exceeds total uncompressed size limit")
            if (
                info.file_size > 0
                and info.compress_size > 0
                and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise WheelArchiveError(f"wheel member exceeds compression ratio limit: {name}")
            if name.endswith(".pth"):
                raise WheelArchiveError(f"wheel contains executable path hook: {name}")
            payload = archive.read(info)
            if len(payload) != info.file_size:
                raise WheelArchiveError(f"wheel member size changed while read: {name}")
            files.append((name, payload))
            if name.endswith(".dist-info/METADATA"):
                metadata_paths.append(name)
    if len(metadata_paths) != 1:
        raise WheelArchiveError("wheel must contain exactly one METADATA file")
    metadata = BytesParser(policy=compat32).parsebytes(dict(files)[metadata_paths[0]])
    actual_requires_python = str(metadata.get("Requires-Python", ""))
    try:
        python_compatibility_matches = SpecifierSet(actual_requires_python) == SpecifierSet(
            requires_python
        )
    except InvalidSpecifier:
        python_compatibility_matches = False
    if (
        _normalized_distribution(str(metadata.get("Name", ""))) != expected_distribution
        or metadata.get("Version") != version
        or not python_compatibility_matches
    ):
        raise WheelArchiveError("wheel metadata compatibility differs")
    top_level = {PurePosixPath(name).parts[0] for name, _ in files}
    if {"shared", "pandas_ta"} & top_level:
        raise WheelArchiveError("wheel exposes a forbidden legacy top-level namespace")
    for module in required_modules:
        if not any(name == f"{module}.py" or name.startswith(f"{module}/") for name, _ in files):
            raise WheelArchiveError(f"wheel required module is absent: {module}")
    return VerifiedWheelV1(
        distribution=distribution,
        version=version,
        requires_python=requires_python,
        files=tuple(sorted(files)),
    )


def _record_digest(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(_safe_member_name(path), date_time=_FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def build_wheel(
    package_files: dict[str, bytes],
    *,
    distribution: str,
    version: str,
    requires_python: str,
    dependencies: tuple[str, ...],
    entry_points: dict[str, dict[str, str]],
    generator: str = "custos-strategy-publisher-v1",
) -> bytes:
    """Build one pure-Python wheel with no host, timestamp, or compression entropy.

    `generator` is recorded in the WHEEL file; it is part of the wheel's bytes, so a
    rebuild that must match an earlier release passes the name that release used.
    """

    normalized_distribution = _normalized_distribution(distribution).replace("-", "_")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?", version):
        raise WheelArchiveError("wheel version is invalid")
    dist_info = f"{normalized_distribution}-{version}.dist-info"
    files = {_safe_member_name(path): bytes(content) for path, content in package_files.items()}
    metadata_lines = [
        "Metadata-Version: 2.3",
        f"Name: {distribution}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
        "Summary: Alephain strategy runtime artifact",
        *(f"Requires-Dist: {dependency}" for dependency in dependencies),
        "",
        "",
    ]
    files[f"{dist_info}/METADATA"] = "\n".join(metadata_lines).encode("utf-8")
    files[f"{dist_info}/WHEEL"] = (
        f"Wheel-Version: 1.0\nGenerator: {generator}\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode()
    entry_point_lines: list[str] = []
    for group in sorted(entry_points):
        entry_point_lines.append(f"[{group}]")
        for name, target in sorted(entry_points[group].items()):
            entry_point_lines.append(f"{name} = {target}")
        entry_point_lines.append("")
    files[f"{dist_info}/entry_points.txt"] = "\n".join(entry_point_lines).encode("utf-8")
    record_path = f"{dist_info}/RECORD"
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for path, payload in sorted(files.items()):
        writer.writerow((path, _record_digest(payload), len(payload)))
    writer.writerow((record_path, "", ""))
    files[record_path] = record_buffer.getvalue().encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, payload in sorted(files.items()):
            archive.writestr(_zip_info(path), payload)
    return output.getvalue()


__all__ = [
    "VerifiedWheelV1",
    "WheelArchiveError",
    "build_wheel",
    "inspect_wheel",
]

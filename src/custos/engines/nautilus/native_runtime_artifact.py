"""Measure an installed native runtime against its exact local wheel, without admission."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.util
import marshal
import os
import platform
import re
import stat
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO

_DIGEST = re.compile(r"[0-9a-f]{64}")
_PACKAGE = "nautilus_trader"
_DISTRIBUTION = "nautilus-trader"
_MODULES = (
    "nautilus_trader",
    "nautilus_trader.live",
    "nautilus_trader.adapters.sandbox",
    "nautilus_trader._libnautilus",
)
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBERS = 8192
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_BYTECODE_BYTES = 16 * 1024 * 1024
_CHUNK = 1024 * 1024


class NativeRuntimeMeasurementError(ValueError):
    """The local archive and actual runtime cannot be established as the same payload."""


@dataclass(frozen=True, slots=True)
class InstalledNativeRuntime:
    distribution_version: str
    package_version: str
    native_package_version: str
    distribution_root: Path
    environment_root: Path
    module_paths: tuple[tuple[str, Path], ...]


@dataclass(frozen=True, slots=True)
class NativeRuntimePayloadFile:
    archive_member: str
    installed_path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NativeRuntimeBytecodeFile:
    source_member: str
    installed_path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NativeRuntimeMeasurement:
    archive_path: Path
    archive_sha256: str
    archive_bytes: int
    distribution_version: str
    native_package_version: str
    wheel_tags: tuple[str, ...]
    module_paths: tuple[tuple[str, Path], ...]
    payload_files: tuple[NativeRuntimePayloadFile, ...]
    bytecode_files: tuple[NativeRuntimeBytecodeFile, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NativeRuntimeMeasurementError(message)


def _plain_path(path: Path) -> Path:
    path = path.absolute()
    _require(".." not in path.parts, "runtime payload path contains parent traversal")
    for component in (path, *path.parents):
        _require(not component.is_symlink(), "runtime payload path contains a symlink")
    return path


@contextmanager
def _stable_file(path: Path, limit: int) -> Iterator[BinaryIO]:
    path = _plain_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode), "runtime payload is not a regular file")
        _require(0 <= before.st_size <= limit, "runtime payload exceeds the size bound")
        yield stream
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        _require(
            all(getattr(before, field) == getattr(after, field) for field in fields),
            "runtime payload changed during measurement",
        )


def _hash(stream: BinaryIO, limit: int = _MAX_PAYLOAD_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
        _require(size <= limit, "runtime payload grew beyond the size bound")
    return digest.hexdigest(), size


def _observe_installation() -> InstalledNativeRuntime:
    distributions = [
        distribution
        for distribution in importlib.metadata.distributions()
        if re.sub(r"[-_.]+", "-", distribution.metadata.get("Name", "")).lower() == _DISTRIBUTION
    ]
    _require(len(distributions) == 1, "native runtime distribution is missing or ambiguous")
    distribution = distributions[0]
    modules = tuple((name, importlib.import_module(name)) for name in _MODULES)
    paths = []
    for name, module in modules:
        origin = getattr(module, "__file__", None)
        _require(isinstance(origin, str), "native runtime module has no file origin")
        paths.append((name, Path(origin)))
    core = importlib.import_module("nautilus_trader.core")
    package_version = getattr(modules[0][1], "__version__", None)
    native_version = getattr(core, "NAUTILUS_VERSION", None)
    _require(isinstance(package_version, str), "native package version is missing")
    _require(
        isinstance(native_version, str) and bool(native_version),
        "native base package version is missing",
    )
    return InstalledNativeRuntime(
        distribution_version=distribution.version,
        package_version=package_version,
        native_package_version=native_version,
        distribution_root=Path(distribution.locate_file("")),
        environment_root=Path(sys.prefix).resolve(),
        module_paths=tuple(paths),
    )


def _supported_tags() -> frozenset[str]:
    # packaging is an explicit dependency of the optional Nautilus adapter.
    from packaging.tags import sys_tags

    return frozenset(str(tag) for tag in sys_tags())


def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    _require(0 < len(entries) <= _MAX_MEMBERS, "wheel member count exceeds the bound")
    result = {}
    total = 0
    for entry in entries:
        path = PurePosixPath(entry.filename)
        _require(
            not path.is_absolute()
            and bool(path.parts)
            and ".." not in path.parts
            and "\\" not in entry.filename
            and "\x00" not in entry.filename,
            "wheel member path is invalid",
        )
        _require(entry.filename not in result, "wheel has duplicate member paths")
        mode = stat.S_IFMT(entry.external_attr >> 16)
        _require(mode != stat.S_IFLNK, "wheel member is a symlink")
        if entry.is_dir():
            _require(mode in (0, stat.S_IFDIR), "wheel directory type is invalid")
        else:
            _require(mode in (0, stat.S_IFREG), "wheel member is not a regular payload")
        _require(not entry.flag_bits & 1, "encrypted wheel members are unsupported")
        total += entry.file_size
        _require(total <= _MAX_PAYLOAD_BYTES, "wheel payload exceeds the size bound")
        result[entry.filename] = entry
    return result


def _wheel_metadata(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    path: Path,
    expected_version: str,
) -> tuple[str, tuple[str, ...]]:
    from packaging.specifiers import SpecifierSet
    from packaging.tags import parse_tag
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version

    name, version, _, filename_tags = parse_wheel_filename(path.name)
    _require(name == _DISTRIBUTION and str(version) == expected_version, "wheel version differs")
    metadata_paths = [name for name in members if name.endswith(".dist-info/METADATA")]
    _require(len(metadata_paths) == 1, "wheel metadata is missing or ambiguous")
    metadata_path = metadata_paths[0]
    _require(len(PurePosixPath(metadata_path).parts) == 2, "wheel metadata path is invalid")
    wheel_path = metadata_path.removesuffix("METADATA") + "WHEEL"
    _require(wheel_path in members, "wheel tag metadata is missing")
    for entry in (metadata_path, wheel_path):
        _require(members[entry].file_size <= _CHUNK, "wheel metadata exceeds the size bound")
    metadata = BytesParser().parsebytes(archive.read(metadata_path))
    wheel = BytesParser().parsebytes(archive.read(wheel_path))
    for field in ("Name", "Version", "Requires-Python"):
        _require(len(metadata.get_all(field, [])) == 1, "wheel metadata fields are ambiguous")
    _require(canonicalize_name(metadata["Name"]) == _DISTRIBUTION, "wheel distribution differs")
    _require(metadata["Version"] == expected_version, "wheel metadata version differs")
    _require(
        Version(platform.python_version()) in SpecifierSet(metadata["Requires-Python"]),
        "wheel Python requirement is incompatible",
    )
    _require(wheel.get_all("Wheel-Version") == ["1.0"], "unsupported wheel format")
    _require(wheel.get_all("Root-Is-Purelib") == ["false"], "native wheel cannot be purelib")
    tag_fields = tuple(wheel.get_all("Tag", []))
    _require(
        bool(tag_fields) and len(set(tag_fields)) == len(tag_fields),
        "wheel tags are missing or duplicated",
    )
    tags = tuple(sorted({str(tag) for field in tag_fields for tag in parse_tag(field)}))
    _require(set(tags) == {str(tag) for tag in filename_tags}, "wheel filename tags differ")
    _require(bool(set(tags) & _supported_tags()), "wheel tag is incompatible with this process")
    return metadata_path, tags


def _bytecode_sources(root: Path, members: set[str]) -> dict[str, tuple[str, int]]:
    sources = {}
    for member in members:
        if not member.endswith(".py"):
            continue
        for optimization in (0, 1, 2):
            cache = Path(
                importlib.util.cache_from_source(
                    str(root / member), optimization=str(optimization) if optimization else ""
                )
            )
            sources[cache.relative_to(root).as_posix()] = (member, optimization)
    return sources


def _verify_bytecode(
    path: Path,
    source_member: str,
    optimization: int,
    root: Path,
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> NativeRuntimeBytecodeFile:
    _require(members[source_member].file_size <= _MAX_SOURCE_BYTES, "cached source exceeds bound")
    source = archive.read(members[source_member])
    with _stable_file(path, _MAX_BYTECODE_BYTES) as stream:
        data = stream.read(_MAX_BYTECODE_BYTES + 1)
    _require(16 <= len(data) <= _MAX_BYTECODE_BYTES, "invalid bytecode size")
    _require(data[:4] == importlib.util.MAGIC_NUMBER, "bytecode belongs to another interpreter")
    flags = int.from_bytes(data[4:8], "little")
    _require(flags in (0, 1, 3), "invalid bytecode flags")
    source_path = root / source_member
    if flags & 1:
        _require(data[8:16] == importlib.util.source_hash(source), "bytecode source hash differs")
    else:
        metadata = source_path.stat()
        _require(
            int.from_bytes(data[8:12], "little") == int(metadata.st_mtime) & 0xFFFFFFFF
            and int.from_bytes(data[12:16], "little") == len(source) & 0xFFFFFFFF,
            "bytecode source timestamp or size differs",
        )
    # Compile trusted archive source without executing it. Never unmarshal an
    # unchecked cache and never trust its filename or header as code integrity.
    expected = compile(source, str(source_path), "exec", dont_inherit=True, optimize=optimization)
    _require(data[16:] == marshal.dumps(expected), "bytecode payload differs from archived source")
    return NativeRuntimeBytecodeFile(
        source_member, path, len(data), hashlib.sha256(data).hexdigest()
    )


def _measure(
    wheel_path: Path, archive_sha256: str, distribution_version: str
) -> NativeRuntimeMeasurement:
    _require(_DIGEST.fullmatch(archive_sha256) is not None, "invalid archive digest")
    observed = _observe_installation()
    _require(
        observed.distribution_version == distribution_version
        and observed.package_version == distribution_version,
        "installed runtime version differs from the requested archive",
    )
    root = _plain_path(observed.distribution_root)
    environment = _plain_path(observed.environment_root)
    _require(root.is_relative_to(environment), "runtime installation is outside this environment")
    _require(tuple(name for name, _ in observed.module_paths) == _MODULES, "module set differs")
    with _stable_file(wheel_path, _MAX_ARCHIVE_BYTES) as stream:
        digest, archive_bytes = _hash(stream, _MAX_ARCHIVE_BYTES)
        _require(digest == archive_sha256, "measured archive digest differs")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members = _archive_members(archive)
            metadata_path, tags = _wheel_metadata(
                archive, members, wheel_path, distribution_version
            )
            package_members = {
                name
                for name, entry in members.items()
                if name.startswith(_PACKAGE + "/") and not entry.is_dir()
            }
            required = package_members | {
                metadata_path,
                metadata_path.removesuffix("METADATA") + "WHEEL",
            }
            origins = []
            for module, origin in observed.module_paths:
                origin = _plain_path(origin)
                _require(origin.is_relative_to(root), "imported module shadows this installation")
                member = origin.relative_to(root).as_posix()
                _require(
                    member in package_members, "imported module is absent from archive payload"
                )
                origins.append((module, origin))
            payloads = []
            for name in sorted(required):
                path = root.joinpath(*PurePosixPath(name).parts)
                with _stable_file(path, _MAX_PAYLOAD_BYTES) as installed:
                    installed_digest, installed_bytes = _hash(installed, members[name].file_size)
                with archive.open(members[name]) as packed:
                    packed_digest, packed_bytes = _hash(packed, members[name].file_size)
                _require(
                    installed_bytes == packed_bytes == members[name].file_size
                    and installed_digest == packed_digest,
                    "installed runtime payload differs from the archive",
                )
                payloads.append(
                    NativeRuntimePayloadFile(name, path, installed_bytes, installed_digest)
                )
            installed_members = set()
            caches = _bytecode_sources(root, package_members)
            bytecodes = []
            for index, path in enumerate((root / _PACKAGE).rglob("*")):
                _require(index < _MAX_MEMBERS, "installed package exceeds the path count bound")
                _require(not path.is_symlink(), "installed package contains a symlink")
                if not path.is_file():
                    continue
                member = path.relative_to(root).as_posix()
                if member in package_members:
                    installed_members.add(member)
                else:
                    _require(member in caches, "installed package has extra executable payload")
                    source, optimization = caches[member]
                    bytecodes.append(
                        _verify_bytecode(path, source, optimization, root, archive, members)
                    )
            _require(
                installed_members == package_members,
                "installed package has extra or missing payload",
            )
    return NativeRuntimeMeasurement(
        archive_path=wheel_path.absolute(),
        archive_sha256=digest,
        archive_bytes=archive_bytes,
        distribution_version=distribution_version,
        native_package_version=observed.native_package_version,
        wheel_tags=tags,
        module_paths=tuple(origins),
        payload_files=tuple(payloads),
        bytecode_files=tuple(sorted(bytecodes, key=lambda item: str(item.installed_path))),
    )


async def measure_native_runtime_archive(
    wheel_path: Path, *, archive_sha256: str, distribution_version: str
) -> NativeRuntimeMeasurement:
    """Return byte evidence only; registered capability and source admission remain separate."""
    try:
        return await asyncio.to_thread(_measure, wheel_path, archive_sha256, distribution_version)
    except NativeRuntimeMeasurementError:
        raise
    except (OSError, ImportError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise NativeRuntimeMeasurementError("native runtime measurement failed") from error

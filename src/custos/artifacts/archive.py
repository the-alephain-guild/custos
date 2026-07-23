from __future__ import annotations

import ast
import base64
import configparser
import csv
import hashlib
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import ArchiveLimitsV1


@dataclass(frozen=True, slots=True)
class QuarantinedWheel:
    root: Path
    verified_entry_point: str
    archive_member_count: int
    total_uncompressed_bytes: int

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _unsafe(detail: str) -> ArtifactVerificationError:
    return ArtifactVerificationError(ArtifactVerificationCode.ARCHIVE_UNSAFE, detail)


def _normalized_name(name: str, *, is_directory: bool, max_path_bytes: int) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise _unsafe("archive member name is empty, NUL-bearing, or uses backslashes")
    candidate = name[:-1] if is_directory and name.endswith("/") else name
    if not candidate or candidate.startswith(("/", "//")):
        raise _unsafe("archive member path is absolute or empty")
    if len(candidate) >= 2 and candidate[1] == ":":
        raise _unsafe("archive member path contains a drive prefix")
    raw_parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise _unsafe("archive member path contains an unsafe segment")
    if len(candidate.encode("utf-8")) > max_path_bytes:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED,
            "archive member path exceeds the signed policy limit",
        )
    normalized = unicodedata.normalize("NFC", candidate)
    if normalized != candidate:
        raise _unsafe("archive member path is not NFC-normalized")
    path = PurePosixPath(candidate)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _unsafe("archive member path is not normalized relative POSIX")
    return path.as_posix()


def _validate_info_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise _unsafe("encrypted wheel members are forbidden")
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    allowed = {0, stat.S_IFREG, stat.S_IFDIR}
    if kind not in allowed:
        raise _unsafe("archive member is a link, device, FIFO, or socket")
    if info.is_dir() and kind not in {0, stat.S_IFDIR}:
        raise _unsafe("directory archive member has a non-directory mode")
    if not info.is_dir() and kind == stat.S_IFDIR:
        raise _unsafe("file archive member has a directory mode")


def _validate_inventory(
    infos: list[zipfile.ZipInfo], limits: ArchiveLimitsV1
) -> tuple[dict[str, zipfile.ZipInfo], int]:
    if len(infos) > limits.max_members:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED,
            "archive member count exceeds the signed policy limit",
        )
    by_name: dict[str, zipfile.ZipInfo] = {}
    collision_keys: set[str] = set()
    total = 0
    for info in infos:
        _validate_info_type(info)
        name = _normalized_name(
            info.filename,
            is_directory=info.is_dir(),
            max_path_bytes=limits.max_path_bytes,
        )
        collision_key = unicodedata.normalize("NFC", name).casefold()
        if name in by_name or collision_key in collision_keys:
            raise _unsafe("archive contains duplicate or normalization-colliding members")
        by_name[name] = info
        collision_keys.add(collision_key)
        if info.file_size > limits.max_member_bytes:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED,
                "archive member exceeds the signed per-member size limit",
            )
        total += info.file_size
        if total > limits.max_total_uncompressed_bytes:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED,
                "archive exceeds the signed total uncompressed size limit",
            )
        if (
            info.file_size
            and info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio
        ):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ARCHIVE_LIMIT_EXCEEDED,
                "archive member exceeds the signed compression-ratio limit",
            )
    return by_name, total


def _extract_to_quarantine(
    archive: zipfile.ZipFile,
    inventory: dict[str, zipfile.ZipInfo],
    root: Path,
) -> None:
    for name, info in inventory.items():
        destination = root.joinpath(*PurePosixPath(name).parts)
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise _unsafe("archive member escapes quarantine") from error
        if info.is_dir():
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        written = 0
        try:
            with archive.open(info, "r") as source, destination.open("xb") as target:
                os.chmod(destination, 0o600)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size:
                        raise ArtifactVerificationError(
                            ArtifactVerificationCode.ARCHIVE_INVALID,
                            "archive member expanded beyond its declared size",
                        )
                    target.write(chunk)
        except zipfile.BadZipFile as error:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ARCHIVE_CRC_MISMATCH,
                "archive member CRC verification failed",
            ) from error
        if written != info.file_size:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ARCHIVE_INVALID,
                "archive member size differs from its central-directory record",
            )


def _decode_record_hash(value: str) -> bytes:
    if not value.startswith("sha256="):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel RECORD uses an unsupported or missing hash algorithm",
        )
    encoded = value.removeprefix("sha256=")
    try:
        result = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel RECORD hash is not valid base64url",
        ) from error
    if len(result) != 32:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel RECORD sha256 hash is not 32 bytes",
        )
    return result


def _verify_wheel_record(root: Path, file_names: set[str]) -> None:
    record_names = sorted(name for name in file_names if name.endswith(".dist-info/RECORD"))
    if len(record_names) != 1:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel must contain exactly one dist-info/RECORD",
        )
    record_name = record_names[0]
    try:
        rows = list(csv.reader((root / record_name).read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel RECORD cannot be parsed",
        ) from error
    recorded: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.WHEEL_RECORD_INVALID,
                "wheel RECORD row must contain exactly three columns",
            )
        name = _normalized_name(row[0], is_directory=False, max_path_bytes=4096)
        if name in recorded:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.WHEEL_RECORD_INVALID,
                "wheel RECORD contains duplicate paths",
            )
        recorded[name] = (row[1], row[2])
    if set(recorded) != file_names:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.WHEEL_RECORD_INVALID,
            "wheel RECORD does not cover exactly every archive file",
        )
    for name, (encoded_hash, encoded_size) in recorded.items():
        if name == record_name:
            if encoded_hash or encoded_size:
                raise ArtifactVerificationError(
                    ArtifactVerificationCode.WHEEL_RECORD_INVALID,
                    "wheel RECORD self-row must omit hash and size",
                )
            continue
        payload = (root / name).read_bytes()
        expected_hash = _decode_record_hash(encoded_hash)
        try:
            expected_size = int(encoded_size)
        except ValueError as error:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.WHEEL_RECORD_INVALID,
                "wheel RECORD size is not an integer",
            ) from error
        if expected_size != len(payload) or expected_hash != hashlib.sha256(payload).digest():
            raise ArtifactVerificationError(
                ArtifactVerificationCode.WHEEL_RECORD_INVALID,
                "wheel RECORD member evidence does not match extracted bytes",
            )


def _entry_point_declared(root: Path, file_names: set[str], group: str, value: str) -> bool:
    metadata_names = sorted(
        name for name in file_names if name.endswith(".dist-info/entry_points.txt")
    )
    if len(metadata_names) != 1:
        return False
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string((root / metadata_names[0]).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error):
        return False
    return parser.has_section(group) and any(
        configured.strip() == value for _, configured in parser.items(group)
    )


def _entry_point_exists_in_ast(root: Path, file_names: set[str], entry_point: str) -> bool:
    module, separator, attribute = entry_point.partition(":")
    if (
        not separator
        or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module)
        or not re.fullmatch(r"[A-Za-z_]\w*", attribute)
    ):
        return False
    module_path = module.replace(".", "/")
    candidates = [f"{module_path}.py", f"{module_path}/__init__.py"]
    present = [candidate for candidate in candidates if candidate in file_names]
    if len(present) != 1:
        return False
    try:
        tree = ast.parse((root / present[0]).read_bytes(), filename=present[0])
    except (OSError, SyntaxError, ValueError):
        return False
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            exported.add(node.name)
        elif isinstance(node, ast.Assign):
            exported.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exported.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            exported.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return attribute in exported


def _verify_required_runtime_artifacts(
    root: Path,
    file_names: set[str],
    required_runtime_artifacts: Sequence[Mapping[str, object]],
) -> None:
    seen_names: set[str] = set()
    for artifact in required_runtime_artifacts:
        name = artifact.get("name")
        role = artifact.get("role")
        size_bytes = artifact.get("size_bytes")
        digest = artifact.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or name in seen_names
            or role != "runtime_artifact"
            or type(size_bytes) is not int
            or size_bytes <= 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                "required runtime artifact metadata is invalid",
            )
        seen_names.add(name)
        candidates = sorted(
            candidate
            for candidate in file_names
            if candidate == name or candidate.endswith(f"/{name}")
        )
        if len(candidates) != 1:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                f"wheel does not contain one exact runtime artifact: {name}",
            )
        payload = (root / candidates[0]).read_bytes()
        if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != digest:
            raise ArtifactVerificationError(
                ArtifactVerificationCode.MEMBER_SET_MISMATCH,
                f"wheel runtime artifact bytes differ: {name}",
            )


def quarantine_wheel(
    wheel_path: Path,
    *,
    entry_point_group: str,
    entry_point: str,
    limits: ArchiveLimitsV1,
    quarantine_parent: Path,
    required_runtime_artifacts: Sequence[Mapping[str, object]] = (),
) -> QuarantinedWheel:
    root: Path | None = None
    try:
        with zipfile.ZipFile(wheel_path, "r") as archive:
            inventory, total = _validate_inventory(archive.infolist(), limits)
            if quarantine_parent.is_symlink():
                raise _unsafe("quarantine parent cannot be a symlink")
            quarantine_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="strategy-wheel-", dir=quarantine_parent))
            os.chmod(root, 0o700)
            _extract_to_quarantine(archive, inventory, root)
        file_names = {name for name, info in inventory.items() if not info.is_dir()}
        _verify_wheel_record(root, file_names)
        _verify_required_runtime_artifacts(
            root,
            file_names,
            required_runtime_artifacts,
        )
        if not _entry_point_declared(root, file_names, entry_point_group, entry_point):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ENTRY_POINT_INVALID,
                "wheel metadata does not declare the verified strategy entry point",
            )
        if not _entry_point_exists_in_ast(root, file_names, entry_point):
            raise ArtifactVerificationError(
                ArtifactVerificationCode.ENTRY_POINT_INVALID,
                "verified strategy entry point is absent from wheel source",
            )
        return QuarantinedWheel(root, entry_point, len(inventory), total)
    except ArtifactVerificationError:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise ArtifactVerificationError(
            ArtifactVerificationCode.ARCHIVE_INVALID,
            "strategy wheel is not a readable safe ZIP archive",
        ) from error

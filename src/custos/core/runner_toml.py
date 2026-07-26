"""Public runner authority metadata persisted at ``~/.arx/runner.toml``.

The file contains only non-secret binding metadata.  The opaque machine
credential and Ed25519 private key live together in the sops+age machine
vault referenced by ``machine_vault_path``.
"""

from __future__ import annotations

import os
import stat
import tomllib
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_WORLD_GROUP_BITS = 0o077


@dataclass(frozen=True, slots=True)
class RunnerToml:
    tenant_id: str
    runner_id: str
    backend_url: str
    credential_id: str
    credential_version: int
    credential_valid_until: str
    machine_key_id: str
    machine_vault_path: str
    enrolled_at: str

    def __post_init__(self) -> None:
        if not self.tenant_id or any(character.isspace() for character in self.tenant_id):
            raise ValueError("tenant_id must be non-empty and contain no whitespace")
        for field in ("runner_id", "credential_id"):
            try:
                parsed = UUID(getattr(self, field))
            except ValueError as exc:
                raise ValueError(f"{field} must be a UUID") from exc
            if parsed.int == 0:
                raise ValueError(f"{field} must not be nil")
        if type(self.credential_version) is not int or self.credential_version < 1:
            raise ValueError("credential_version must be positive")
        _parse_timestamp(self.credential_valid_until, "credential_valid_until")
        _parse_timestamp(self.enrolled_at, "enrolled_at")
        parsed_backend = urllib.parse.urlsplit(self.backend_url)
        if not parsed_backend.scheme or not parsed_backend.hostname:
            raise ValueError("backend_url must be an absolute URL")
        vault_path = Path(self.machine_vault_path).expanduser()
        if not vault_path.is_absolute():
            raise ValueError("machine_vault_path must be absolute")
        if not self.machine_key_id.startswith("ed25519-"):
            raise ValueError("machine_key_id must identify an Ed25519 key")

    @staticmethod
    def write(path: Path, record: RunnerToml) -> None:
        path = path.expanduser().resolve()
        path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(path.parent, _DIR_MODE)
        temporary = path.parent / f".{path.name}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
            try:
                os.write(descriptor, _serialise(record).encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(temporary, _FILE_MODE)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def read(path: Path) -> RunnerToml:
        path = path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found; run `arx-runner enroll` before starting the runner"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & _WORLD_GROUP_BITS:
            raise PermissionError(f"{path} must have mode 0600")
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        if set(document) != set(_FIELDS):
            missing = sorted(set(_FIELDS) - set(document))
            unexpected = sorted(set(document) - set(_FIELDS))
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unexpected:
                detail.append(f"unexpected {', '.join(unexpected)}")
            raise ValueError(f"{path} is not a v1 runner authority document ({'; '.join(detail)})")
        try:
            return RunnerToml(**document)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} has invalid runner authority metadata: {exc}") from exc


_FIELDS = (
    "tenant_id",
    "runner_id",
    "backend_url",
    "credential_id",
    "credential_version",
    "credential_valid_until",
    "machine_key_id",
    "machine_vault_path",
    "enrolled_at",
)


def _serialise(record: RunnerToml) -> str:
    data = asdict(record)
    lines = []
    for field in _FIELDS:
        value = data[field]
        if isinstance(value, int):
            lines.append(f"{field} = {value}")
        else:
            lines.append(f'{field} = "{_escape(value)}"')
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)

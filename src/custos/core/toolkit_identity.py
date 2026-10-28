"""Observe installed toolkit identity and compare it with verified build inputs.

The observer reads a stable identity file and cross-checks installed distribution
versions. Missing or untrusted identity remains a source build. The judge returns
consistency and mismatch details; callers must apply their admission rules before
activation or strategy import. This module does not activate artifacts, install
host gates, or grant execution authority.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.parser import Parser
from enum import StrEnum
from pathlib import Path

from custos.core.log import get_logger

log = get_logger("custos.toolkit-identity")

TOOLKIT_IDENTITY_FILE = Path("/usr/local/share/custos/toolkit-identity.json")
BASE_DISTRIBUTION = "custos-strategy-toolkit"
NAUTILUS_DISTRIBUTION = "custos-strategy-toolkit-nautilus"
ENGINE_DISTRIBUTION = "nautilus-trader"
_IDENTITY_SCHEMA_VERSION = 1
_MANIFEST_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9A-Za-z.+!_-]{1,64}")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_IDENTITY_MAX_BYTES = 64 * 1024


class ToolkitConsistencyState(StrEnum):
    CONSISTENT = "consistent"
    DIFFERS = "differs"
    RUNTIME_SOURCE_BUILD = "runtime_source_build"
    BUILD_UNREADABLE = "build_unreadable"


class Mismatch(StrEnum):
    TOOLKIT_MANIFEST = "toolkit_manifest"
    TOOLKIT_WHEEL = "toolkit_wheel"
    TOOLKIT_VERSION = "toolkit_version"
    ENGINE = "engine"


# Why live refuses, by state; a ``differs`` that is only the engine has its own code.
REFUSAL_REASON = {
    ToolkitConsistencyState.DIFFERS: "runtime_toolkit_differs_from_build",
    ToolkitConsistencyState.RUNTIME_SOURCE_BUILD: "runtime_toolkit_source_build",
    ToolkitConsistencyState.BUILD_UNREADABLE: "build_toolkit_unreadable",
}
ENGINE_REFUSAL_REASON = "runtime_engine_differs_from_build"


@dataclass(frozen=True, slots=True)
class RuntimeToolkitIdentity:
    """The toolkit and engine this process runs.

    ``candidate_version`` and the digests are set only when the image identity is
    trusted; ``engine_version`` comes from the installed engine in every case.
    """

    trusted: bool
    candidate_version: str | None
    manifest_digest: str | None
    nautilus_wheel_sha256: str | None
    engine_version: str | None


@dataclass(frozen=True, slots=True)
class RuntimeToolkitSnapshot:
    """An identity and the digest of the same verified file read.

    An untrusted observation has no digest that local authorization can bind.
    This observation does not prove operating-system identity isolation.
    """

    identity: RuntimeToolkitIdentity
    identity_sha256: str | None


@dataclass(frozen=True, slots=True)
class BuildToolkitIdentity:
    """The toolkit and engine a strategy was built against.

    A strategy release gives the manifest and wheel digests; a development
    snapshot gives the pinned toolkit version. ``unreadable`` says why neither
    could be read, and then nothing else is set.
    """

    channel: str
    manifest_digest: str | None = None
    toolkit_wheel_sha256: str | None = None
    toolkit_version: str | None = None
    engine_version: str | None = None
    unreadable: str | None = None


@dataclass(frozen=True, slots=True)
class ToolkitConsistency:
    state: ToolkitConsistencyState
    # The runtime candidate version, only while the runtime identity is trusted. On
    # live a deployment that started is consistent, so this is also the build rc.
    runtime_candidate_version: str | None
    mismatches: tuple[Mismatch, ...]
    build: BuildToolkitIdentity
    runtime: RuntimeToolkitIdentity

    @property
    def consistent(self) -> bool:
        return self.state is ToolkitConsistencyState.CONSISTENT

    def refusal_reason(self) -> str | None:
        if self.consistent:
            return None
        if self.state is ToolkitConsistencyState.DIFFERS and self.mismatches == (Mismatch.ENGINE,):
            return ENGINE_REFUSAL_REASON
        return REFUSAL_REASON[self.state]

    def summary(self) -> dict[str, object]:
        """Versions, digest prefixes, the state and what differs; nothing else."""

        return {
            "state": self.state.value,
            "mismatches": [mismatch.value for mismatch in self.mismatches],
            "build_channel": self.build.channel,
            "build_toolkit_manifest": _prefix(self.build.manifest_digest),
            "build_toolkit_version": self.build.toolkit_version,
            "build_engine_version": self.build.engine_version,
            "runtime_candidate_version": self.runtime_candidate_version,
            "runtime_toolkit_manifest": _prefix(self.runtime.manifest_digest),
            "runtime_engine_version": self.runtime.engine_version,
        }


def _prefix(digest: str | None) -> str | None:
    if digest is None:
        return None
    return digest.removeprefix("sha256:")[:12]


Report = Callable[[str, dict[str, object]], None]


def _log_error(event: str, fields: dict[str, object]) -> None:
    log.error(event, **fields)


def _installed_engine_version(
    installed_version: Callable[[str], str], report: Report
) -> str | None:
    """The installed engine version, or None when it cannot be read.

    None differs from every build engine, so live refuses; a failure other than
    an absent distribution is reported as well.
    """

    try:
        version = installed_version(ENGINE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception as error:  # noqa: BLE001 - unreadable metadata is no engine
        report(
            "runtime_engine_version_unreadable",
            {"error_type": type(error).__name__, "detail": str(error)[:200]},
        )
        return None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        report("runtime_engine_version_unreadable", {"error_type": "malformed_version"})
        return None
    return version


def _read_identity_file(path: Path) -> bytes:
    """Read the identity only if it is a regular file, without waiting on anything else.

    The path is checked with lstat before it is opened, and opened non-blocking, so
    a FIFO or a device there is refused rather than opened and waited on. The open
    descriptor must still be that regular file, unchanged while it is read.
    """

    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("toolkit identity is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError("toolkit identity changed while it was opened")
        payload = os.read(descriptor, _IDENTITY_MAX_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > _IDENTITY_MAX_BYTES or len(payload) != after.st_size:
        raise ValueError("toolkit identity size differs from what was read")
    if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("toolkit identity changed while it was read")
    return payload


def observe_runtime_toolkit_identity(
    path: Path = TOOLKIT_IDENTITY_FILE,
    installed_version: Callable[[str], str] = importlib.metadata.version,
    report: Report = _log_error,
) -> RuntimeToolkitIdentity:
    """Read the image identity and cross-check it against the installed toolkit.

    ``report`` receives what made an identity untrusted; by default it is a local
    ERROR log. A command whose standard output is its result passes its own.
    """

    return observe_runtime_toolkit_snapshot(path, installed_version, report).identity


def observe_runtime_toolkit_snapshot(
    path: Path = TOOLKIT_IDENTITY_FILE,
    installed_version: Callable[[str], str] = importlib.metadata.version,
    report: Report = _log_error,
) -> RuntimeToolkitSnapshot:
    """Bind identity validation and its digest to one stable byte snapshot."""

    engine_version = _installed_engine_version(installed_version, report)
    untrusted = RuntimeToolkitSnapshot(
        identity=RuntimeToolkitIdentity(
            trusted=False,
            candidate_version=None,
            manifest_digest=None,
            nautilus_wheel_sha256=None,
            engine_version=engine_version,
        ),
        identity_sha256=None,
    )
    try:
        payload = _read_identity_file(path)
    except FileNotFoundError:
        # No identity: a source build. A path that cannot be examined at all, a
        # directory without permission for one, is reported below like any other
        # unreadable identity rather than raised.
        return untrusted
    except Exception as error:  # noqa: BLE001 - an unreadable identity is a source build
        report(
            "runtime_toolkit_identity_untrusted",
            {"path": str(path), "error_type": type(error).__name__, "detail": str(error)[:200]},
        )
        return untrusted
    try:
        document = json.loads(payload)
        candidate = document["candidate_version"]
        manifest = document["manifest_digest"]
        wheel = document["nautilus_wheel_sha256"]
        if document.get("schema_version") != _IDENTITY_SCHEMA_VERSION:
            raise ValueError("toolkit identity schema version differs")
        if not (
            isinstance(candidate, str)
            and _VERSION.fullmatch(candidate)
            and isinstance(manifest, str)
            and _MANIFEST_DIGEST.fullmatch(manifest)
            and isinstance(wheel, str)
            and _SHA256.fullmatch(wheel)
        ):
            raise ValueError("toolkit identity fields are malformed")
        installed = {
            name: installed_version(name) for name in (BASE_DISTRIBUTION, NAUTILUS_DISTRIBUTION)
        }
        if any(version != candidate for version in installed.values()):
            raise ValueError(
                "installed toolkit versions differ from the image identity: "
                + ", ".join(f"{name} {version}" for name, version in sorted(installed.items()))
                + f" against {candidate}"
            )
    except Exception as error:  # noqa: BLE001 - an untrusted identity is a source build
        report(
            "runtime_toolkit_identity_untrusted",
            {
                "path": str(path),
                "error_type": type(error).__name__,
                "detail": str(error)[:200],
            },
        )
        return untrusted
    return RuntimeToolkitSnapshot(
        identity=RuntimeToolkitIdentity(
            trusted=True,
            candidate_version=candidate,
            manifest_digest=manifest,
            nautilus_wheel_sha256=wheel,
            engine_version=engine_version,
        ),
        identity_sha256=hashlib.sha256(payload).hexdigest(),
    )


def build_identity_from_release_bom(bom: Mapping[str, object]) -> BuildToolkitIdentity:
    """The toolkit and engine a strategy release's BOM records."""

    coordinate = bom.get("toolkit_coordinate")
    wheel = bom.get("toolkit_wheel_sha256")
    engine = bom.get("engine_version")
    manifest = coordinate.rpartition("@")[2] if isinstance(coordinate, str) else None
    if manifest is None or not _MANIFEST_DIGEST.fullmatch(manifest):
        return BuildToolkitIdentity("release", unreadable="toolkit_coordinate has no digest")
    if not isinstance(wheel, str) or not _SHA256.fullmatch(wheel):
        return BuildToolkitIdentity("release", unreadable="toolkit_wheel_sha256 is missing")
    if not isinstance(engine, str) or not _VERSION.fullmatch(engine):
        return BuildToolkitIdentity("release", unreadable="engine_version is missing")
    return BuildToolkitIdentity(
        "release",
        manifest_digest=manifest,
        toolkit_wheel_sha256=wheel,
        engine_version=engine,
    )


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_PINNED = (BASE_DISTRIBUTION, NAUTILUS_DISTRIBUTION, ENGINE_DISTRIBUTION)


class _UnreadablePins(ValueError):
    """The wheel's pins on the toolkit or the engine cannot be read as one version."""


def _effective_pins(requirements: list[str]) -> dict[str, str]:
    """The one exact version each toolkit and engine requirement takes in this process.

    A requirement whose environment marker does not hold here does not apply. An
    applying requirement on one of these distributions must be a single ``==``
    without a URL, and every applying requirement on it must name the same version.
    """

    # The adapter toolkit depends on packaging, so it is present wherever a
    # development snapshot runs; the upgrade check, which reads no wheel metadata,
    # does not need it.
    from packaging.requirements import InvalidRequirement, Requirement

    versions: dict[str, set[str]] = {}
    for line in requirements:
        named = _REQUIREMENT_NAME.match(line)
        if named is None or _normalized(named.group(1)) not in _PINNED:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise _UnreadablePins(f"unparsable requirement: {line[:80]}") from error
        name = _normalized(requirement.name)
        try:
            applies = requirement.marker is None or requirement.marker.evaluate({"extra": ""})
        except Exception as error:  # noqa: BLE001 - a marker that cannot be evaluated here
            raise _UnreadablePins(f"unevaluable marker on {name}: {error}"[:200]) from error
        if not applies:
            continue
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1 or specifiers[0].operator != "==":
            raise _UnreadablePins(f"{name} is not pinned to one exact version")
        versions.setdefault(name, set()).add(specifiers[0].version)
    ambiguous = sorted(name for name, found in versions.items() if len(found) > 1)
    if ambiguous:
        raise _UnreadablePins(f"conflicting pins for {', '.join(ambiguous)}")
    return {name: next(iter(found)) for name, found in versions.items()}


def build_identity_from_wheel_metadata(metadata: str) -> BuildToolkitIdentity:
    """The toolkit and engine a development snapshot's wheel pins in this process."""

    try:
        pins = _effective_pins(Parser().parsestr(metadata).get_all("Requires-Dist") or [])
    except (_UnreadablePins, ImportError) as error:
        return BuildToolkitIdentity("development", unreadable=str(error)[:200])
    toolkit_pins = {
        pins[name] for name in (BASE_DISTRIBUTION, NAUTILUS_DISTRIBUTION) if name in pins
    }
    engine = pins.get(ENGINE_DISTRIBUTION)
    if not toolkit_pins:
        return BuildToolkitIdentity("development", unreadable="no exact toolkit pin")
    if len(toolkit_pins) > 1:
        return BuildToolkitIdentity(
            "development", unreadable="base and adapter toolkit pins name different versions"
        )
    if engine is None:
        return BuildToolkitIdentity("development", unreadable="no exact engine pin")
    return BuildToolkitIdentity(
        "development", toolkit_version=toolkit_pins.pop(), engine_version=engine
    )


def build_identity_from_activation_root(root: Path) -> BuildToolkitIdentity:
    """Read the METADATA of the single wheel distribution activated under ``root``."""

    dist_infos = sorted(root.glob("*.dist-info"))
    if len(dist_infos) != 1:
        return BuildToolkitIdentity(
            "development", unreadable=f"expected one dist-info, found {len(dist_infos)}"
        )
    try:
        metadata = (dist_infos[0] / "METADATA").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return BuildToolkitIdentity(
            "development", unreadable=f"wheel metadata is unreadable: {type(error).__name__}"
        )
    return build_identity_from_wheel_metadata(metadata)


def build_identity_of_prepared(prepared: object) -> BuildToolkitIdentity:
    """The build identity of a prepared artifact, before anything of it is imported.

    A strategy release carries its verified BOM; a development snapshot has been
    extracted to its quarantine directory, where its wheel metadata is read.
    """

    release_authority = getattr(prepared, "release_authority", None)
    if release_authority is not None:
        bom = getattr(release_authority, "release_bom", None)
        if isinstance(bom, Mapping):
            return build_identity_from_release_bom(bom)
        return BuildToolkitIdentity("release", unreadable="release BOM is missing")
    if getattr(prepared, "verified_artifact", None) is not None:
        root = getattr(prepared, "quarantine_root", None)
        if isinstance(root, Path):
            return build_identity_from_activation_root(root)
        return BuildToolkitIdentity("development", unreadable="quarantine root is missing")
    return BuildToolkitIdentity("unknown", unreadable="artifact carries no build identity")


def build_identity_of(artifact: object) -> BuildToolkitIdentity:
    """The build identity of an activated artifact, whichever channel it came from."""

    prepared = getattr(artifact, "prepared", None)
    release_authority = getattr(prepared, "release_authority", None)
    if release_authority is not None:
        bom = getattr(release_authority, "release_bom", None)
        if isinstance(bom, Mapping):
            return build_identity_from_release_bom(bom)
        return BuildToolkitIdentity("release", unreadable="release BOM is missing")
    if getattr(prepared, "verified_artifact", None) is not None:
        root = getattr(artifact, "activation_root", None)
        if isinstance(root, Path):
            return build_identity_from_activation_root(root)
        return BuildToolkitIdentity("development", unreadable="activation root is missing")
    return BuildToolkitIdentity("unknown", unreadable="artifact carries no build identity")


def judge(build: BuildToolkitIdentity, runtime: RuntimeToolkitIdentity) -> ToolkitConsistency:
    """Compare what the strategy was built against with what this process runs."""

    candidate = runtime.candidate_version if runtime.trusted else None
    mismatches: list[Mismatch] = []
    if build.unreadable is None:
        if runtime.trusted:
            if build.channel == "release":
                if build.manifest_digest != runtime.manifest_digest:
                    mismatches.append(Mismatch.TOOLKIT_MANIFEST)
                if build.toolkit_wheel_sha256 != runtime.nautilus_wheel_sha256:
                    mismatches.append(Mismatch.TOOLKIT_WHEEL)
            elif build.toolkit_version != runtime.candidate_version:
                mismatches.append(Mismatch.TOOLKIT_VERSION)
        # The engine is observable without the image identity, so it is compared
        # for a source build too.
        if build.engine_version != runtime.engine_version:
            mismatches.append(Mismatch.ENGINE)
    if build.unreadable is not None:
        state = ToolkitConsistencyState.BUILD_UNREADABLE
    elif not runtime.trusted:
        state = ToolkitConsistencyState.RUNTIME_SOURCE_BUILD
    elif mismatches:
        state = ToolkitConsistencyState.DIFFERS
    else:
        state = ToolkitConsistencyState.CONSISTENT
    return ToolkitConsistency(
        state=state,
        runtime_candidate_version=candidate,
        mismatches=tuple(mismatches),
        build=build,
        runtime=runtime,
    )


__all__ = [
    "BuildToolkitIdentity",
    "ENGINE_REFUSAL_REASON",
    "Mismatch",
    "REFUSAL_REASON",
    "RuntimeToolkitIdentity",
    "TOOLKIT_IDENTITY_FILE",
    "ToolkitConsistency",
    "ToolkitConsistencyState",
    "build_identity_from_activation_root",
    "build_identity_from_release_bom",
    "build_identity_from_wheel_metadata",
    "build_identity_of",
    "build_identity_of_prepared",
    "judge",
    "observe_runtime_toolkit_identity",
]

"""Bind a local candidate's installed native bytes to its build-selected descriptor."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custos.engines.nautilus.native_opening_inventory import NativeRuntimeArtifactIdentity

PROFILE_ID = "native-capture-darwin7-v1"
DESCRIPTOR_RESOURCE = "_native_build_descriptor_v1.json"


class NativeRuntimeBundleError(ValueError):
    """A candidate bundle does not match the build-selected native identity."""


def validate_native_engine(value: object) -> dict:
    """Validate explicit build features, not the transitive Cargo feature closure."""
    if not isinstance(value, dict) or set(value) != {
        "distribution",
        "version",
        "artifact_sha256",
        "abi",
        "platform",
        "native_build",
    }:
        raise NativeRuntimeBundleError("native engine descriptor must be closed and complete")
    build = value["native_build"]
    if (
        value["distribution"] != "nautilus-trader"
        or value["version"] != "2.0.0rc5+papertrade.7"
        or value["abi"] != "cp312"
        or value["platform"] != "macosx_11_0_arm64"
        or not isinstance(value["artifact_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["artifact_sha256"]) is None
        or not isinstance(build, dict)
        or set(build) != {"source_revision", "crate_version", "profile", "features"}
    ):
        raise NativeRuntimeBundleError("native engine build identity is invalid")
    if (
        not isinstance(build["source_revision"], str)
        or re.fullmatch(r"[0-9a-f]{40}", build["source_revision"]) is None
        or build["source_revision"] == "0" * 40
        or not isinstance(build["crate_version"], str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", build["crate_version"]
        )
        is None
        or build["profile"] != "dev"
        or not isinstance(build["features"], list)
        or not build["features"]
        or any(
            not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", v) is None
            for v in build["features"]
        )
        or len(build["features"]) != len(set(build["features"]))
        or "extension-module" not in build["features"]
    ):
        raise NativeRuntimeBundleError(
            "native source revision, crate or explicit features are invalid"
        )
    return deepcopy(value)


def native_build_descriptor(recipe: dict, source_commit: str) -> dict:
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None
        or set(source_commit) == {"0"}
    ):
        raise NativeRuntimeBundleError("candidate source commit is invalid")
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "source_commit": source_commit,
        "engine": validate_native_engine(recipe["engine"]),
    }


def descriptor_bytes(recipe: dict, source_commit: str) -> bytes:
    return (
        json.dumps(
            native_build_descriptor(recipe, source_commit), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise NativeRuntimeBundleError("candidate descriptor contains duplicate fields")
        result[key] = value
    return result


def _protected_path(path: Path, *, directory=False) -> Path:
    path = path.absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise NativeRuntimeBundleError("candidate bundle contains a symlink or path escape")
    try:
        info = path.lstat()
    except OSError as error:
        raise NativeRuntimeBundleError("candidate bundle input is missing") from error
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise NativeRuntimeBundleError("candidate bundle input is not a protected owner file")
    return path


def _read(path: Path) -> bytes:
    path = _protected_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
        ):
            raise NativeRuntimeBundleError("opened candidate input is not a protected regular file")
        data = stream.read(8 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid")
    if len(data) > 8 * 1024 * 1024 or any(
        getattr(before, f) != getattr(after, f) or getattr(after, f) != getattr(current, f)
        for f in fields
    ):
        raise NativeRuntimeBundleError("candidate bundle input changed while reading")
    return data


def _document(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise NativeRuntimeBundleError("candidate bundle document is invalid") from error
    if not isinstance(value, dict):
        raise NativeRuntimeBundleError("candidate bundle document must be an object")
    return value


def _installed_descriptor() -> dict | None:
    from custos_toolkit.contracts.engine_profile import ENGINE_PROFILE_ID, ENGINE_VERSION

    resource = Path(str(files("custos.core").joinpath(DESCRIPTOR_RESOURCE)))
    try:
        info = resource.lstat()
    except FileNotFoundError as error:
        if (ENGINE_PROFILE_ID, ENGINE_VERSION) == (
            "released-sodex3-v1",
            "2.0.0rc5+sodex.3",
        ):
            return None
        raise NativeRuntimeBundleError("candidate native build descriptor is missing") from error
    if not stat.S_ISREG(info.st_mode):
        raise NativeRuntimeBundleError("installed native build descriptor is not a regular file")
    value = _document(_read(resource))
    if (
        set(value) != {"schema_version", "profile_id", "source_commit", "engine"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["profile_id"] != PROFILE_ID
    ):
        raise NativeRuntimeBundleError("installed native build descriptor is invalid")
    expected = native_build_descriptor(value, value["source_commit"])
    if value != expected:
        raise NativeRuntimeBundleError("installed native build descriptor differs")
    if ENGINE_PROFILE_ID != value["profile_id"] or ENGINE_VERSION != value["engine"]["version"]:
        raise NativeRuntimeBundleError(
            "installed static engine profile differs from the native descriptor"
        )
    return value


def _bundle_file(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise NativeRuntimeBundleError("candidate bundle path is invalid")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise NativeRuntimeBundleError("candidate bundle path escapes its root")
    result = root.joinpath(*path.parts)
    for parent in reversed(result.parents):
        if parent != root and root in parent.parents:
            _protected_path(parent, directory=True)
    return _protected_path(result)


def _validate_profile_metadata(profile: dict) -> None:
    distributions = profile["distributions"]
    names = {
        "custos-runner",
        "custos-strategy-toolkit",
        "custos-strategy-toolkit-nautilus",
        "custos-strategy-publisher",
    }
    fields = {
        "name",
        "version",
        "wheel_path",
        "sha256",
        "size_bytes",
        "metadata_sha256",
        "requires_python",
        "requires_dist",
    }
    valid = (
        type(profile["source_date_epoch"]) is int
        and profile["source_date_epoch"] >= 315532800
        and isinstance(profile["source_version"], str)
        and bool(profile["source_version"])
        and isinstance(distributions, dict)
        and set(distributions) == names
        and isinstance(profile["acceptance"], dict)
        and profile["acceptance"].get("local_candidate") is True
        and profile["acceptance"].get("rc_published") is False
        and profile["acceptance"].get("control_ready") is False
    )
    if not valid:
        raise NativeRuntimeBundleError("candidate source profile metadata differs")
    for name, row in distributions.items():
        if (
            not isinstance(row, dict)
            or set(row) != fields
            or row["name"] != name
            or any(
                not isinstance(row[f], str) or not row[f]
                for f in ("version", "requires_python", "wheel_path")
            )
            or any(
                not isinstance(row[f], str) or re.fullmatch(r"[0-9a-f]{64}", row[f]) is None
                for f in ("sha256", "metadata_sha256")
            )
            or type(row["size_bytes"]) is not int
            or row["size_bytes"] <= 0
            or not isinstance(row["requires_dist"], list)
            or any(not isinstance(v, str) or not v for v in row["requires_dist"])
            or PurePosixPath(row["wheel_path"]).is_absolute()
            or ".." in PurePosixPath(row["wheel_path"]).parts
            or PurePosixPath(row["wheel_path"]).as_posix() != row["wheel_path"]
            or "\\" in row["wheel_path"]
            or "\x00" in row["wheel_path"]
        ):
            raise NativeRuntimeBundleError("candidate distribution record differs")
    definitions = profile["engine_definitions"]
    paths = {
        "source/packages/custos-strategy-toolkit/src/custos_toolkit/contracts/engine_profile.py",
        "source/packages/custos-strategy-publisher/src/custos_strategy_publisher/engine_profile.py",
    }
    if (
        not isinstance(definitions, dict)
        or set(definitions) != paths
        or any(
            not isinstance(v, dict)
            or set(v) != {"sha256"}
            or not isinstance(v["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) is None
            for v in definitions.values()
        )
    ):
        raise NativeRuntimeBundleError("candidate static engine definitions differ")


async def measure_candidate_native_bundle(
    bundle_root: Path | None,
) -> NativeRuntimeArtifactIdentity | None:
    """Measure a build-selected candidate; model qualification remains independent."""
    from custos.engines.nautilus.native_opening_inventory import NativeRuntimeArtifactIdentity
    from custos.engines.nautilus.native_runtime_artifact import measure_native_runtime_archive

    descriptor = _installed_descriptor()
    if bundle_root is None:
        if descriptor is not None:
            raise NativeRuntimeBundleError("candidate native runtime requires its protected bundle")
        return None
    if descriptor is None:
        raise NativeRuntimeBundleError("released runtime has no candidate native build descriptor")
    root = _protected_path(Path(bundle_root), directory=True)
    profile_path = _bundle_file(root, "candidate-source-profile.json")
    profile_bytes = _read(profile_path)
    profile = _document(profile_bytes)
    required = {
        "schema_version",
        "profile_id",
        "source_repository",
        "source_commit",
        "source_date_epoch",
        "source_root",
        "source_inventory_path",
        "source_inventory_sha256",
        "source_version",
        "engine",
        "distributions",
        "engine_definitions",
        "acceptance",
    }
    if (
        set(profile) != required
        or profile["schema_version"] != "alephain.custos.native-candidate-source-profile.v1"
        or profile["profile_id"] != descriptor["profile_id"]
        or profile["source_repository"] != "https://github.com/the-alephain-guild/custos"
        or profile["source_commit"] != descriptor["source_commit"]
        or profile["source_root"] != "source"
        or profile["source_inventory_path"] != "source-inventory.json"
        or profile["acceptance"]
        != {"local_candidate": True, "rc_published": False, "control_ready": False}
        or not isinstance(profile["engine"], dict)
        or set(profile["engine"]) != set(descriptor["engine"]) | {"wheel_path"}
        or {k: v for k, v in profile["engine"].items() if k != "wheel_path"} != descriptor["engine"]
    ):
        raise NativeRuntimeBundleError(
            "candidate bundle profile differs from the installed descriptor"
        )
    _validate_profile_metadata(profile)
    inventory = _read(_bundle_file(root, profile["source_inventory_path"]))
    if hashlib.sha256(inventory).hexdigest() != profile["source_inventory_sha256"]:
        raise NativeRuntimeBundleError("candidate source inventory digest differs")
    source_resource = "src/custos/core/" + DESCRIPTOR_RESOURCE
    source_bytes = _read(_bundle_file(root, "source/" + source_resource))
    resource_record = _document(inventory).get(source_resource)
    if _document(source_bytes) != descriptor or resource_record != {
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
    }:
        raise NativeRuntimeBundleError(
            "candidate source descriptor differs from the installed build"
        )
    wheel = _bundle_file(root, profile["engine"]["wheel_path"])
    measured = await measure_native_runtime_archive(
        wheel,
        archive_sha256=descriptor["engine"]["artifact_sha256"],
        distribution_version=descriptor["engine"]["version"],
    )
    tag = f"{descriptor['engine']['abi']}-{descriptor['engine']['abi']}-{descriptor['engine']['platform']}"
    if measured.wheel_tags != (tag,) or _read(profile_path) != profile_bytes:
        raise NativeRuntimeBundleError("candidate ABI or bundle profile changed during measurement")
    return NativeRuntimeArtifactIdentity(
        measured.archive_sha256, descriptor["engine"]["native_build"]["crate_version"]
    )

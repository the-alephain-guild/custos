"""The runtime a Runner declares in its capability: image, source revision and engine.

A Runner observes this identity in its own process. A container cannot see its
own image digest, so trusted deployment configuration supplies it through
``CUSTOS_RUNTIME_IMAGE_DIGEST``, the same trust position as live admission. The
Runner then checks what it can observe itself: the image's source revision file
must hold a commit, and the engine version comes from the installed engine
distribution. Without that variable the Runner is a development build: it
declares no digest and no revision, and the control plane never treats its facts
as qualifying a runtime.

The variable describes what this Runner is, in every mode. It is not the
``--runtime-image-digest`` live admission input and never defaults it.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from custos.core.runtime_admission import RuntimeAdmission

RUNTIME_IMAGE_DIGEST_ENV = "CUSTOS_RUNTIME_IMAGE_DIGEST"
OCI_IMAGE = "oci_image"
DEVELOPMENT = "development"
ENGINE = "nautilus"
RUNTIME_FIELDS = frozenset(
    {"distribution", "image_digest", "source_revision", "engine", "engine_version"}
)

_ENGINE_DISTRIBUTION = "nautilus-trader"
# The multi-platform index digest, which a release shares across architectures.
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}")
# PEP 440 characters, ASCII only, so its length is the same in characters and bytes.
_ENGINE_VERSION = re.compile(r"[0-9A-Za-z.+!_-]{1,64}")
_REPUBLISH = "publish the capability again with this runtime, then restart the runner"


class RuntimeIdentityError(ValueError):
    """The runtime identity is unobservable, malformed or not the running one."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_runtime_identity(value: object) -> dict[str, Any]:
    """Apply the rules the control plane applies to a declared runtime."""

    if not isinstance(value, Mapping) or set(value) != RUNTIME_FIELDS:
        raise RuntimeIdentityError(
            "runtime_identity_invalid",
            "runtime must have exactly distribution, image_digest, source_revision, "
            "engine and engine_version",
        )
    distribution = value["distribution"]
    if distribution == OCI_IMAGE:
        if not _matches(_IMAGE_DIGEST, value["image_digest"]):
            raise RuntimeIdentityError(
                "runtime_identity_invalid",
                "an oci_image runtime declares sha256: and 64 lowercase hexadecimal digits",
            )
        if not _matches(_SOURCE_REVISION, value["source_revision"]):
            raise RuntimeIdentityError(
                "runtime_identity_invalid",
                "an oci_image runtime declares a 40-digit lowercase commit revision",
            )
    elif distribution == DEVELOPMENT:
        if value["image_digest"] is not None or value["source_revision"] is not None:
            raise RuntimeIdentityError(
                "runtime_identity_invalid",
                "a development runtime declares no image digest and no source revision",
            )
    else:
        raise RuntimeIdentityError(
            "runtime_identity_invalid", "runtime distribution is oci_image or development"
        )
    if value["engine"] != ENGINE or not isinstance(value["engine"], str):
        raise RuntimeIdentityError("runtime_identity_invalid", "runtime engine is nautilus")
    if not _matches(_ENGINE_VERSION, value["engine_version"]):
        raise RuntimeIdentityError(
            "runtime_identity_invalid",
            "runtime engine_version is 1 to 64 characters of 0-9 A-Z a-z . + ! _ -",
        )
    return dict(value)


def installed_engine_version() -> str:
    try:
        return importlib.metadata.version(_ENGINE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeIdentityError(
            "runtime_engine_unavailable",
            f"{_ENGINE_DISTRIBUTION} is not installed, so the runner cannot declare its engine",
        ) from error


def _running_source_revision() -> str:
    from custos.artifacts.sigstore_verifier import _read_stable_regular_file
    from custos.core import runtime_admission

    try:
        revision = (
            _read_stable_regular_file(
                runtime_admission.RUNTIME_SOURCE_REVISION_FILE, "running image source revision"
            )
            .decode("ascii")
            .strip()
        )
    except Exception as error:
        raise RuntimeIdentityError(
            "runtime_source_revision_unavailable",
            f"{RUNTIME_IMAGE_DIGEST_ENV} is set but the image source revision file is unreadable",
        ) from error
    if _SOURCE_REVISION.fullmatch(revision) is None:
        raise RuntimeIdentityError(
            "runtime_source_revision_invalid",
            f"{RUNTIME_IMAGE_DIGEST_ENV} is set but the image was not built from a commit",
        )
    return revision


def observe_runtime_identity(environ: Mapping[str, str]) -> dict[str, Any]:
    """Return the runtime this process runs, or refuse; never guess a weaker one."""

    engine_version = installed_engine_version()
    image_digest = environ.get(RUNTIME_IMAGE_DIGEST_ENV)
    if image_digest is None:
        identity: dict[str, Any] = {
            "distribution": DEVELOPMENT,
            "image_digest": None,
            "source_revision": None,
        }
    else:
        if not _matches(_IMAGE_DIGEST, image_digest):
            raise RuntimeIdentityError(
                "runtime_image_digest_invalid",
                f"{RUNTIME_IMAGE_DIGEST_ENV} is sha256: and 64 lowercase hexadecimal digits",
            )
        identity = {
            "distribution": OCI_IMAGE,
            "image_digest": image_digest,
            "source_revision": _running_source_revision(),
        }
    identity["engine"] = ENGINE
    identity["engine_version"] = engine_version
    return validate_runtime_identity(identity)


def declare_runtime(manifest: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    """Write the observed runtime into a manifest, or keep one that is identical."""

    if "runtime" not in manifest:
        return {**manifest, "runtime": dict(observed)}
    declared = manifest["runtime"]
    if not isinstance(declared, Mapping) or dict(declared) != dict(observed):
        raise RuntimeIdentityError(
            "runtime_identity_mismatch",
            "the manifest declares a runtime other than the one this process runs; "
            "remove runtime from the manifest to let the runner declare its own",
        )
    return dict(manifest)


def require_declared_runtime(
    manifest: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    admission: RuntimeAdmission | None,
) -> None:
    """Refuse to start unless the capability declares the runtime running now."""

    if "runtime" not in manifest:
        raise RuntimeIdentityError(
            "runtime_identity_missing", f"the capability declares no runtime; {_REPUBLISH}"
        )
    declared = validate_runtime_identity(manifest["runtime"])
    differing = sorted(field for field in RUNTIME_FIELDS if declared[field] != observed[field])
    if differing:
        raise RuntimeIdentityError(
            "runtime_identity_mismatch",
            f"the capability declares another runtime ({', '.join(differing)} differ); "
            f"after changing the image or the engine, {_REPUBLISH}",
        )
    if admission is not None and (
        declared["distribution"] != OCI_IMAGE
        or declared["image_digest"] != admission.image_digest
        or declared["source_revision"] != admission.source_revision
    ):
        raise RuntimeIdentityError(
            "runtime_identity_not_admitted",
            "live requires the capability to declare the admitted image digest and revision",
        )

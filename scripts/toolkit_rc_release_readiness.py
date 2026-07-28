#!/usr/bin/env python3
"""Prepare deterministic, unsigned inputs for the protected toolkit RC release runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from custos_toolkit.contracts import (
    ImmutableToolkitArtifactBindingV1,
    LockedToolkitDependencyV1,
    ToolkitRcCycloneDxSbomV1,
    ToolkitRcMemberRole,
    ToolkitRcMemberV1,
    ToolkitRcPendingReceiptV1,
    ToolkitRcReceiptManifestV1,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

DISTRIBUTION_ROLES: Final = {
    "custos-strategy-toolkit": ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL,
    "custos-strategy-toolkit-nautilus": ToolkitRcMemberRole.NAUTILUS_WHEEL,
}
SOURCE_REPOSITORY: Final = "https://github.com/the-alephain-guild/custos"
WORKFLOW_IDENTITY: Final = (
    "https://github.com/the-alephain-guild/custos/.github/workflows/"
    "release-toolkit-rc.yml@refs/heads/main"
)
OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
FINAL_BLOCKER: Final = (
    "execute the protected production release runner with credentials and register "
    "its verified remote receipt"
)
RC_VERSION_RE: Final = re.compile(r"^0\.1\.0rc[1-9][0-9]*$")


class ReleaseReadinessError(RuntimeError):
    """Deterministic protected-release readiness evidence could not be produced safely."""


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    name: str
    version: str
    requirement: str
    source: Mapping[str, str]
    artifact_sha256: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "requirement": self.requirement,
            "source": dict(self.source),
            "artifact_sha256": list(self.artifact_sha256),
        }


@dataclass(frozen=True, slots=True)
class ReleaseReadinessArtifacts:
    candidate_version: str
    source_commit: str
    source_date_epoch: int
    dependency_lock_path: Path
    sbom_paths: dict[str, Path]
    provenance_path: Path
    pending_receipt_path: Path


@dataclass(frozen=True, slots=True)
class PublicationAssemblyArtifacts:
    candidate_version: str
    manifest_path: Path
    object_sources_path: Path


@dataclass(frozen=True, slots=True)
class _WheelInput:
    distribution_name: str
    version: str
    path: Path
    sha256: str
    size_bytes: int
    requires_python: str
    requires_dist: tuple[str, ...]
    top_level_modules: tuple[str, ...]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _read_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        content = path.read_bytes()
        document = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseReadinessError(f"{label} is invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise ReleaseReadinessError(f"{label} must be a JSON object")
    return content, document


def _locked_package_map(lock_path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        lock_document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseReadinessError(f"uv.lock is invalid: {exc}") from exc
    packages = lock_document.get("package")
    if not isinstance(packages, list):
        raise ReleaseReadinessError("uv.lock has no package records")
    result: dict[str, Mapping[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ReleaseReadinessError("uv.lock contains an invalid package record")
        name = canonicalize_name(package["name"])
        if name in result:
            raise ReleaseReadinessError(f"uv.lock contains ambiguous versions for {name}")
        result[name] = package
    return result


def _artifact_hashes(package: Mapping[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()
    sdist = package.get("sdist")
    if isinstance(sdist, dict) and isinstance(sdist.get("hash"), str):
        values.add(sdist["hash"].removeprefix("sha256:"))
    wheels = package.get("wheels")
    if isinstance(wheels, list):
        for wheel in wheels:
            if isinstance(wheel, dict) and isinstance(wheel.get("hash"), str):
                values.add(wheel["hash"].removeprefix("sha256:"))
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
        raise ReleaseReadinessError("uv.lock contains a non-SHA-256 artifact hash")
    return tuple(sorted(values))


def resolve_locked_dependencies(
    *,
    raw_requirements: Sequence[str],
    candidate_version: str,
    lock_path: Path,
) -> tuple[ResolvedDependency, ...]:
    """Resolve direct wheel requirements to exact versions from the committed uv.lock."""

    packages = _locked_package_map(lock_path)
    resolved: dict[str, ResolvedDependency] = {}
    for raw in raw_requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise ReleaseReadinessError(f"wheel requirement is invalid: {raw}") from exc
        if requirement.url is not None:
            raise ReleaseReadinessError("editable, URL, and path dependencies are forbidden")
        name = canonicalize_name(requirement.name)
        if name == "custos-strategy-toolkit":
            version_text = candidate_version
            source: Mapping[str, str] = {"toolkit_rc_candidate": candidate_version}
            hashes: tuple[str, ...] = ()
        else:
            package = packages.get(name)
            if package is None or not isinstance(package.get("version"), str):
                raise ReleaseReadinessError(f"dependency {name} is not locked in uv.lock")
            version_text = package["version"]
            source_value = package.get("source")
            if not isinstance(source_value, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in source_value.items()
            ):
                raise ReleaseReadinessError(f"dependency {name} has no immutable lock source")
            source = source_value
            hashes = _artifact_hashes(package)
            if "registry" in source and not hashes:
                raise ReleaseReadinessError(
                    f"registry dependency {name} has no locked artifact hashes"
                )
        try:
            version = Version(version_text)
            satisfies = not requirement.specifier or requirement.specifier.contains(
                version, prereleases=True
            )
        except (InvalidVersion, InvalidSpecifier) as exc:
            raise ReleaseReadinessError(f"dependency {name} has invalid version policy") from exc
        if not satisfies:
            raise ReleaseReadinessError(f"locked {name}=={version_text} does not satisfy {raw}")
        dependency = ResolvedDependency(
            name=name,
            version=version_text,
            requirement=f"{name}=={version_text}",
            source=source,
            artifact_sha256=hashes,
        )
        existing = resolved.get(name)
        if existing is not None and existing != dependency:
            raise ReleaseReadinessError(f"dependency {name} resolves inconsistently")
        resolved[name] = dependency
    return tuple(resolved[name] for name in sorted(resolved))


def _load_wheels(
    build_root: Path,
) -> tuple[bytes, dict[str, Any], dict[str, _WheelInput]]:
    manifest_path = build_root / "toolkit-rc-build-manifest-input.json"
    manifest_content, document = _read_json(manifest_path, "build manifest")
    if document.get("status") != "BUILD_CANDIDATE_ONLY":
        raise ReleaseReadinessError("build evidence build status differs")
    candidate_version = document.get("candidate_version")
    if not isinstance(candidate_version, str) or not RC_VERSION_RE.fullmatch(candidate_version):
        raise ReleaseReadinessError("build candidate version is not immutable 0.1.0rcN")
    required_flags = {
        "reproducible": True,
        "registry_accessed": False,
        "ready_receipt_created": False,
        "strategy_release_bom_created": False,
    }
    if any(document.get(name) != value for name, value in required_flags.items()):
        raise ReleaseReadinessError("build candidate-only safety flags differ")
    builds = document.get("builds")
    if not isinstance(builds, dict):
        raise ReleaseReadinessError("build evidence build records are absent")
    first = builds.get("build-1")
    second = builds.get("build-2")
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise ReleaseReadinessError("build evidence requires two isolated build records")
    if set(first) != set(DISTRIBUTION_ROLES) or first != second:
        raise ReleaseReadinessError("build evidence build records are not identical")

    wheels: dict[str, _WheelInput] = {}
    for distribution in sorted(DISTRIBUTION_ROLES):
        record = first[distribution]
        if not isinstance(record, dict):
            raise ReleaseReadinessError(f"build evidence {distribution} record is invalid")
        filename = record.get("filename")
        digest = record.get("sha256")
        size_bytes = record.get("size_bytes")
        if (
            not isinstance(filename, str)
            or not isinstance(digest, str)
            or not isinstance(size_bytes, int)
        ):
            raise ReleaseReadinessError(f"build evidence {distribution} wheel binding is invalid")
        first_path = build_root / "build-1" / "dist" / distribution / filename
        second_path = build_root / "build-2" / "dist" / distribution / filename
        try:
            first_bytes = first_path.read_bytes()
            second_bytes = second_path.read_bytes()
        except OSError as exc:
            raise ReleaseReadinessError(
                f"build evidence {distribution} wheel is missing: {exc}"
            ) from exc
        if first_bytes != second_bytes:
            raise ReleaseReadinessError(f"build evidence {distribution} wheel bytes differ")
        if _sha256(first_bytes) != digest or len(first_bytes) != size_bytes:
            raise ReleaseReadinessError(
                f"build evidence {distribution} wheel digest or size differs"
            )
        raw_dependencies = record.get("requires_dist")
        top_levels = record.get("top_level_modules")
        if not isinstance(raw_dependencies, list) or not all(
            isinstance(value, str) for value in raw_dependencies
        ):
            raise ReleaseReadinessError(
                f"build evidence {distribution} dependency evidence differs"
            )
        if not isinstance(top_levels, list) or not all(
            isinstance(value, str) for value in top_levels
        ):
            raise ReleaseReadinessError(f"build evidence {distribution} module evidence differs")
        wheels[distribution] = _WheelInput(
            distribution_name=distribution,
            version=str(record.get("version")),
            path=first_path,
            sha256=digest,
            size_bytes=size_bytes,
            requires_python=str(record.get("requires_python")),
            requires_dist=tuple(raw_dependencies),
            top_level_modules=tuple(top_levels),
        )
    return manifest_content, document, wheels


def _fixed_timestamp(source_date_epoch: int) -> str:
    return (
        datetime.fromtimestamp(source_date_epoch, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _cyclonedx_document(
    *,
    wheel: _WheelInput,
    dependencies: tuple[ResolvedDependency, ...],
    source_commit: str,
    source_date_epoch: int,
) -> dict[str, object]:
    root_ref = f"pkg:pypi/{quote(wheel.distribution_name)}@{quote(wheel.version)}"
    components = [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{quote(dependency.name)}@{quote(dependency.version)}",
            "name": dependency.name,
            "version": dependency.version,
            "purl": f"pkg:pypi/{quote(dependency.name)}@{quote(dependency.version)}",
            "properties": [{"name": "alephain:exact_requirement", "value": dependency.requirement}],
        }
        for dependency in dependencies
    ]
    dependency_refs = [component["bom-ref"] for component in components]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": (
            "urn:uuid:"
            f"{uuid5(NAMESPACE_URL, f'{wheel.sha256}:{source_commit}:{source_date_epoch}')}"
        ),
        "version": 1,
        "metadata": {
            "timestamp": _fixed_timestamp(source_date_epoch),
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": wheel.distribution_name,
                "version": wheel.version,
                "purl": root_ref,
                "hashes": [{"alg": "SHA-256", "content": wheel.sha256}],
            },
            "properties": [
                {"name": "alephain:source_commit", "value": source_commit},
                {
                    "name": "alephain:source_date_epoch",
                    "value": str(source_date_epoch),
                },
            ],
        },
        "components": components,
        "dependencies": [
            {"ref": root_ref, "dependsOn": dependency_refs},
            *({"ref": value, "dependsOn": []} for value in dependency_refs),
        ],
    }


def _subject(name: str, content: bytes) -> dict[str, object]:
    return {"name": name, "digest": {"sha256": _sha256(content)}}


def _binding(
    *, candidate_version: str, category: str, filename: str, content: bytes
) -> ImmutableToolkitArtifactBindingV1:
    digest = _sha256(content)
    return ImmutableToolkitArtifactBindingV1(
        coordinate=(
            f"pending://custos/toolkit-rc/{candidate_version}/{category}/{filename}@sha256:{digest}"
        ),
        sha256=digest,
        size_bytes=len(content),
    )


def prepare_toolkit_rc_release_readiness(
    *,
    repository_root: Path,
    build_root: Path,
    lock_path: Path,
    contract_schema_path: Path,
    contract_asset_index_path: Path,
    toolkit_extraction_receipt_path: Path,
    toolkit_typing_closure_receipt_path: Path,
    pre_import_verifier_receipt_path: Path,
    output_root: Path,
) -> ReleaseReadinessArtifacts:
    """Generate deterministic unsigned protected-release evidence without contacting a service."""

    repository_root = repository_root.resolve()
    build_root = build_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise ReleaseReadinessError(
            "protected-release readiness output is immutable and must not exist"
        )
    manifest_content, build_document, wheels = _load_wheels(build_root)
    candidate_version = str(build_document["candidate_version"])
    source_commit = build_document.get("source_commit")
    source_date_epoch = build_document.get("source_date_epoch")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ReleaseReadinessError("build source commit is not exact")
    if not isinstance(source_date_epoch, int) or source_date_epoch < 315_532_800:
        raise ReleaseReadinessError("build SOURCE_DATE_EPOCH is invalid")

    lock_bytes = lock_path.read_bytes()
    dependency_sets = {
        distribution: resolve_locked_dependencies(
            raw_requirements=wheel.requires_dist,
            candidate_version=candidate_version,
            lock_path=lock_path,
        )
        for distribution, wheel in wheels.items()
    }
    lock_document = {
        "schema_version": "alephain.custos.toolkit-rc-dependency-locks.v1",
        "candidate_version": candidate_version,
        "source_commit": source_commit,
        "uv_lock_sha256": _sha256(lock_bytes),
        "distributions": {
            distribution: [dependency.document() for dependency in dependencies]
            for distribution, dependencies in sorted(dependency_sets.items())
        },
    }
    dependency_lock_bytes = _json_bytes(lock_document)

    sbom_bytes = {
        distribution: _json_bytes(
            _cyclonedx_document(
                wheel=wheel,
                dependencies=dependency_sets[distribution],
                source_commit=source_commit,
                source_date_epoch=source_date_epoch,
            )
        )
        for distribution, wheel in wheels.items()
    }
    support_paths = (
        contract_schema_path,
        contract_asset_index_path,
        toolkit_extraction_receipt_path,
        toolkit_typing_closure_receipt_path,
        pre_import_verifier_receipt_path,
    )
    support_bytes: dict[Path, bytes] = {}
    for path in support_paths:
        try:
            support_bytes[path] = path.read_bytes()
        except OSError as exc:
            raise ReleaseReadinessError(f"release authority input is missing: {path}") from exc

    subjects = [
        *(_subject(wheel.path.name, wheel.path.read_bytes()) for wheel in wheels.values()),
        *(
            _subject(f"{distribution}.cdx.json", content)
            for distribution, content in sbom_bytes.items()
        ),
        *(_subject(path.name, content) for path, content in support_bytes.items()),
        _subject("toolkit-rc-dependency-locks.json", dependency_lock_bytes),
    ]
    subjects.sort(key=lambda value: str(value["name"]))
    timestamp = _fixed_timestamp(source_date_epoch)
    provenance_document = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": ("https://custos.the-alephain-guild/build-types/toolkit-rc/v1"),
                "externalParameters": {
                    "candidate_version": candidate_version,
                    "source_commit": source_commit,
                    "source_date_epoch": source_date_epoch,
                },
                "internalParameters": {
                    "build_seam": "scripts/toolkit_rc_build.py",
                    "release_readiness_seam": ("scripts/toolkit_rc_release_readiness.py"),
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+{SOURCE_REPOSITORY}@{source_commit}",
                        "digest": {"gitCommit": source_commit},
                    },
                    {
                        "uri": "file:uv.lock",
                        "digest": {"sha256": _sha256(lock_bytes)},
                    },
                ],
            },
            "runDetails": {
                "builder": {"id": WORKFLOW_IDENTITY},
                "metadata": {
                    "invocationId": (f"urn:sha256:{_sha256(manifest_content)}"),
                    "startedOn": timestamp,
                    "finishedOn": timestamp,
                },
                "byproducts": [
                    _subject("toolkit-rc-build-manifest-input.json", manifest_content),
                    _subject("toolkit-rc-dependency-locks.json", dependency_lock_bytes),
                ],
            },
        },
    }
    provenance_bytes = _json_bytes(provenance_document)

    pending_receipt = ToolkitRcPendingReceiptV1(
        candidate_version=candidate_version,
        source_commit=source_commit,
        source_date_epoch=source_date_epoch,
        build_manifest=_binding(
            candidate_version=candidate_version,
            category="build",
            filename="toolkit-rc-build-manifest-input.json",
            content=manifest_content,
        ),
        dependency_lock_evidence=_binding(
            candidate_version=candidate_version,
            category="dependencies",
            filename="toolkit-rc-dependency-locks.json",
            content=dependency_lock_bytes,
        ),
        cyclonedx_sboms=tuple(
            ToolkitRcCycloneDxSbomV1(
                role=DISTRIBUTION_ROLES[distribution],
                distribution_name=distribution,
                wheel_sha256=wheels[distribution].sha256,
                artifact=_binding(
                    candidate_version=candidate_version,
                    category="sbom",
                    filename=f"{distribution}.cdx.json",
                    content=sbom_bytes[distribution],
                ),
            )
            for distribution in sorted(wheels)
        ),
        provenance_statement=_binding(
            candidate_version=candidate_version,
            category="provenance",
            filename="toolkit-rc.intoto.json",
            content=provenance_bytes,
        ),
        final_blockers=(FINAL_BLOCKER,),
    )
    pending_bytes = _json_bytes(pending_receipt.model_dump(mode="json"))

    output_root.mkdir(parents=True)
    dependency_lock_path = output_root / "toolkit-rc-dependency-locks.json"
    dependency_lock_path.write_bytes(dependency_lock_bytes)
    sbom_paths: dict[str, Path] = {}
    for distribution, content in sbom_bytes.items():
        path = output_root / f"{distribution}.cdx.json"
        path.write_bytes(content)
        sbom_paths[distribution] = path
    provenance_path = output_root / "toolkit-rc.intoto.json"
    provenance_path.write_bytes(provenance_bytes)
    pending_receipt_path = output_root / "toolkit-rc-pending.json"
    pending_receipt_path.write_bytes(pending_bytes)
    return ReleaseReadinessArtifacts(
        candidate_version=candidate_version,
        source_commit=source_commit,
        source_date_epoch=source_date_epoch,
        dependency_lock_path=dependency_lock_path,
        sbom_paths=sbom_paths,
        provenance_path=provenance_path,
        pending_receipt_path=pending_receipt_path,
    )


def _release_binding(
    *, candidate_version: str, category: str, path: Path
) -> ImmutableToolkitArtifactBindingV1:
    content = path.read_bytes()
    digest = _sha256(content)
    return ImmutableToolkitArtifactBindingV1(
        coordinate=(
            f"artifact://custos/toolkit-rc/{candidate_version}/{category}/{path.name}"
            f"@sha256:{digest}"
        ),
        sha256=digest,
        size_bytes=len(content),
    )


def _validate_sigstore_bundle(path: Path, provenance_path: Path) -> bytes:
    if any(value in path.name.lower() for value in ("fixture", "test", "fake")):
        raise ReleaseReadinessError("test or fake Sigstore bundles are forbidden")
    content, document = _read_json(path, "Sigstore bundle")
    media_type = document.get("mediaType")
    if (
        not isinstance(media_type, str)
        or not media_type.startswith("application/vnd.dev.sigstore.bundle")
        or not isinstance(document.get("verificationMaterial"), dict)
        or not any(name in document for name in ("messageSignature", "dsseEnvelope"))
    ):
        raise ReleaseReadinessError("Sigstore bundle structure differs")
    try:
        verification = subprocess.run(
            [
                "sigstore",
                "verify",
                "identity",
                "--bundle",
                str(path),
                "--cert-identity",
                WORKFLOW_IDENTITY,
                "--cert-oidc-issuer",
                OIDC_ISSUER,
                str(provenance_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReleaseReadinessError(f"Sigstore verification command is unavailable: {exc}") from exc
    if verification.returncode != 0:
        raise ReleaseReadinessError(
            "Sigstore verification failed for the exact protected-release workflow identity: "
            f"{verification.stderr.strip()}"
        )
    return content


def assemble_toolkit_rc_publication_inputs(
    *,
    build_root: Path,
    readiness_root: Path,
    contract_schema_path: Path,
    contract_asset_index_path: Path,
    toolkit_extraction_receipt_path: Path,
    toolkit_typing_closure_receipt_path: Path,
    pre_import_verifier_receipt_path: Path,
    sigstore_bundle_path: Path,
    output_root: Path,
) -> PublicationAssemblyArtifacts:
    """Assemble publication inputs after the workflow verifies a production bundle."""

    build_root = build_root.resolve()
    readiness_root = readiness_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise ReleaseReadinessError("publication input output is immutable and must not exist")
    _, build_document, wheels = _load_wheels(build_root)
    candidate_version = str(build_document["candidate_version"])
    source_commit = str(build_document["source_commit"])
    pending_path = readiness_root / "toolkit-rc-pending.json"
    _, pending_document = _read_json(pending_path, "pending receipt")
    try:
        pending = ToolkitRcPendingReceiptV1.model_validate(pending_document)
    except ValueError as exc:
        raise ReleaseReadinessError(f"pending receipt contract differs: {exc}") from exc
    if (
        pending.candidate_version != candidate_version
        or pending.source_commit != source_commit
        or pending.ready is not False
    ):
        raise ReleaseReadinessError("pending receipt does not bind the build")

    dependency_lock_path = readiness_root / "toolkit-rc-dependency-locks.json"
    dependency_lock_content, dependency_document = _read_json(
        dependency_lock_path, "dependency lock evidence"
    )
    if (
        _sha256(dependency_lock_content) != pending.dependency_lock_evidence.sha256
        or dependency_document.get("candidate_version") != candidate_version
        or dependency_document.get("source_commit") != source_commit
    ):
        raise ReleaseReadinessError("dependency lock evidence differs from the pending receipt")
    distributions = dependency_document.get("distributions")
    if not isinstance(distributions, dict) or set(distributions) != set(DISTRIBUTION_ROLES):
        raise ReleaseReadinessError("dependency lock distribution matrix differs")

    provenance_path = readiness_root / "toolkit-rc.intoto.json"
    provenance_content, provenance_document = _read_json(provenance_path, "SLSA provenance")
    if (
        _sha256(provenance_content) != pending.provenance_statement.sha256
        or provenance_document.get("_type") != "https://in-toto.io/Statement/v1"
        or provenance_document.get("predicateType") != "https://slsa.dev/provenance/v1"
    ):
        raise ReleaseReadinessError("SLSA provenance differs from the pending receipt")
    _validate_sigstore_bundle(sigstore_bundle_path, provenance_path)

    source_paths: dict[str, Path] = {}

    def bind(category: str, path: Path) -> ImmutableToolkitArtifactBindingV1:
        try:
            binding = _release_binding(
                candidate_version=candidate_version,
                category=category,
                path=path.resolve(),
            )
        except OSError as exc:
            raise ReleaseReadinessError(f"publication object is missing: {path}") from exc
        existing = source_paths.get(binding.coordinate)
        if existing is not None and existing.read_bytes() != path.read_bytes():
            raise ReleaseReadinessError("one publication coordinate binds different bytes")
        source_paths[binding.coordinate] = path.resolve()
        return binding

    shared_bindings = {
        "contract_schema": bind("contracts", contract_schema_path),
        "contract_asset_index": bind("contracts", contract_asset_index_path),
        "dependency_lock_evidence": bind("dependencies", dependency_lock_path),
        "slsa_provenance": bind("provenance", provenance_path),
        "sigstore_attestation": bind("attestations", sigstore_bundle_path),
        "toolkit_extraction_receipt": bind("prerequisites", toolkit_extraction_receipt_path),
        "toolkit_typing_closure_receipt": bind(
            "prerequisites", toolkit_typing_closure_receipt_path
        ),
        "pre_import_verifier_receipt": bind("prerequisites", pre_import_verifier_receipt_path),
    }
    members: list[ToolkitRcMemberV1] = []
    for distribution in sorted(wheels):
        wheel = wheels[distribution]
        role = DISTRIBUTION_ROLES[distribution]
        sbom_path = readiness_root / f"{distribution}.cdx.json"
        pending_sbom = next(
            (value for value in pending.cyclonedx_sboms if value.role is role), None
        )
        if pending_sbom is None or _sha256(sbom_path.read_bytes()) != (
            pending_sbom.artifact.sha256
        ):
            raise ReleaseReadinessError(f"{distribution} SBOM differs from the pending receipt")
        raw_dependencies = distributions[distribution]
        if not isinstance(raw_dependencies, list):
            raise ReleaseReadinessError(f"{distribution} dependency locks are invalid")
        dependencies: list[LockedToolkitDependencyV1] = []
        for value in raw_dependencies:
            if not isinstance(value, dict):
                raise ReleaseReadinessError(f"{distribution} dependency lock record is invalid")
            try:
                dependencies.append(
                    LockedToolkitDependencyV1(
                        name=value["name"],
                        version=value["version"],
                        requirement=value["requirement"],
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ReleaseReadinessError(
                    f"{distribution} dependency lock contract differs"
                ) from exc
        members.append(
            ToolkitRcMemberV1(
                role=role,
                distribution_name=distribution,
                version=candidate_version,
                python_requires=(
                    ">=3.11" if role is ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL else ">=3.12,<3.13"
                ),
                nautilus_version=(
                    None if role is ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL else "1.230.0"
                ),
                top_level_modules=wheel.top_level_modules,
                dependencies=tuple(dependencies),
                wheel=bind("wheels", wheel.path),
                sbom=bind("sbom", sbom_path),
                source_repository=SOURCE_REPOSITORY,
                source_commit=source_commit,
                **shared_bindings,
            )
        )
    manifest = ToolkitRcReceiptManifestV1(
        candidate_version=candidate_version,
        members=tuple(members),
    )
    manifest_bytes = _json_bytes(manifest.model_dump(mode="json"))
    source_document = {coordinate: str(path) for coordinate, path in sorted(source_paths.items())}

    output_root.mkdir(parents=True)
    manifest_path = output_root / "toolkit-rc-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    object_sources_path = output_root / "toolkit-rc-object-sources.json"
    object_sources_path.write_bytes(_json_bytes(source_document))
    return PublicationAssemblyArtifacts(
        candidate_version=candidate_version,
        manifest_path=manifest_path,
        object_sources_path=object_sources_path,
    )


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--build-root", required=True, type=Path)
    parser.add_argument("--lock-path", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--contract-schema",
        type=Path,
        default=Path("docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json"),
    )
    parser.add_argument(
        "--contract-asset-index",
        type=Path,
        default=Path("docs/authority/strategy-contract-assets-v1.json"),
    )
    parser.add_argument(
        "--toolkit-extraction-receipt",
        type=Path,
        default=Path("docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"),
    )
    parser.add_argument(
        "--toolkit-typing-closure-receipt",
        type=Path,
        default=Path("docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"),
    )
    parser.add_argument(
        "--pre-import-verifier-receipt",
        type=Path,
        default=Path("docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"),
    )


def _repository_path(repository_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repository_root / path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    _add_common_paths(prepare)
    prepare.add_argument("--output-root", required=True, type=Path)
    assemble = subparsers.add_parser("assemble")
    _add_common_paths(assemble)
    assemble.add_argument("--readiness-root", required=True, type=Path)
    assemble.add_argument("--sigstore-bundle", required=True, type=Path)
    assemble.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository_root = arguments.repository_root.resolve()
    if arguments.command == "assemble":
        assembly = assemble_toolkit_rc_publication_inputs(
            build_root=arguments.build_root,
            readiness_root=arguments.readiness_root,
            contract_schema_path=_repository_path(repository_root, arguments.contract_schema),
            contract_asset_index_path=_repository_path(
                repository_root, arguments.contract_asset_index
            ),
            toolkit_extraction_receipt_path=_repository_path(
                repository_root, arguments.toolkit_extraction_receipt
            ),
            toolkit_typing_closure_receipt_path=_repository_path(
                repository_root, arguments.toolkit_typing_closure_receipt
            ),
            pre_import_verifier_receipt_path=_repository_path(
                repository_root, arguments.pre_import_verifier_receipt
            ),
            sigstore_bundle_path=arguments.sigstore_bundle,
            output_root=arguments.output_root,
        )
        print(
            json.dumps(
                {
                    "candidate_version": assembly.candidate_version,
                    "manifest_path": str(assembly.manifest_path),
                    "object_sources_path": str(assembly.object_sources_path),
                    "status": "PENDING_REMOTE_PUBLICATION",
                },
                sort_keys=True,
            )
        )
        return 0
    evidence = prepare_toolkit_rc_release_readiness(
        repository_root=repository_root,
        build_root=arguments.build_root,
        lock_path=_repository_path(repository_root, arguments.lock_path),
        contract_schema_path=_repository_path(repository_root, arguments.contract_schema),
        contract_asset_index_path=_repository_path(repository_root, arguments.contract_asset_index),
        toolkit_extraction_receipt_path=_repository_path(
            repository_root, arguments.toolkit_extraction_receipt
        ),
        toolkit_typing_closure_receipt_path=_repository_path(
            repository_root, arguments.toolkit_typing_closure_receipt
        ),
        pre_import_verifier_receipt_path=_repository_path(
            repository_root, arguments.pre_import_verifier_receipt
        ),
        output_root=arguments.output_root,
    )
    print(
        json.dumps(
            {
                "candidate_version": evidence.candidate_version,
                "pending_receipt_path": str(evidence.pending_receipt_path),
                "provenance_path": str(evidence.provenance_path),
                "status": "PENDING_PROTECTED_RELEASE",
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "PublicationAssemblyArtifacts",
    "ReleaseReadinessArtifacts",
    "ReleaseReadinessError",
    "ResolvedDependency",
    "assemble_toolkit_rc_publication_inputs",
    "prepare_toolkit_rc_release_readiness",
    "resolve_locked_dependencies",
]


if __name__ == "__main__":
    raise SystemExit(main())

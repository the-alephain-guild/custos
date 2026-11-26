"""Local artifact consistency preparation, without execution authorization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from fractions import Fraction
from importlib.resources import files
from pathlib import Path

from custos_toolkit.contracts.strategy_execution import (
    FrozenJsonObject,
    StrategyArtifactRefV1,
    StrategyLocalContextSupportV1,
    StrategyManifestV1,
    canonical_json_bytes,
    canonical_json_digest,
    deep_freeze_json,
    parse_and_freeze_json_object,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from custos.artifacts.archive import QuarantinedWheel, quarantine_wheel
from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError
from custos.artifacts.policy import ArchiveLimitsV1
from custos.artifacts.runtime import (
    VerifiedArtifactMemberV1,
    _strict_json_object,
    verify_execution_member_files,
    verify_release_execution_bindings,
)
from custos.core.toolkit_identity import (
    RuntimeToolkitSnapshot,
    build_identity_from_release_bom,
    judge,
)
from custos.local.manifest import LocalRunManifestV1

SUPPORT_NAME = "resources/local-context-support-v1.json"
_MAX_JSON_EXPONENT = 1024


@dataclass(frozen=True, slots=True)
class LocalStrategyMaterialV1:
    """Consistent material bytes, never a permit or a readiness decision."""

    quarantine: QuarantinedWheel
    strategy_wheel: VerifiedArtifactMemberV1
    manifest: StrategyManifestV1
    artifact_ref: StrategyArtifactRefV1
    release_bom: FrozenJsonObject
    manifest_digest: str
    artifact_ref_digest: str
    release_bom_digest: str
    support: StrategyLocalContextSupportV1
    support_digest: str


def _invalid(detail: str) -> ArtifactVerificationError:
    return ArtifactVerificationError(ArtifactVerificationCode.MEMBER_SET_MISMATCH, detail)


def _exact_schema_number(token: str) -> int | Fraction:
    decimal = Decimal(token)
    exponent = decimal.as_tuple().exponent
    if not isinstance(exponent, int) or abs(exponent) > _MAX_JSON_EXPONENT:
        raise ValueError("configuration number exceeds the schema numeric range")
    number = Fraction(decimal)
    return number.numerator if number.denominator == 1 else number


def bind_local_strategy_material(
    *,
    material: LocalStrategyMaterialV1,
    run: LocalRunManifestV1,
    effective_config_bytes: bytes,
    toolkit: RuntimeToolkitSnapshot,
) -> FrozenJsonObject:
    """Check a prospective run before import. This does not verify an operator permit."""
    if (
        run.artifact != material.artifact_ref
        or run.release_bom_sha256 != material.release_bom_digest
        or run.entry_point != material.quarantine.verified_entry_point
        or run.entry_point != material.manifest.entry_point
    ):
        raise _invalid("local run artifact, BOM or entry point differs")
    if not toolkit.identity.trusted or toolkit.identity_sha256 != run.toolkit_identity_sha256:
        raise _invalid("local run toolkit identity snapshot differs or is untrusted")
    version = toolkit.identity.candidate_version
    if (
        version is None
        or material.manifest.base_contracts_version != version
        or material.manifest.engine_toolkit_version != version
    ):
        raise _invalid("local run toolkit distribution versions differ")
    consistency = judge(build_identity_from_release_bom(material.release_bom), toolkit.identity)
    if not consistency.consistent:
        raise _invalid(consistency.refusal_reason() or "local run toolkit binding differs")

    scope = material.manifest.trading_scope
    if (
        scope.connector != run.account.connector
        or scope.leverage != run.limits.leverage
        or any(re.fullmatch(r"[A-Z0-9]+-USD-PERP", pair) is None for pair in scope.pairs)
        or {f"{pair}.PAPERTRADE" for pair in scope.pairs} != set(run.instrument_ids)
    ):
        raise _invalid("local run trading scope differs from the strategy artifact")

    if hashlib.sha256(effective_config_bytes).hexdigest() != run.effective_config_digest:
        raise _invalid("local run effective configuration bytes differ")
    try:
        effective = parse_and_freeze_json_object(effective_config_bytes)
    except (DecimalException, TypeError, ValueError):
        raise _invalid("local run effective configuration digest or content differs") from None
    if "trading" in effective:
        raise _invalid("local run effective configuration cannot override trading scope")

    schema_name = next(
        member.name
        for member in material.manifest.runtime_artifacts
        if member.sha256 == material.manifest.config_schema_sha256
        and member.media_type == "application/schema+json"
    )
    schema_payload = next(
        asset.content
        for asset in material.quarantine.runtime_artifacts
        if asset.name == schema_name
    )
    try:
        # Preserve the same exact numerics for both schema limits and config values,
        # including divisibility checks under an arbitrary Decimal context.
        parse_and_freeze_json_object(schema_payload)
        schema = json.loads(schema_payload, parse_float=_exact_schema_number)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry())
        valid = validator.is_valid(
            json.loads(effective_config_bytes, parse_float=_exact_schema_number)
        )
    except (DecimalException, SchemaError, TypeError, ValueError, Unresolvable):
        raise _invalid(
            "local run config schema is invalid or has an unresolved reference"
        ) from None
    if not valid:
        raise _invalid("local run effective configuration fails the artifact schema")
    return effective


def prepare_local_strategy_material(
    *,
    wheel_path: Path,
    manifest_bytes: bytes,
    artifact_ref: StrategyArtifactRefV1,
    release_bom_bytes: bytes,
    quarantine_parent: Path,
    limits: ArchiveLimitsV1,
) -> LocalStrategyMaterialV1:
    """Prepare pinned material without importing code or accepting operator authority."""
    if not isinstance(artifact_ref, StrategyArtifactRefV1):
        raise _invalid("local material requires StrategyArtifactRefV1")
    release_bom = _strict_json_object(release_bom_bytes, "local release BOM")
    manifest_value = _strict_json_object(manifest_bytes, "local strategy manifest")
    try:
        manifest = StrategyManifestV1.model_validate(manifest_value)
    except ValidationError:
        raise _invalid("local strategy manifest is invalid") from None
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        manifest_digest != artifact_ref.manifest_sha256
        or len(manifest_bytes) != artifact_ref.manifest_size_bytes
    ):
        raise _invalid("local manifest bytes differ from StrategyArtifactRefV1")
    try:
        verify_release_execution_bindings(release_bom, artifact_ref, manifest=manifest)
    except ValueError:
        raise _invalid("local execution metadata bindings differ") from None
    members = release_bom["members"]
    manifest_members = [member for member in members if member.get("role") == "strategy_manifest"]
    if len(manifest_members) != 1 or (
        manifest_members[0].get("sha256") != manifest_digest
        or type(manifest_members[0].get("size_bytes")) is not int
        or manifest_members[0].get("size_bytes") != len(manifest_bytes)
        or release_bom.get("strategy_manifest_sha256") != manifest_digest
    ):
        raise _invalid("local manifest bytes differ from release BOM")
    try:
        wheel_size = wheel_path.stat().st_size
    except OSError:
        raise _invalid("local strategy wheel is unavailable") from None
    if (
        artifact_ref.artifact_size_bytes > limits.max_total_uncompressed_bytes
        or wheel_size != artifact_ref.artifact_size_bytes
    ):
        raise _invalid("local strategy wheel size exceeds the expected boundary")
    strategy_member = next(member for member in members if member.get("role") == "strategy_wheel")
    verified = verify_execution_member_files(release_bom, {strategy_member["name"]: wheel_path})[0]
    quarantine = quarantine_wheel(
        verified.verified_content,
        entry_point_group=manifest.entry_point_group,
        entry_point=manifest.entry_point,
        limits=limits,
        quarantine_parent=quarantine_parent,
        required_runtime_artifacts=tuple(
            member.model_dump(mode="json") for member in artifact_ref.required_runtime_artifacts
        ),
    )
    try:
        declarations = [
            member for member in manifest.runtime_artifacts if member.name == SUPPORT_NAME
        ]
        actual = [member for member in quarantine.runtime_artifacts if member.name == SUPPORT_NAME]
        if (
            len(declarations) != 1
            or len(actual) != 1
            or declarations[0].media_type != "application/json"
        ):
            raise _invalid("local support declaration is absent or ambiguous")
        payload = actual[0].content
        value = _strict_json_object(payload, "local context support")
        try:
            support = StrategyLocalContextSupportV1.model_validate(value)
        except ValidationError:
            raise _invalid("local context support is invalid") from None
        if canonical_json_bytes(support.model_dump(mode="json")) != payload:
            raise _invalid("local context support must have the exact canonical fields")
        schema = (
            files("custos_toolkit")
            .joinpath("contracts/assets/local_strategy_execution_context_v1.schema.json")
            .read_bytes()
        )
        schema_value = _strict_json_object(schema, "loaded toolkit local context schema")
        if (
            schema_value.get("$id") != support.context_schema_id
            or hashlib.sha256(schema).hexdigest() != support.context_schema_sha256
        ):
            raise _invalid("local context support differs from the loaded toolkit schema")
        config_schemas = [
            member
            for member in manifest.runtime_artifacts
            if member.sha256 == manifest.config_schema_sha256
            and member.media_type == "application/schema+json"
        ]
        if len(config_schemas) != 1:
            raise _invalid("local config schema is not uniquely bound")
        return LocalStrategyMaterialV1(
            quarantine=quarantine,
            strategy_wheel=verified,
            manifest=manifest,
            artifact_ref=artifact_ref,
            release_bom=deep_freeze_json(release_bom),
            manifest_digest=manifest_digest,
            artifact_ref_digest=canonical_json_digest(artifact_ref.model_dump(mode="json")),
            release_bom_digest=hashlib.sha256(release_bom_bytes).hexdigest(),
            support=support,
            support_digest=hashlib.sha256(payload).hexdigest(),
        )
    except Exception:
        quarantine.destroy()
        raise

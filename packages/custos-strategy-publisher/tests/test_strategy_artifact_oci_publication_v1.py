from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from example_producer import (
    GHCR_REPOSITORY,
    OIDC_SUBJECT,
    SOURCE_REF,
    WORKFLOW_IDENTITY,
    WORKFLOW_REF,
)

ROOT = Path(__file__).resolve().parents[1]

from custos_strategy_publisher import oci_publication  # noqa: E402
from custos_strategy_publisher.model import (  # noqa: E402
    ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION,
    DSSE_PAYLOAD_TYPE,
    STRATEGY_RELEASE_PREDICATE_TYPE,
    ArtifactAttestationRefV1,
    canonical_json_bytes,
)
from custos_strategy_publisher.oci_primitives import (  # noqa: E402
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_ISSUER,
    OCI_MANIFEST_MEDIA_TYPE,
    RELEASE_ARTIFACT_TYPE,
    RELEASE_LAYER_MEDIA_TYPES,
    SIGSTORE_ARTIFACT_TYPE,
    SIGSTORE_BUNDLE_MEDIA_TYPE,
    OciBlobReadbackV1,
    OciBlobV1,
    OciDescriptorV1,
    OciManifestReadbackV1,
    OciPublicationError,
    PublicationWorkflowIdentityV1,
)
from custos_strategy_publisher.oci_publication import (  # noqa: E402
    RELEASE_CONFIG_V1_MEDIA_TYPE,
    OciCandidateV1,
    StrategyArtifactOciPublicationReceiptV1,
    publish_verified_candidate,
)


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class MemoryRegistryV1:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[OciDescriptorV1, bytes]] = {}
        self.manifests: dict[str, tuple[OciDescriptorV1, bytes]] = {}
        self.tags: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.returned_manifest_digest: str | None = None
        self.readback_manifest_body: bytes | None = None

    def put_blob(
        self,
        repository: str,
        descriptor: OciDescriptorV1,
        content: bytes,
    ) -> OciDescriptorV1:
        assert repository == GHCR_REPOSITORY
        assert descriptor.digest == _digest(content)
        assert descriptor.size_bytes == len(content)
        self.calls.append(("put_blob", descriptor.digest))
        self.blobs[descriptor.digest] = (descriptor, content)
        return descriptor

    def get_blob(self, repository: str, digest: str) -> OciBlobReadbackV1:
        assert repository == GHCR_REPOSITORY
        self.calls.append(("get_blob", digest))
        visible_digests = {
            descriptor["digest"]
            for _, content in self.manifests.values()
            for descriptor in (
                json.loads(content)["config"],
                *json.loads(content)["layers"],
            )
        }
        if digest not in visible_digests:
            raise OciPublicationError("blob is not manifest-visible")
        descriptor, content = self.blobs[digest]
        return OciBlobReadbackV1(descriptor=descriptor, content=content)

    def resolve_manifest(
        self,
        repository: str,
        reference: str,
    ) -> OciManifestReadbackV1 | None:
        assert repository == GHCR_REPOSITORY
        self.calls.append(("resolve_manifest", reference))
        digest = self.tags.get(reference, reference if reference in self.manifests else None)
        if digest is None:
            return None
        descriptor, content = self.manifests[digest]
        return OciManifestReadbackV1(descriptor=descriptor, content=content)

    def put_manifest(
        self,
        repository: str,
        reference: str,
        media_type: str,
        content: bytes,
    ) -> OciDescriptorV1:
        assert repository == GHCR_REPOSITORY
        assert media_type == OCI_MANIFEST_MEDIA_TYPE
        self.calls.append(("put_manifest", reference))
        manifest = json.loads(content)
        descriptor = OciDescriptorV1(
            media_type=media_type,
            digest=_digest(content),
            size_bytes=len(content),
            artifact_type=manifest.get("artifactType"),
        )
        self.manifests[descriptor.digest] = (descriptor, content)
        if not reference.startswith("sha256:"):
            self.tags[reference] = descriptor.digest
        if self.returned_manifest_digest is not None:
            return replace(descriptor, digest=self.returned_manifest_digest)
        return descriptor

    def get_manifest(self, repository: str, digest: str) -> OciManifestReadbackV1:
        assert repository == GHCR_REPOSITORY
        self.calls.append(("get_manifest", digest))
        descriptor, content = self.manifests[digest]
        return OciManifestReadbackV1(
            descriptor=descriptor,
            content=self.readback_manifest_body or content,
        )


def _workflow(*, run_id: int = 42, run_attempt: int = 2) -> PublicationWorkflowIdentityV1:
    return PublicationWorkflowIdentityV1(
        workflow_identity=WORKFLOW_IDENTITY,
        workflow_ref=WORKFLOW_REF,
        workflow_run_id=run_id,
        workflow_run_attempt=run_attempt,
        source_ref=SOURCE_REF,
        oidc_issuer=GITHUB_OIDC_ISSUER,
        oidc_subject=OIDC_SUBJECT,
        oidc_audience=GITHUB_OIDC_AUDIENCE,
    )


def _release_layers() -> tuple[OciBlobV1, ...]:
    fixtures = (
        ("strategy_artifact", "strategy.whl", b"deterministic-wheel"),
        ("strategy_manifest", "strategy-manifest.json", b'{"entry":"factory"}'),
        (
            "strategy_artifact_ref",
            "strategy-artifact-ref-v1.json",
            json.dumps(
                {
                    "artifact_coordinate": "fixture://strategy.whl@sha256:" + "1" * 64,
                    "artifact_kind": "wheel",
                    "artifact_sha256": "1" * 64,
                    "artifact_size_bytes": 19,
                    "base_contracts_version": "1.0.0rc1",
                    "build_inputs": [{"name": "uv.lock", "sha256": "2" * 64}],
                    "contract_schema_sha256": "3" * 64,
                    "engine": "nautilus",
                    "engine_toolkit_version": "1.0.0rc1",
                    "engine_version": "2.0.0rc5+sodex.2",
                    "manifest_sha256": "4" * 64,
                    "manifest_size_bytes": 19,
                    "normalized_source_tree_sha256": "5" * 64,
                    "python_version": "3.12.13",
                    "required_runtime_artifacts": [
                        {
                            "media_type": "application/schema+json",
                            "name": "resources/strategy-config-v1.schema.json",
                            "role": "runtime_artifact",
                            "sha256": "6" * 64,
                            "size_bytes": 64,
                        }
                    ],
                    "sbom_sha256": "7" * 64,
                    "schema_version": 1,
                    "source_commit": "a" * 40,
                    "source_repository": ("https://github.com/alchymia-labs/philosophers-stone"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        ("strategy_release_bom", "strategy-release-bom.json", b'{"members":[]}'),
        ("strategy_sbom", "strategy.spdx.json", b'{"spdxVersion":"SPDX-2.3"}'),
        (
            "strategy_release_statement",
            "strategy-release-statement.json",
            b'{"_type":"https://in-toto.io/Statement/v1"}',
        ),
    )
    return tuple(
        OciBlobV1(
            role=role,
            name=name,
            media_type=RELEASE_LAYER_MEDIA_TYPES[role],
            content=content,
        )
        for role, name, content in fixtures
    )


def _attestation_ref(
    release_layers: tuple[OciBlobV1, ...],
    bundle: bytes,
) -> ArtifactAttestationRefV1:
    statement = next(
        layer for layer in release_layers if layer.role == "strategy_release_statement"
    )
    statement_digest = hashlib.sha256(statement.content).hexdigest()
    bundle_digest = hashlib.sha256(bundle).hexdigest()
    return ArtifactAttestationRefV1(
        schema_version=ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION,
        statement_coordinate=(
            f"workflow-artifact://strategy-release-statement.json@sha256:{statement_digest}"
        ),
        statement_sha256=statement_digest,
        bundle_coordinate=(
            f"workflow-artifact://strategy-release.bundle.json@sha256:{bundle_digest}"
        ),
        bundle_sha256=bundle_digest,
        payload_type=DSSE_PAYLOAD_TYPE,
        predicate_type=STRATEGY_RELEASE_PREDICATE_TYPE,
    )


def _candidate(
    *,
    workflow: PublicationWorkflowIdentityV1 | None = None,
    producer_commit: str = "a" * 40,
) -> OciCandidateV1:
    layers = _release_layers()
    bundle = b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}'
    return OciCandidateV1(
        repository=GHCR_REPOSITORY,
        discovery_tag="trend-supertrend-0.1.0rc1",
        producer_repository="alchymia-labs/philosophers-stone",
        producer_commit=producer_commit,
        strategy_coordinate="trend/supertrend@0.1.0rc1",
        workflow=workflow or _workflow(),
        release_layers=layers,
        attestation_bundle=OciBlobV1(
            role="attestation_bundle",
            name="strategy-release.bundle.json",
            media_type=SIGSTORE_BUNDLE_MEDIA_TYPE,
            content=bundle,
        ),
        attestation_ref=_attestation_ref(layers, bundle),
    )


def test_v1_derives_oci_artifact_ref_after_stable_release_manifest() -> None:
    registry = MemoryRegistryV1()

    bundle = publish_verified_candidate(_candidate(), registry)

    artifact_ref = bundle.artifact_ref.to_mapping()
    receipt = bundle.receipt
    assert artifact_ref["schema_version"] == 1
    assert artifact_ref["artifact_kind"] == "oci"
    assert artifact_ref["registry"] == "ghcr.io"
    assert artifact_ref["repository"] == ("alchymia-labs/v1-team-strategy-artifacts")
    assert artifact_ref["manifest_digest"] == receipt.release_manifest_digest
    assert hashlib.sha256(bundle.artifact_ref.canonical_bytes()).hexdigest() == (
        receipt.artifact_ref_digest
    )
    assert receipt.artifact_ref == bundle.artifact_ref
    assert [layer["role"] for layer in artifact_ref["layers"]] == list(RELEASE_LAYER_MEDIA_TYPES)
    assert registry.tags[receipt.discovery_tag] == receipt.release_manifest_digest
    first_manifest_put = next(
        index for index, call in enumerate(registry.calls) if call[0] == "put_manifest"
    )
    first_blob_readback = next(
        index for index, call in enumerate(registry.calls) if call[0] == "get_blob"
    )
    assert first_manifest_put < first_blob_readback


def test_release_config_v1_is_stable_and_has_no_back_reference_or_provenance() -> None:
    registry = MemoryRegistryV1()

    bundle = publish_verified_candidate(_candidate(), registry)
    release_descriptor, release_bytes = registry.manifests[bundle.receipt.release_manifest_digest]
    assert release_descriptor.artifact_type == RELEASE_ARTIFACT_TYPE
    release_manifest = json.loads(release_bytes)
    config_digest = release_manifest["config"]["digest"]
    config_descriptor, config_bytes = registry.blobs[config_digest]
    assert config_descriptor.media_type == RELEASE_CONFIG_V1_MEDIA_TYPE
    config = json.loads(config_bytes)

    forbidden = {
        "artifact_ref",
        "artifact_ref_digest",
        "manifest_digest",
        "release_manifest_digest",
        "workflow_identity",
        "workflow_ref",
        "workflow_run_id",
        "workflow_run_attempt",
        "source_ref",
        "oidc_issuer",
        "oidc_subject",
        "oidc_audience",
        "producer_commit",
        "source_commit",
    }
    assert forbidden.isdisjoint(config)
    assert config == {
        "artifact_kind": "oci",
        "artifact_type": RELEASE_ARTIFACT_TYPE,
        "schema_version": "alephain.strategy-release-oci-config.v1",
    }


def test_identical_release_inputs_are_stable_across_runs_and_commits() -> None:
    first = publish_verified_candidate(
        _candidate(workflow=_workflow(run_id=41, run_attempt=1)),
        MemoryRegistryV1(),
    )
    second = publish_verified_candidate(
        _candidate(
            workflow=_workflow(run_id=99, run_attempt=7),
            producer_commit="c" * 40,
        ),
        MemoryRegistryV1(),
    )

    assert first.artifact_ref.canonical_bytes() == second.artifact_ref.canonical_bytes()
    assert first.artifact_ref.digest() == second.artifact_ref.digest()
    assert first.receipt.release_manifest_digest == (second.receipt.release_manifest_digest)
    assert first.receipt.attestation_manifest_digest != (second.receipt.attestation_manifest_digest)
    assert canonical_json_bytes(first.receipt.to_mapping()) != canonical_json_bytes(
        second.receipt.to_mapping()
    )


def test_attestation_referrer_binds_derived_artifact_ref_and_release_subject() -> None:
    registry = MemoryRegistryV1()

    bundle = publish_verified_candidate(_candidate(), registry)
    attestation_descriptor, attestation_bytes = registry.manifests[
        bundle.receipt.attestation_manifest_digest
    ]
    assert attestation_descriptor.artifact_type == SIGSTORE_ARTIFACT_TYPE
    attestation_manifest = json.loads(attestation_bytes)
    assert attestation_manifest["subject"]["digest"] == (bundle.receipt.release_manifest_digest)
    config_digest = attestation_manifest["config"]["digest"]
    _, config_bytes = registry.blobs[config_digest]
    attestation_config = json.loads(config_bytes)
    assert attestation_config["artifact_ref_digest"] == bundle.artifact_ref.digest()
    assert attestation_config["release_manifest_digest"] == (bundle.receipt.release_manifest_digest)
    assert attestation_config["publisher_profile"] == "github_oidc"
    assert bundle.receipt.to_mapping()["publisher_profile"] == "github_oidc"
    assert attestation_config["workflow_run_id"] == 42


def test_publication_receipt_schema_requires_the_github_oidc_publisher_profile() -> None:
    schema = oci_publication.publication_receipt_v1_json_schema()

    assert schema["properties"]["publisher_profile"] == {
        "const": "github_oidc",
        "type": "string",
    }
    assert "publisher_profile" in schema["required"]


def test_receipt_rejects_cross_object_artifact_ref_substitution() -> None:
    bundle = publish_verified_candidate(_candidate(), MemoryRegistryV1())
    value = bundle.receipt.to_mapping()
    artifact_ref = dict(value["artifact_ref"])
    artifact_ref["manifest_digest"] = "sha256:" + ("0" * 64)
    value["artifact_ref"] = artifact_ref

    with pytest.raises(ValueError, match="StrategyOciArtifactRefV1"):
        StrategyArtifactOciPublicationReceiptV1.from_mapping(value)


def test_receipt_rejects_an_unimplemented_team_kms_publisher_profile() -> None:
    bundle = publish_verified_candidate(_candidate(), MemoryRegistryV1())
    value = bundle.receipt.to_mapping()
    value["publisher_profile"] = "team_kms"

    with pytest.raises(ValueError, match="publisher profile"):
        StrategyArtifactOciPublicationReceiptV1.from_mapping(value)


def test_v1_candidate_does_not_accept_caller_supplied_artifact_ref_digest() -> None:
    candidate = _candidate()
    values = {field: getattr(candidate, field) for field in candidate.__dataclass_fields__}
    values["artifact_ref_digest"] = "b" * 64

    with pytest.raises(TypeError, match="artifact_ref_digest"):
        OciCandidateV1(**values)


def test_v1_candidate_does_not_accept_caller_supplied_publisher_profile() -> None:
    candidate = _candidate()
    values = {field: getattr(candidate, field) for field in candidate.__dataclass_fields__}
    values["publisher_profile"] = "team_kms"

    with pytest.raises(TypeError, match="publisher_profile"):
        OciCandidateV1(**values)


def test_v1_rejects_existing_discovery_tag_without_overwrite() -> None:
    registry = MemoryRegistryV1()
    existing = b'{"existing":true}'
    existing_descriptor = OciDescriptorV1(
        media_type=OCI_MANIFEST_MEDIA_TYPE,
        digest=_digest(existing),
        size_bytes=len(existing),
    )
    registry.manifests[existing_descriptor.digest] = (existing_descriptor, existing)
    registry.tags["trend-supertrend-0.1.0rc1"] = existing_descriptor.digest

    with pytest.raises(OciPublicationError, match="already exists"):
        publish_verified_candidate(_candidate(), registry)


def test_v1_rejects_registry_returned_manifest_drift() -> None:
    registry = MemoryRegistryV1()
    registry.returned_manifest_digest = "sha256:" + ("f" * 64)

    with pytest.raises(OciPublicationError, match="returned manifest descriptor"):
        publish_verified_candidate(_candidate(), registry)


def test_v1_rejects_release_manifest_readback_byte_drift() -> None:
    registry = MemoryRegistryV1()
    registry.readback_manifest_body = b'{"drift":true}'

    with pytest.raises(OciPublicationError, match="readback bytes"):
        publish_verified_candidate(_candidate(), registry)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("wrong subject digest", lambda s: {**s, "digest": "sha256:" + ("a" * 64)}),
        ("wrong subject size", lambda s: {**s, "size": s["size"] + 1}),
        ("wrong subject media type", lambda s: {**s, "mediaType": "application/json"}),
        ("dropped subject", lambda s: None),
    ],
)
def test_v1_rejects_an_attestation_not_bound_to_this_release(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    mutate: object,
) -> None:
    """Inject at the builder: a readback cannot catch this, it compares wrong to wrong.

    This replaces referrer discovery, which GHCR does not implement.
    """

    original = oci_publication._attestation_manifest_bytes

    def broken(release, blobs, descriptors):  # type: ignore[no-untyped-def]
        document = json.loads(original(release, blobs, descriptors))
        replacement = cast("Callable[[dict[str, object]], dict[str, object] | None]", mutate)(
            document["subject"]
        )
        if replacement is None:
            document.pop("subject")
        else:
            document["subject"] = replacement
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    monkeypatch.setattr(oci_publication, "_attestation_manifest_bytes", broken)

    with pytest.raises(OciPublicationError, match="subject binding differs"):
        publish_verified_candidate(_candidate(), MemoryRegistryV1())


def test_v1_publication_needs_no_referrers_capability() -> None:
    """A registry offering only Distribution 2.0 verbs must be enough to publish."""

    registry = MemoryRegistryV1()
    publish_verified_candidate(_candidate(), registry)

    assert not hasattr(registry, "list_referrers")
    assert all(call[0] != "list_referrers" for call in registry.calls)


def test_v1_rejects_reordered_release_layers() -> None:
    candidate = _candidate()

    with pytest.raises(ValueError, match="release layer order"):
        replace(candidate, release_layers=tuple(reversed(candidate.release_layers)))

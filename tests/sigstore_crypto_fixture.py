from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import rfc8785
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier
from sigstore._internal.sct import _pack_digitally_signed
from sigstore._utils import KeyID
from sigstore_protobuf_specs.dev.sigstore.bundle import v1 as bundle_v1
from sigstore_protobuf_specs.dev.sigstore.common import v1 as common_v1
from sigstore_protobuf_specs.dev.sigstore.rekor import v1 as rekor_v1
from sigstore_protobuf_specs.dev.sigstore.trustroot import v1 as trustroot_v1
from sigstore_protobuf_specs.io import intoto

from custos.artifacts.policy import SigstoreIdentityV1
from custos.artifacts.verification_types import DigestSubject, SigstoreVerificationRequest

ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW_IDENTITY = (
    "https://github.com/alephain-guild/custos/.github/workflows/release.yml@refs/heads/main"
)
SOURCE_REPOSITORY = "https://github.com/alephain-guild/custos"
_GITHUB_REPOSITORY_COORDINATE = "alephain-guild/custos"
SUBJECT_NAME = "custos-1.0.0-py3-none-any.whl"
SUBJECT_SHA256 = "12" * 32

DIFFERENT_ISSUER = "https://issuer.example.invalid"
DIFFERENT_WORKFLOW_IDENTITY = (
    "https://github.com/attacker/custos/.github/workflows/release.yml@refs/heads/main"
)
DIFFERENT_SUBJECT_SHA256 = "34" * 32

_OIDC_ISSUER_OID = ObjectIdentifier("1.3.6.1.4.1.57264.1.1")
_GITHUB_REPOSITORY_OID = ObjectIdentifier("1.3.6.1.4.1.57264.1.5")
_SCT_LIST_OID = ObjectIdentifier("1.3.6.1.4.1.11129.2.4.2")
_DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_TRUSTED_ROOT_MEDIA_TYPE = "application/vnd.dev.sigstore.trustedroot+json;version=0.1"
_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"


@dataclass(frozen=True)
class OfflineSigstoreFixture:
    bundle_path: Path
    request: SigstoreVerificationRequest
    identity: SigstoreIdentityV1
    required_subjects: tuple[DigestSubject, ...]


def _public_key_der(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _key_id(key: ec.EllipticCurvePublicKey) -> bytes:
    return hashlib.sha256(_public_key_der(key)).digest()


def _root_certificate(
    key: ec.EllipticCurvePrivateKey,
    *,
    now: datetime,
) -> x509.Certificate:
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Custos tests"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Local Fulcio root"),
        ]
    )
    public_key = key.public_key()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )


def _serialized_sct_list(
    *,
    log_id: bytes,
    timestamp_ms: int,
    signature: bytes,
) -> bytes:
    sct = b"".join(
        [
            struct.pack("!B", 0),
            log_id,
            struct.pack("!Q", timestamp_ms),
            struct.pack("!H", 0),
            struct.pack("!BBH", 4, 3, len(signature)),
            signature,
        ]
    )
    serialized = struct.pack("!H", len(sct)) + sct
    tls_list = struct.pack("!H", len(serialized)) + serialized
    if len(tls_list) < 128:
        der_length = bytes([len(tls_list)])
    else:
        der_length = b"\x81" + bytes([len(tls_list)])
    return b"\x04" + der_length + tls_list


def _leaf_certificate(
    *,
    root_key: ec.EllipticCurvePrivateKey,
    root_certificate: x509.Certificate,
    leaf_key: ec.EllipticCurvePrivateKey,
    ct_key: ec.EllipticCurvePrivateKey,
    now: datetime,
    identity: str,
    issuer: str,
    repository_coordinate: str = _GITHUB_REPOSITORY_COORDINATE,
) -> x509.Certificate:
    leaf_public_key = leaf_key.public_key()
    base_builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Custos release")]))
        .issuer_name(root_certificate.subject)
        .public_key(leaf_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity)]),
            critical=False,
        )
        .add_extension(x509.UnrecognizedExtension(_OIDC_ISSUER_OID, issuer.encode()), False)
        .add_extension(
            x509.UnrecognizedExtension(
                _GITHUB_REPOSITORY_OID,
                repository_coordinate.encode(),
            ),
            False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
    )

    ct_log_id = _key_id(ct_key.public_key())
    timestamp_ms = int(now.timestamp() * 1000)
    placeholder_signature = ct_key.sign(b"placeholder", ec.ECDSA(hashes.SHA256()))
    placeholder_certificate = base_builder.add_extension(
        x509.UnrecognizedExtension(
            _SCT_LIST_OID,
            _serialized_sct_list(
                log_id=ct_log_id,
                timestamp_ms=timestamp_ms,
                signature=placeholder_signature,
            ),
        ),
        critical=False,
    ).sign(root_key, hashes.SHA256())
    placeholder_sct = placeholder_certificate.extensions.get_extension_for_class(
        x509.PrecertificateSignedCertificateTimestamps
    ).value[0]
    signed_sct_data = _pack_digitally_signed(
        placeholder_sct,
        placeholder_certificate,
        KeyID(_key_id(root_key.public_key())),
    )
    sct_signature = ct_key.sign(signed_sct_data, ec.ECDSA(hashes.SHA256()))

    return base_builder.add_extension(
        x509.UnrecognizedExtension(
            _SCT_LIST_OID,
            _serialized_sct_list(
                log_id=ct_log_id,
                timestamp_ms=timestamp_ms,
                signature=sct_signature,
            ),
        ),
        critical=False,
    ).sign(root_key, hashes.SHA256())


def _trust_log(
    key: ec.EllipticCurvePublicKey,
    *,
    now: datetime,
    base_url: str,
) -> trustroot_v1.TransparencyLogInstance:
    log_id = _key_id(key)
    valid_for = common_v1.TimeRange(
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
    )
    return trustroot_v1.TransparencyLogInstance(
        base_url=base_url,
        hash_algorithm=common_v1.HashAlgorithm.SHA2_256,
        public_key=common_v1.PublicKey(
            raw_bytes=_public_key_der(key),
            key_details=common_v1.PublicKeyDetails.PKIX_ECDSA_P256_SHA_256,
            valid_for=valid_for,
        ),
        log_id=common_v1.LogId(key_id=log_id),
        checkpoint_key_id=common_v1.LogId(key_id=log_id),
    )


def _trusted_root_bytes(
    *,
    root_certificate: x509.Certificate,
    rekor_key: ec.EllipticCurvePublicKey,
    ct_key: ec.EllipticCurvePublicKey,
    now: datetime,
) -> bytes:
    validity = common_v1.TimeRange(
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
    )
    authority = trustroot_v1.CertificateAuthority(
        subject=common_v1.DistinguishedName(
            organization="Custos tests",
            common_name="Local Fulcio root",
        ),
        uri="https://fulcio.local.invalid",
        cert_chain=common_v1.X509CertificateChain(
            certificates=[
                common_v1.X509Certificate(
                    raw_bytes=root_certificate.public_bytes(serialization.Encoding.DER)
                )
            ]
        ),
        valid_for=validity,
    )
    trusted_root = trustroot_v1.TrustedRoot(
        media_type=_TRUSTED_ROOT_MEDIA_TYPE,
        tlogs=[
            _trust_log(
                rekor_key,
                now=now,
                base_url="https://rekor.local.invalid",
            )
        ],
        certificate_authorities=[authority],
        ctlogs=[
            _trust_log(
                ct_key,
                now=now,
                base_url="https://ct.local.invalid",
            )
        ],
        timestamp_authorities=[],
    )
    return trusted_root.to_json().encode()


def _dsse_payload(subject_sha256: str) -> bytes:
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicate": {},
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "digest": {"sha256": subject_sha256},
                "name": SUBJECT_NAME,
            }
        ],
    }
    return json.dumps(statement, separators=(",", ":"), sort_keys=True).encode()


def _dsse_pae(payload: bytes) -> bytes:
    payload_type = _DSSE_PAYLOAD_TYPE.encode()
    return b" ".join(
        [
            b"DSSEv1",
            str(len(payload_type)).encode(),
            payload_type,
            str(len(payload)).encode(),
            payload,
        ]
    )


def _rekor_body(
    *,
    payload: bytes,
    signature: bytes,
    certificate: x509.Certificate,
    envelope_json: bytes,
) -> bytes:
    body: dict[str, Any] = {
        "apiVersion": "0.0.1",
        "kind": "dsse",
        "spec": {
            "envelopeHash": {
                "algorithm": "sha256",
                "value": hashlib.sha256(envelope_json).hexdigest(),
            },
            "payloadHash": {
                "algorithm": "sha256",
                "value": hashlib.sha256(payload).hexdigest(),
            },
            "signatures": [
                {
                    "signature": base64.b64encode(signature).decode(),
                    "verifier": base64.b64encode(
                        certificate.public_bytes(serialization.Encoding.PEM)
                    ).decode(),
                }
            ],
        },
    }
    return rfc8785.dumps(body)


def _checkpoint(
    *,
    root_hash: bytes,
    rekor_key: ec.EllipticCurvePrivateKey,
) -> str:
    log_id = _key_id(rekor_key.public_key())
    note = f"rekor.local.invalid\n1\n{base64.b64encode(root_hash).decode()}\n"
    signature = rekor_key.sign(note.encode(), ec.ECDSA(hashes.SHA256()))
    encoded_signature = base64.b64encode(log_id[:4] + signature).decode()
    return f"{note}\n\u2014 rekor.local.invalid {encoded_signature}\n"


def _bundle_bytes(
    *,
    payload: bytes,
    signature: bytes,
    certificate: x509.Certificate,
    rekor_key: ec.EllipticCurvePrivateKey,
    integrated_time: int,
    raw_blob: bool = False,
) -> bytes:
    envelope = intoto.Envelope(
        payload=payload,
        payload_type=_DSSE_PAYLOAD_TYPE,
        signatures=[intoto.Signature(sig=signature, keyid="")],
    )
    body = _rekor_body(
        payload=payload,
        signature=signature,
        certificate=certificate,
        envelope_json=envelope.to_json().encode(),
    )
    if raw_blob:
        body = rfc8785.dumps(
            {
                "apiVersion": "0.0.1",
                "kind": "hashedrekord",
                "spec": {
                    "data": {
                        "hash": {
                            "algorithm": "sha256",
                            "value": hashlib.sha256(payload).hexdigest(),
                        }
                    },
                    "signature": {
                        "content": base64.b64encode(signature).decode(),
                        "publicKey": {
                            "content": base64.b64encode(
                                certificate.public_bytes(serialization.Encoding.PEM)
                            ).decode()
                        },
                    },
                },
            }
        )
    body_b64 = base64.b64encode(body).decode()
    log_id = _key_id(rekor_key.public_key())
    log_index = 0
    set_payload = rfc8785.dumps(
        {
            "body": body_b64,
            "integratedTime": integrated_time,
            "logID": log_id.hex(),
            "logIndex": log_index,
        }
    )
    signed_entry_timestamp = rekor_key.sign(
        set_payload,
        ec.ECDSA(hashes.SHA256()),
    )
    root_hash = hashlib.sha256(b"\x00" + body).digest()
    entry = rekor_v1.TransparencyLogEntry(
        log_index=log_index,
        log_id=common_v1.LogId(key_id=log_id),
        kind_version=rekor_v1.KindVersion(
            kind="hashedrekord" if raw_blob else "dsse", version="0.0.1"
        ),
        integrated_time=integrated_time,
        inclusion_promise=rekor_v1.InclusionPromise(signed_entry_timestamp=signed_entry_timestamp),
        inclusion_proof=rekor_v1.InclusionProof(
            log_index=log_index,
            root_hash=root_hash,
            tree_size=1,
            hashes=[],
            checkpoint=rekor_v1.Checkpoint(
                envelope=_checkpoint(root_hash=root_hash, rekor_key=rekor_key)
            ),
        ),
        canonicalized_body=body,
    )
    bundle = bundle_v1.Bundle(
        media_type=_BUNDLE_MEDIA_TYPE,
        verification_material=bundle_v1.VerificationMaterial(
            certificate=common_v1.X509Certificate(
                raw_bytes=certificate.public_bytes(serialization.Encoding.DER)
            ),
            tlog_entries=[entry],
        ),
        **(
            {
                "message_signature": common_v1.MessageSignature(
                    message_digest=common_v1.HashOutput(
                        algorithm=common_v1.HashAlgorithm.SHA2_256,
                        digest=hashlib.sha256(payload).digest(),
                    ),
                    signature=signature,
                )
            }
            if raw_blob
            else {"dsse_envelope": envelope}
        ),
    )
    return bundle.to_json().encode()


def build_offline_sigstore_fixture(
    root: Path,
    *,
    statement_sha256: str = SUBJECT_SHA256,
    certificate_identity: str = WORKFLOW_IDENTITY,
    certificate_issuer: str = ISSUER,
) -> OfflineSigstoreFixture:
    root.mkdir(parents=True, exist_ok=True)
    quarantine_parent = root / "quarantine"
    quarantine_parent.mkdir()
    now = datetime.now(UTC).replace(microsecond=0)

    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    ct_key = ec.generate_private_key(ec.SECP256R1())
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    root_certificate = _root_certificate(root_key, now=now)
    leaf_certificate = _leaf_certificate(
        root_key=root_key,
        root_certificate=root_certificate,
        leaf_key=leaf_key,
        ct_key=ct_key,
        now=now,
        identity=certificate_identity,
        issuer=certificate_issuer,
    )

    payload = _dsse_payload(statement_sha256)
    signature = leaf_key.sign(_dsse_pae(payload), ec.ECDSA(hashes.SHA256()))
    bundle_path = root / "release.sigstore.json"
    bundle_path.write_bytes(
        _bundle_bytes(
            payload=payload,
            signature=signature,
            certificate=leaf_certificate,
            rekor_key=rekor_key,
            integrated_time=int(now.timestamp()),
        )
    )
    trusted_root_bytes = _trusted_root_bytes(
        root_certificate=root_certificate,
        rekor_key=rekor_key.public_key(),
        ct_key=ct_key.public_key(),
        now=now,
    )
    identity = SigstoreIdentityV1(
        issuer=ISSUER,
        workflow_identity=WORKFLOW_IDENTITY,
        source_repository=SOURCE_REPOSITORY,
    )
    required_subjects = (DigestSubject(name=SUBJECT_NAME, sha256=SUBJECT_SHA256),)
    request = SigstoreVerificationRequest(
        bundle_path=bundle_path,
        trusted_root_bytes=trusted_root_bytes,
        accepted_identities=(identity,),
        required_subjects=required_subjects,
        quarantine_parent=quarantine_parent,
    )
    return OfflineSigstoreFixture(
        bundle_path=bundle_path,
        request=request,
        identity=identity,
        required_subjects=required_subjects,
    )


def _flip_base64(value: str) -> str:
    raw = bytearray(base64.b64decode(value))
    raw[-1] ^= 1
    return base64.b64encode(raw).decode()


def tamper_bundle(
    path: Path,
    mutation: Literal["signature", "rekor_set"],
) -> None:
    bundle = json.loads(path.read_bytes())
    if mutation == "signature":
        signature = bundle["dsseEnvelope"]["signatures"][0]
        signature["sig"] = _flip_base64(signature["sig"])
    else:
        inclusion = bundle["verificationMaterial"]["tlogEntries"][0]["inclusionPromise"]
        inclusion["signedEntryTimestamp"] = _flip_base64(inclusion["signedEntryTimestamp"])
    path.write_text(json.dumps(bundle, separators=(",", ":")))


def build_offline_blob_fixture(
    root: Path, payload: bytes, identity: SigstoreIdentityV1
) -> tuple[Path, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
    root_key, leaf_key, ct_key, rekor_key = [
        ec.generate_private_key(ec.SECP256R1()) for _ in range(4)
    ]
    root_certificate = _root_certificate(root_key, now=now)
    certificate = _leaf_certificate(
        root_key=root_key,
        root_certificate=root_certificate,
        leaf_key=leaf_key,
        ct_key=ct_key,
        now=now,
        identity=identity.workflow_identity,
        issuer=identity.issuer,
        repository_coordinate=identity.source_repository.removeprefix("https://github.com/"),
    )
    bundle = root / "blob.sigstore.json"
    bundle.write_bytes(
        _bundle_bytes(
            payload=payload,
            signature=leaf_key.sign(payload, ec.ECDSA(hashes.SHA256())),
            certificate=certificate,
            rekor_key=rekor_key,
            integrated_time=int(now.timestamp()),
            raw_blob=True,
        )
    )
    trusted_root = _trusted_root_bytes(
        root_certificate=root_certificate,
        rekor_key=rekor_key.public_key(),
        ct_key=ct_key.public_key(),
        now=now,
    )
    return bundle, trusted_root

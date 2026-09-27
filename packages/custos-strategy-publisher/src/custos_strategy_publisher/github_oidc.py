"""Who a GitHub Actions job signs as, read from its OIDC token and its certificate.

A release is signed inside the publisher's reusable workflow, called from the
producer's repository. Fulcio then names two different things in the signing
certificate: the reusable workflow as the identity (the token's
``job_workflow_ref``) and the calling repository as the workflow repository (the
token's ``repository``). ``GITHUB_WORKFLOW_REF`` names neither: in a called
workflow it is the caller's own workflow file. So the identity a release is
built for is read from the token, and after signing the certificate is checked
to say the same.

Decoding the token here does not verify it and does not need to: Fulcio verifies
it before issuing a certificate, and the certificate is what gets checked.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from .oci_primitives import (
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_ISSUER,
    PublicationWorkflowIdentityV1,
)

# Fulcio's extension for the repository a GitHub workflow ran in (raw string value).
GITHUB_WORKFLOW_REPOSITORY_OID = "1.3.6.1.4.1.57264.1.5"
_REQUEST_TIMEOUT_SECONDS = 30
_JOB_WORKFLOW_REF_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+/\.github/workflows/[^@\s]+@\S+$")

__all__ = [
    "GitHubOidcError",
    "GitHubWorkflowClaimsV1",
    "certificate_identity",
    "read_workflow_claims",
    "request_oidc_token",
]


class GitHubOidcError(RuntimeError):
    """The job has no usable OIDC token, or its identity is not what was expected."""


@dataclass(frozen=True, slots=True)
class GitHubWorkflowClaimsV1:
    """The claims of a job's OIDC token that a release identity is made of."""

    issuer: str
    subject: str
    repository: str
    ref: str
    job_workflow_ref: str
    run_id: int
    run_attempt: int

    @property
    def workflow_identity(self) -> str:
        return f"https://github.com/{self.job_workflow_ref}"

    def publication_identity(self) -> PublicationWorkflowIdentityV1:
        return PublicationWorkflowIdentityV1(
            workflow_identity=self.workflow_identity,
            workflow_ref=self.job_workflow_ref,
            workflow_run_id=self.run_id,
            workflow_run_attempt=self.run_attempt,
            source_ref=self.ref,
            oidc_issuer=self.issuer,
            oidc_subject=self.subject,
            oidc_audience=GITHUB_OIDC_AUDIENCE,
        )


def request_oidc_token(environment: Mapping[str, str], audience: str = GITHUB_OIDC_AUDIENCE) -> str:
    """Ask the Actions runtime for this job's OIDC token."""

    url = environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise GitHubOidcError(
            "this job has no GitHub OIDC token; it must run in GitHub Actions "
            "with the id-token: write permission"
        )
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={urllib.parse.quote(audience)}",
        headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        body = json.loads(response.read())
    token = body.get("value") if isinstance(body, dict) else None
    if not isinstance(token, str) or token.count(".") != 2:
        raise GitHubOidcError("the Actions runtime returned no OIDC token")
    return token


def _claims(token: str) -> dict[str, object]:
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        value = json.loads(decoded)
    except (IndexError, ValueError, binascii.Error) as error:
        raise GitHubOidcError("the OIDC token is not a JWT") from error
    if not isinstance(value, dict):
        raise GitHubOidcError("the OIDC token claims are not an object")
    return value


def _claim(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise GitHubOidcError(f"the OIDC token has no {name} claim")
    return value


def _positive(claims: Mapping[str, object], name: str) -> int:
    value = _claim(claims, name)
    if not value.isdigit() or int(value) <= 0:
        raise GitHubOidcError(f"the OIDC token's {name} claim is not a positive integer")
    return int(value)


def read_workflow_claims(token: str) -> GitHubWorkflowClaimsV1:
    claims = _claims(token)
    issuer = _claim(claims, "iss")
    if issuer != GITHUB_OIDC_ISSUER:
        raise GitHubOidcError(f"the OIDC token was issued by {issuer}, not GitHub Actions")
    job_workflow_ref = _claim(claims, "job_workflow_ref")
    if _JOB_WORKFLOW_REF_RE.fullmatch(job_workflow_ref) is None:
        raise GitHubOidcError("the OIDC token's job_workflow_ref is not a workflow ref")
    return GitHubWorkflowClaimsV1(
        issuer=issuer,
        subject=_claim(claims, "sub"),
        repository=_claim(claims, "repository"),
        ref=_claim(claims, "ref"),
        job_workflow_ref=job_workflow_ref,
        run_id=_positive(claims, "run_id"),
        run_attempt=_positive(claims, "run_attempt"),
    )


def certificate_identity(bundle_bytes: bytes) -> tuple[str, str]:
    """The identity and workflow repository the bundle's signing certificate names."""

    from cryptography import x509

    try:
        bundle = json.loads(bundle_bytes)
        raw = bundle["verificationMaterial"]["certificate"]["rawBytes"]
        certificate = x509.load_der_x509_certificate(base64.b64decode(raw, validate=True))
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise GitHubOidcError("the signing bundle carries no leaf certificate") from error
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(names) != 1:
        raise GitHubOidcError("the signing certificate names more than one identity")
    try:
        extension = certificate.extensions.get_extension_for_oid(
            x509.ObjectIdentifier(GITHUB_WORKFLOW_REPOSITORY_OID)
        )
    except x509.ExtensionNotFound as error:
        raise GitHubOidcError("the signing certificate names no workflow repository") from error
    repository = extension.value
    if not isinstance(repository, x509.UnrecognizedExtension):
        raise GitHubOidcError("the certificate's workflow repository extension is malformed")
    return names[0], repository.value.decode("utf-8")

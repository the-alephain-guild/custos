"""One producer's publication identity, used as an example throughout the tests.

These are the values of the producer whose releases the contract goldens were
generated from; nothing in the publisher requires them.
"""

GHCR_REPOSITORY = "ghcr.io/alchymia-labs/v1-team-strategy-artifacts"
ARTIFACT_REF_REPOSITORY = GHCR_REPOSITORY.removeprefix("ghcr.io/")
WORKFLOW_REF = (
    "alchymia-labs/philosophers-stone/.github/workflows/"
    "publish-strategy-artifact.yml@refs/heads/main"
)
WORKFLOW_IDENTITY = f"https://github.com/{WORKFLOW_REF}"
SOURCE_REF = "refs/heads/main"
OIDC_SUBJECT = f"repo:alchymia-labs/philosophers-stone:ref:{SOURCE_REF}"

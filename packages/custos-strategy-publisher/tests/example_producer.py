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

# The names Philosophers-Stone's SuperTrend 2.1.1 release carries. The golden
# tests/goldens/philosophers-stone-supertrend-runtime.py.txt is the runtime module
# that producer generated at 375d6d8.
PS_SUPERTREND = {
    "registered_name": "supertrend",
    "display_name": "SuperTrend",
    "distribution": "alephain-strategy-supertrend",
    "version": "2.1.1",
    "package": "alephain_strategy_supertrend",
    "coordinate": "ps://strategy/trend/supertrend@2.1.1",
    "adapter_class": "SuperTrendRuntimeAdapterV1",
    "catalog_alias": "supertrend",
    "member_coordinate_prefix": "artifact://philosophers-stone/strategy/supertrend/2.1.1",
    "source_tree_name": "supertrend-source-tree-v1.bin",
    "source_tree_fragment": "supertrend-source-tree",
    "sbom_namespace": "https://philosophers-stone.the-alephain-guild/spdx/supertrend",
    "config_schema_id": (
        "https://philosophers-stone.the-alephain-guild/contracts/supertrend-config-v1.schema.json"
    ),
    "config_schema_title": "SuperTrendEffectiveConfigV1",
    "characterization_parameters": ("atr_multiplier", "atr_period"),
    "wheel_generator": "philosophers-stone-v1",
}

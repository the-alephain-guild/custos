"""Taking the account partition and registering the node is one scope, or it leaks.

``deploy`` claims an execution account partition, builds a node, validates the fact
bindings, attaches the bridges and only then registers the instance. Three of those
steps sat inside cleanup blocks; the validation between them did not. Any of its
five exits therefore left the partition claimed and the node undisposed -- and since
the instance is not in ``_active_nodes`` yet, ``stop`` cannot undo it either, so it
is a leak with no way back short of restarting the daemon.

The bite is the ordinary one: you get the config wrong, deploy fails, you fix the
config, and the corrected deployment is refused because the failed one still owns
the scope.
"""

from __future__ import annotations

import pytest

from custos.core.runner_fact import RunnerFactContractError
from custos.engines.nautilus import host as host_module
from tests.fixtures.fake_live_node import FakeLiveNode, FakeLiveNodeType
from tests.test_nt_trading_node_host import (
    _Artifact,
    _credential,
    _fact_host,
    _fact_spec,
    _StrategyDouble,
)


@pytest.fixture
def fake_nodes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(host_module, "LiveNode", FakeLiveNodeType)
    return FakeLiveNode


def _artifact(timeframe: str = "5-MINUTE") -> _Artifact:
    from types import SimpleNamespace as NS

    strategy = _StrategyDouble()
    strategy.config = NS(platforms=NS(nautilus=NS(bar_type=timeframe)))
    return _Artifact(strategy=strategy)


def _assert_took_nothing(host, spec: dict, nodes) -> None:
    """Whatever went wrong, the deployment must own nothing afterwards."""
    instance = spec["deployment_instance_id"]
    assert instance not in host._execution_account_partitions, (  # noqa: SLF001
        "a deployment that never started must not keep the account scope"
    )
    assert instance not in host._active_nodes  # noqa: SLF001
    assert nodes.instances, "precondition: a node was built before the validation ran"
    assert nodes.instances[-1].disposed, "the node it built must not be left behind"


@pytest.mark.asyncio
async def test_a_timeframe_mismatch_releases_the_account_scope(fake_nodes) -> None:
    host, _ = _fact_host()
    spec = _fact_spec(
        "lb4-timeframe",
        "binance_perpetual",
        trading_mode="testnet",
        strategy_config={"timeframe": "1-MINUTE"},
    )

    with pytest.raises(RunnerFactContractError, match="timeframe differs"):
        await host.deploy(spec, _credential(), _artifact("5-MINUTE"))

    _assert_took_nothing(host, spec, fake_nodes)


@pytest.mark.parametrize(
    ("label", "overrides", "expected"),
    [
        ("no-strategy-id", {"strategy_id": ""}, "canonical strategy_id"),
        (
            "bad-coverage-start",
            {"reconciliation_coverage_started_at": "not-a-timestamp"},
            "ISO-8601",
        ),
        (
            "unsupported-settlement",
            {"pairs": ["BTC-XYZ"]},
            "outside RunnerFact v1|not supported",
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_reachable_fact_validation_exit_releases_what_it_took(
    fake_nodes, label: str, overrides: dict, expected: str
) -> None:
    """Each exit of the fact-context build that a deploy can actually reach.

    Two of the five exits cannot be reached from here, and saying so is part of the
    answer rather than a gap: an empty ``deployment_spec_digest`` is refused by
    ``EngineLifecycleAuthority.from_spec`` before anything is claimed (covered
    below), and the settlement currency is derived from the pairs rather than read
    from the spec, so it is reached by giving it a pair that settles in nothing
    supported.
    """
    host, _ = _fact_host()
    spec = _fact_spec(
        f"lb4-{label}",
        "binance_perpetual",
        trading_mode="testnet",
        **overrides,
    )

    with pytest.raises(Exception, match=expected):
        await host.deploy(spec, _credential(), _artifact())

    _assert_took_nothing(host, spec, fake_nodes)


@pytest.mark.asyncio
async def test_a_spec_refused_before_the_claim_takes_nothing_either(fake_nodes) -> None:
    """An empty spec digest never reaches the fact context; it is refused earlier.

    Nothing is claimed and no node is built, so this one asserts the absence of both
    rather than their cleanup -- the same invariant read from the other side.
    """
    host, _ = _fact_host()
    spec = _fact_spec(
        "lb4-no-digest",
        "binance_perpetual",
        trading_mode="testnet",
        deployment_spec_digest="",
    )
    before = len(fake_nodes.instances)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        await host.deploy(spec, _credential(), _artifact())

    assert spec["deployment_instance_id"] not in host._execution_account_partitions  # noqa: SLF001
    assert len(fake_nodes.instances) == before, "refused before a node was even built"


@pytest.mark.asyncio
async def test_the_corrected_config_can_start_on_the_same_host(fake_nodes) -> None:
    """The scenario an operator actually hits: get it wrong, fix it, deploy again.

    Deliberately the *same* host. Starting a clean one would prove nothing -- the
    leak is precisely that the failed attempt keeps owning the scope in the host it
    failed on.
    """
    host, _ = _fact_host()
    scope = _fact_spec("lb4-scope-owner", "binance_perpetual")["credential_scope"]
    wrong = _fact_spec(
        "lb4-wrong",
        "binance_perpetual",
        trading_mode="testnet",
        credential_scope=scope,
        strategy_config={"timeframe": "1-MINUTE"},
    )
    with pytest.raises(RunnerFactContractError):
        await host.deploy(wrong, _credential(), _artifact("5-MINUTE"))

    corrected = _fact_spec(
        "lb4-corrected",
        "binance_perpetual",
        trading_mode="testnet",
        credential_scope=scope,
        strategy_config={"timeframe": "5-MINUTE"},
    )
    await host.deploy(corrected, _credential(), _artifact("5-MINUTE"))

    try:
        assert corrected["deployment_instance_id"] in host._active_nodes  # noqa: SLF001
    finally:
        await host.stop(corrected["deployment_instance_id"])


@pytest.mark.asyncio
async def test_a_deployment_that_did_start_keeps_the_scope(fake_nodes) -> None:
    """The control: releasing on failure must not become releasing on success."""
    host, _ = _fact_host()
    spec = _fact_spec("lb4-healthy", "binance_perpetual", trading_mode="testnet")

    await host.deploy(spec, _credential(), _artifact())

    try:
        assert spec["deployment_instance_id"] in host._execution_account_partitions  # noqa: SLF001
    finally:
        await host.stop(spec["deployment_instance_id"])


def test_the_scope_conflict_guard_still_refuses_a_second_owner() -> None:
    """And the guard the partition exists for is untouched.

    Checked on the claim itself rather than through two deploys: a second deploy is
    refused earlier by the one-node-per-event-loop guard, so routing through deploy
    would prove that other guard works, not this one.
    """
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.host import NtTradingNodeHost, _deployment_identity

    host = NtTradingNodeHost()
    scope = _fact_spec("lb4-contested", "binance_perpetual")["credential_scope"]

    def identity_of(spec: dict):
        return _deployment_identity(spec, EngineLifecycleAuthority.from_spec(spec))

    first = _fact_spec(
        "lb4-owner", "binance_perpetual", trading_mode="testnet", credential_scope=scope
    )
    second = _fact_spec(
        "lb4-usurper", "binance_perpetual", trading_mode="testnet", credential_scope=scope
    )

    host._claim_execution_account_partition(first, identity_of(first))  # noqa: SLF001

    with pytest.raises(RuntimeError):
        host._claim_execution_account_partition(second, identity_of(second))  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_failure_rolls_back_only_what_this_call_took(fake_nodes) -> None:
    """ "Release" must mean this deployment's partition, not the table.

    A sibling that already owns a scope has to keep it: clearing everything would
    let two live deployments end up on one venue account, which is the thing the
    partition exists to prevent.
    """
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.host import _deployment_identity

    host, _ = _fact_host()
    sibling = _fact_spec("lb4-sibling", "binance_perpetual", trading_mode="testnet")
    host._claim_execution_account_partition(  # noqa: SLF001 - seeding a prior owner
        sibling, _deployment_identity(sibling, EngineLifecycleAuthority.from_spec(sibling))
    )
    failing = _fact_spec(
        "lb4-innocent-bystander",
        "binance_perpetual",
        trading_mode="testnet",
        strategy_config={"timeframe": "1-MINUTE"},
    )

    with pytest.raises(RunnerFactContractError):
        await host.deploy(failing, _credential(), _artifact("5-MINUTE"))

    assert sibling["deployment_instance_id"] in host._execution_account_partitions  # noqa: SLF001
    assert failing["deployment_instance_id"] not in host._execution_account_partitions  # noqa: SLF001

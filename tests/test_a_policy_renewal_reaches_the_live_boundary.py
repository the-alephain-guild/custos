"""Renewing the signed policy must not stop the strategy that is running under it.

The reservation boundary binds a policy id when a deployment starts and kept it for
life. Renewal took a different path entirely: the control consumer verified the new
policy, advanced the durable head and acked, without ever touching a live boundary.
Reserving requires the boundary's policy to *be* the head, so after an ordinary
renewal every new order was refused -- even one well inside a limit that had not
changed.

The breaker already does the right thing across a config swap: apply_config keeps
the freeze and the high-water mark, because a cloud-side edit raises or lowers the
ceilings, it does not reset the trip. What was missing is anyone calling it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID

import pytest

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerStateAuthorityError
from tests.test_order_reservation import (
    INSTANCE_A,
    POLICY_ID,
    RUNNER_ID,
    TENANT_ID,
    _store,
)

SECOND_POLICY = UUID("20000000-0000-4000-8000-000000000002")


def _renew(store, *, policy_id: UUID = SECOND_POLICY) -> None:
    """Advance the durable head to a second signed revision, as a renewal does."""
    with store._outbox._connect() as connection:  # noqa: SLF001 - fixture wiring
        connection.execute(
            """
            INSERT INTO runner_cap_policy (
                policy_id, policy_revision, policy_digest,
                tenant_scope, trading_mode, runner_id, previous_policy_id,
                previous_policy_revision, previous_policy_digest,
                settlement_currency, max_order_notional, max_notional,
                effective_at_ns, expires_at_ns, policy_status, signer_key_id,
                signature_profile, exact_subject, fingerprint,
                verified_event_bytes_digest, exact_event_bytes, signed_policy,
                policy_json, consumed_at_ns
            ) SELECT ?, 2, ?, tenant_scope, trading_mode, runner_id, policy_id,
                     1, policy_digest, settlement_currency, max_order_notional,
                     max_notional, effective_at_ns, expires_at_ns, policy_status,
                     signer_key_id, signature_profile, exact_subject, ?, ?,
                     exact_event_bytes, signed_policy, policy_json, 2
              FROM runner_cap_policy WHERE policy_id = ?
            """,
            (str(policy_id), "1" * 64, "2" * 64, "3" * 64, str(POLICY_ID)),
        )
        connection.execute(
            """
            UPDATE runner_cap_policy_head
            SET policy_id = ?, policy_revision = 2, policy_digest = ?, updated_at_ns = 2
            WHERE tenant_scope = ? AND trading_mode = 'sandbox' AND runner_id = ?
            """,
            (str(policy_id), "1" * 64, TENANT_ID, str(RUNNER_ID)),
        )


def _config(max_notional: str = "1000") -> FallbackBreakerConfig:
    return FallbackBreakerConfig(
        max_notional=Decimal(max_notional),
        max_drawdown_pct=Decimal("10"),
        policy_id=POLICY_ID,
        owner_policy=True,
    )


def _boundary(store, breaker: FallbackBreaker | None = None) -> RunnerReservationBoundary:
    return RunnerReservationBoundary(
        store=store,
        deployment_instance_id=INSTANCE_A,
        policy_id=POLICY_ID,
        fallback_breaker=breaker or FallbackBreaker(_config()),
        trading_mode="sandbox",
    )


def _reserve(boundary: RunnerReservationBoundary, store, order_id: str, notional: str) -> None:
    store.reserve_order_notional_sync(
        event_id=f"reserve-{order_id}",
        deployment_instance_id=INSTANCE_A,
        client_order_id=order_id,
        policy_id=boundary.policy_id,
        requested_notional=Decimal(notional),
    )


def test_an_order_inside_the_limit_is_refused_after_a_renewal(tmp_path: Path) -> None:
    """The defect, stated as the strategy experiences it."""
    store = _store(tmp_path / "renewal.sqlite3")
    boundary = _boundary(store)
    _renew(store)

    with pytest.raises(RunnerStateAuthorityError, match="current effective runner policy"):
        store.reserve_order_notional_sync(
            event_id="stale",
            deployment_instance_id=INSTANCE_A,
            client_order_id="stale-entry",
            policy_id=POLICY_ID,
            requested_notional=Decimal("25"),
        )
    assert boundary.policy_id == POLICY_ID


def test_adopting_the_renewal_lets_the_strategy_keep_trading(tmp_path: Path) -> None:
    store = _store(tmp_path / "adopt.sqlite3")
    boundary = _boundary(store)
    _renew(store)

    boundary.adopt_policy(SECOND_POLICY, _config(), trading_mode="sandbox")

    _reserve(boundary, store, "after-renewal", "25")
    assert boundary.policy_id == SECOND_POLICY


def test_adopting_a_renewal_keeps_an_existing_freeze(tmp_path: Path) -> None:
    """A cloud-side edit raises or lowers the ceilings; it does not lift a trip."""
    store = _store(tmp_path / "frozen.sqlite3")
    breaker = FallbackBreaker(_config())
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("800"))
    assert breaker.frozen is True
    boundary = _boundary(store, breaker)
    _renew(store)

    boundary.adopt_policy(SECOND_POLICY, _config(max_notional="5000"), trading_mode="sandbox")

    assert breaker.frozen is True, "a renewal must not be a way to clear the breaker"
    assert breaker.config.max_notional == Decimal("5000"), "but the ceilings do change"


def test_adopting_a_renewal_keeps_the_high_water_mark(tmp_path: Path) -> None:
    store = _store(tmp_path / "peak.sqlite3")
    breaker = FallbackBreaker(
        FallbackBreakerConfig(
            max_notional=Decimal("1000"),
            max_drawdown_pct=Decimal("50"),
            policy_id=POLICY_ID,
            owner_policy=True,
        )
    )
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
    boundary = _boundary(store, breaker)
    _renew(store)

    boundary.adopt_policy(SECOND_POLICY, _config(max_notional="5000"), trading_mode="sandbox")

    assert breaker.peak_equity == Decimal("1000"), (
        "losing the mark would re-baseline the drawdown at whatever equity is now"
    )


def test_a_renewal_keeps_the_exposure_recorded_under_the_old_revision(
    tmp_path: Path,
) -> None:
    """The risk scope is tenant + mode + runner, so it spans revisions -- assert it."""
    store = _store(tmp_path / "scope.sqlite3")
    boundary = _boundary(store)
    _reserve(boundary, store, "before-renewal", "100")
    store.record_order_fill_sync(
        event_id="fill-before",
        deployment_instance_id=INSTANCE_A,
        client_order_id="before-renewal",
        fill_notional=Decimal("100"),
        fill_quantity=Decimal("1"),
        leaves_quantity=Decimal("0"),
    )
    _renew(store)

    boundary.adopt_policy(SECOND_POLICY, _config(), trading_mode="sandbox")

    with pytest.raises(RunnerStateAuthorityError, match="aggregate cap"):
        _reserve(boundary, store, "over-the-cap", "100")


def test_the_daemon_hands_a_renewal_to_the_boundary_of_that_mode() -> None:
    """The wiring: the consumer records the policy, and the boundary hears about it."""
    from custos.cli._daemon import _build_policy_renewal_notifier

    breaker = FallbackBreaker(_config())
    boundary = RunnerReservationBoundary(
        store=NS(),
        deployment_instance_id=INSTANCE_A,
        policy_id=POLICY_ID,
        fallback_breaker=breaker,
        trading_mode="sandbox",
    )
    boundaries = {str(INSTANCE_A): boundary}

    async def resolve(_trading_mode: str):
        return NS(owner_policy=True, policy_id=SECOND_POLICY, breaker=_config("5000"))

    notify = _build_policy_renewal_notifier(
        boundaries=boundaries, safety_policy_resolver=NS(resolve=resolve)
    )

    import asyncio

    asyncio.run(notify("sandbox"))

    assert boundary.policy_id == SECOND_POLICY
    assert breaker.config.max_notional == Decimal("5000")


@pytest.mark.asyncio
async def test_the_consumer_calls_the_notifier_after_it_commits() -> None:
    """Wiring, not capability: a notifier nobody calls changes nothing.

    Ordered deliberately -- the notification happens after the policy is durably
    recorded, because telling a boundary to adopt a policy that failed to commit
    would point it at a head that is not there.
    """
    from custos.core.runner_control_consumer import RunnerControlConsumerV1

    sequence: list[str] = []
    verified = NS(policy=NS(trading_mode="sandbox"))

    class _Store:
        async def record_verified_runner_safety_policy(self, value):
            sequence.append("commit")
            return NS(decision=NS(value="newer"))

    class _Message:
        subject = "crucible.runner.policy.v1.acme.sandbox"
        data = b"{}"

        async def ack(self):
            sequence.append("ack")

        async def term(self):  # pragma: no cover - not reached here
            sequence.append("term")

    async def notify(trading_mode: str) -> None:
        sequence.append(f"notify:{trading_mode}")

    consumer = RunnerControlConsumerV1(
        command_runtime=NS(),
        policy_authenticator=NS(verify=lambda **_kw: verified),
        state_store=_Store(),
        on_policy_committed=notify,
    )

    await consumer._process_policy(  # noqa: SLF001 - the unit under test
        NS(assert_policy_binding=lambda *_a: None), _Message()
    )

    assert sequence == ["commit", "notify:sandbox", "ack"]


@pytest.mark.asyncio
async def test_a_notifier_that_raises_does_not_undo_a_committed_policy() -> None:
    """The policy is on disk. Nak-ing it would only redeliver what already landed."""
    from structlog.testing import capture_logs

    from custos.core.runner_control_consumer import RunnerControlConsumerV1

    sequence: list[str] = []

    class _Store:
        async def record_verified_runner_safety_policy(self, value):
            return NS(decision=NS(value="newer"))

    class _Message:
        subject = "crucible.runner.policy.v1.acme.sandbox"
        data = b"{}"

        async def ack(self):
            sequence.append("ack")

        async def nak(self, delay: float = 0.0):
            sequence.append("nak")

    async def explode(_trading_mode: str) -> None:
        raise RuntimeError("a boundary refused the renewal")

    consumer = RunnerControlConsumerV1(
        command_runtime=NS(),
        policy_authenticator=NS(verify=lambda **_kw: NS(policy=NS(trading_mode="sandbox"))),
        state_store=_Store(),
        on_policy_committed=explode,
    )

    with capture_logs() as events:
        await consumer._process_policy(  # noqa: SLF001 - the unit under test
            NS(assert_policy_binding=lambda *_a: None), _Message()
        )

    assert sequence == ["ack"], "a committed policy must not be redelivered"
    assert any(event["event"] == "runner_policy_renewal_notification_failed" for event in events), (
        "and it must be loud about the boundaries that did not hear it"
    )


def test_the_daemon_hands_the_notifier_to_the_control_consumer() -> None:
    """The composition root is the last place this can fall apart.

    Everything above proves the notifier works and that the consumer calls it.
    Neither proves the daemon connects the two -- and the composition root is one
    long function with no seam to probe at runtime, so this reads its source.

    What it pins: the call site passes `on_policy_committed`, and what it passes
    is the notifier built over the same boundary registry the deployment path
    writes into. What it cannot pin: that any of it behaves correctly. That is
    what the tests above are for.
    """
    import ast
    import inspect

    from custos.cli import _daemon

    tree = ast.parse(inspect.getsource(_daemon))
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunnerControlConsumerV1"
    ]
    assert constructions, "the daemon no longer builds a control consumer"

    for call in constructions:
        notifier = next((kw.value for kw in call.keywords if kw.arg == "on_policy_committed"), None)
        assert notifier is not None, "a control consumer without a notifier renews nothing"
        assert isinstance(notifier, ast.Call)
        assert isinstance(notifier.func, ast.Name)
        assert notifier.func.id == "_build_policy_renewal_notifier"
        passed = {kw.arg: ast.unparse(kw.value) for kw in notifier.keywords}
        assert passed.get("boundaries") == "runner_safety_boundaries", (
            "the notifier must hold the same registry the deployment path writes into"
        )
        assert passed.get("safety_policy_resolver") == "safety_policy_resolver"


class TestARenewalStaysInsideItsOwnMode:
    """FR-5: a policy is signed per trading mode; a renewal is too.

    The notifier resolved limits for the mode the message carried and then walked
    every boundary in the registry. A runner with more than one mode enabled --
    which is what ``--enabled-modes`` is for -- had its testnet ceilings replaced
    by whatever sandbox was signed for, and an exposure that was legitimate under
    the testnet policy tripped the breaker on the next evaluation.
    """

    @staticmethod
    def _live(trading_mode: str, max_notional: str):
        breaker = FallbackBreaker(_config(max_notional))
        return breaker, RunnerReservationBoundary(
            store=NS(),
            deployment_instance_id=INSTANCE_A,
            policy_id=POLICY_ID,
            fallback_breaker=breaker,
            trading_mode=trading_mode,
        )

    @staticmethod
    def _notify(boundaries, *, max_notional: str = "100"):
        import asyncio

        from custos.cli._daemon import _build_policy_renewal_notifier

        async def resolve(_trading_mode: str):
            return NS(owner_policy=True, policy_id=SECOND_POLICY, breaker=_config(max_notional))

        return lambda mode: asyncio.run(
            _build_policy_renewal_notifier(
                boundaries=boundaries, safety_policy_resolver=NS(resolve=resolve)
            )(mode)
        )

    def test_a_sandbox_renewal_leaves_a_testnet_boundary_alone(self):
        testnet_breaker, testnet = self._live("testnet", "10000")
        _sandbox_breaker, sandbox = self._live("sandbox", "100")

        self._notify({"testnet-instance": testnet, "sandbox-instance": sandbox})("sandbox")

        assert testnet.policy_id == POLICY_ID, "a sandbox renewal is not testnet's head"
        assert testnet_breaker.config.max_notional == Decimal("10000")
        assert sandbox.policy_id == SECOND_POLICY, "and the mode it was signed for adopts it"

    def test_exposure_that_was_legitimate_stays_legitimate(self):
        """The consequence the report measured: the wrong ceiling trips the breaker."""
        testnet_breaker, testnet = self._live("testnet", "10000")

        self._notify({"testnet-instance": testnet})("sandbox")

        assert not testnet_breaker.evaluate(
            open_notional=Decimal("500"), current_equity=Decimal("1000")
        ).tripped

    def test_the_boundary_refuses_a_policy_from_another_mode_on_its_own(self):
        """A relaxed double: the selection layer removed, the boundary still refuses.

        Without this the inner check is a dead branch -- the notifier would be the
        only thing standing between a sandbox policy and a testnet deployment.
        """
        _breaker, testnet = self._live("testnet", "10000")

        with pytest.raises(RuntimeError, match="trading mode"):
            testnet.adopt_policy(SECOND_POLICY, _config("100"), trading_mode="sandbox")

        assert testnet.policy_id == POLICY_ID

    def test_a_renewal_for_a_mode_with_no_deployment_is_recorded_not_lost(self):
        from structlog.testing import capture_logs

        _breaker, testnet = self._live("testnet", "10000")

        with capture_logs() as events:
            self._notify({"testnet-instance": testnet})("live")

        assert any(event["event"] == "runner_policy_renewal_no_boundary" for event in events)

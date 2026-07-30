"""Binance refuses an order whose client order id is too long, so the id has to fit.

On 2026-07-30 the offline lane reached a real venue for the first time and every
`submit_order` came back as

    -4015 "Client order id length should be less than 36 chars"

against `O-20260730-044937-dcb00e520b45569e83b0-000-2`, which is 44 characters. Nothing
about it was intermittent: the id's shape made it too long every time, so this lane
could not place a single order on Binance. A green test suite and a green sandbox had
said nothing about it, because the sandbox matches locally and never submits.

These assertions reach the id through the runtime's own construction — this
repository's trader id builder, the strategy config the toolkit builds, and the
NautilusTrader generator those two feed — rather than through a format string copied
into the test. A copied format would only ever test the copy.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.cache.cache import Cache  # noqa: E402
from nautilus_trader.common.component import TestClock  # noqa: E402
from nautilus_trader.common.factories import OrderFactory  # noqa: E402
from nautilus_trader.model.identifiers import StrategyId, TraderId  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402

from custos.engines.nautilus.venue_binance import (  # noqa: E402
    BINANCE_CLIENT_ORDER_ID_LEN_LIMIT,
)

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit.config import load_config  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)

# The instance id from the failing run, in the shape a deployment instance really has.
OBSERVED_INSTANCE_ID = "dcb00e52-0b45-569e-83b0-2f1a4c7d9e11"

# The generator holds the order count in a 32-bit signed int — found by pushing
# `set_client_order_id_count` until it refused above 2**31 - 1.
#
# Two values matter, because generating *increments* before rendering:
#   - 2**31 - 2 renders the largest positive counter, "2147483647", ten characters.
#   - 2**31 - 1 increments past the ceiling and wraps to -2147483648, which renders as
#     eleven characters including the sign. That is the widest the component can ever be,
#     so it is the real worst case even though it is an overflow rather than a count.
# Both are exercised. An earlier version used only the first and described it as the
# widest possible, which was wrong.
LARGEST_POSITIVE_ORDER_COUNT = 2**31 - 2
ORDER_COUNT_THAT_WRAPS = 2**31 - 1


@pytest.fixture(scope="module")
def strategy_config(tmp_path_factory) -> NautilusTradingStrategyConfig:
    """A config built the way the runner builds one: from a file, through the builder.

    This is the registry's own path — load the strategy's YAML, hand it to
    ``build_nautilus_base_config``, construct the config from what comes back. Building
    the sections by hand here instead would skip the builder, which is where the id shape
    is decided, and the test would pass while the runner still failed.
    """

    directory = tmp_path_factory.mktemp("strategy")
    (directory / "config.yaml").write_text("strategy:\n  name: probe\n", encoding="utf-8")

    sections = build_nautilus_base_config(load_config(directory / "config.yaml"))
    return NautilusTradingStrategyConfig(**sections)


def _factory_as_the_engine_builds_it(
    config: NautilusTradingStrategyConfig,
    *,
    instance_id: str = OBSERVED_INSTANCE_ID,
    strategy_id: str = "SuperTrendStrategy-000",
) -> OrderFactory:
    """Build the factory the way a registered strategy does.

    The engine creates the factory inside ``Strategy.register``, reading the two id-shape
    flags off the strategy — which took them from its config. Both are read-only from
    Python, so nothing downstream of construction can change the id shape; the config is
    the only place that decides it. That is why the flags are read from a real strategy
    here rather than passed in by hand.
    """

    from custos.engines.nautilus.host import NtTradingNodeHost

    strategy = Strategy(config=config)
    return OrderFactory(
        trader_id=TraderId(NtTradingNodeHost._trader_id(instance_id)),
        strategy_id=StrategyId(strategy_id),
        clock=TestClock(),
        cache=Cache(database=None),
        use_uuid_client_order_ids=strategy.use_uuid_client_order_ids,
        use_hyphens_in_client_order_ids=strategy.use_hyphens_in_client_order_ids,
    )


def test_a_generated_client_order_id_fits_what_binance_accepts(strategy_config) -> None:
    """The id the runtime actually generates must be short enough to be accepted."""

    generated = _factory_as_the_engine_builds_it(strategy_config).generate_client_order_id().value

    assert len(generated) < BINANCE_CLIENT_ORDER_ID_LEN_LIMIT, (
        f"{generated!r} is {len(generated)} characters; Binance refuses anything not "
        f"shorter than {BINANCE_CLIENT_ORDER_ID_LEN_LIMIT} with -4015"
    )


def test_the_length_holds_at_the_worst_case_this_runner_can_reach(strategy_config) -> None:
    """Pins the property, not one measurement: nothing this runner varies changes it.

    Three things could push a structured id over the bound, and all three are pushed to
    their limit at once here — the order counter to both of its widest renderings, the
    trader tag to the longest this repository's builder can emit (it truncates to 20
    alphanumerics), and the strategy tag well past anything a strategy names itself.

    An id shape whose length depends on any of these has a ceiling rather than a fix, and
    passing today would only mean the ceiling is far away.
    """

    factory = _factory_as_the_engine_builds_it(
        strategy_config,
        instance_id="f" * 64,
        strategy_id="A" * 40 + "-" + "9" * 12,
    )
    for count in (LARGEST_POSITIVE_ORDER_COUNT, ORDER_COUNT_THAT_WRAPS):
        factory.set_client_order_id_count(count)

        generated = factory.generate_client_order_id().value

        assert len(generated) < BINANCE_CLIENT_ORDER_ID_LEN_LIMIT, (
            f"{generated!r} is {len(generated)} characters at the worst case; the id shape "
            "still depends on the counter or on a tag, so this is a ceiling, not a fix"
        )


def test_the_length_does_not_move_at_all_as_the_counter_grows(strategy_config) -> None:
    """The counter was the part that grew with trading volume, so pin that it is gone."""

    factory = _factory_as_the_engine_builds_it(strategy_config)

    lengths = set()
    for count in (0, 9, 99, 99_999, LARGEST_POSITIVE_ORDER_COUNT, ORDER_COUNT_THAT_WRAPS):
        factory.set_client_order_id_count(count)
        lengths.add(len(factory.generate_client_order_id().value))

    assert len(lengths) == 1, (
        f"id length varies with the order counter: {sorted(lengths)}. A fix that only "
        "shortens a tag moves the failure to a higher order count instead of removing it"
    )


def test_the_shape_is_decided_by_the_config_and_nothing_after_it(strategy_config) -> None:
    """Guards where the fix has to live.

    The engine reads both flags off the strategy when it registers, and they are
    ``cdef readonly``, so the host cannot override the shape at the venue boundary even
    though it is the venue that imposes the limit. If these ever became writable, the fix
    could move somewhere less visible than the config — this notices that.
    """

    strategy = Strategy(config=strategy_config)

    for attribute in ("use_uuid_client_order_ids", "use_hyphens_in_client_order_ids"):
        with pytest.raises(AttributeError):
            setattr(strategy, attribute, True)


def test_the_observed_rejection_is_reproducible_with_the_old_shape() -> None:
    """The default shape really does produce the 44 characters the venue refused.

    Keeps the bug itself on record. If this stops reproducing, the arithmetic behind the
    fix has changed and the fix's reasoning needs revisiting rather than trusting.
    """

    from custos.engines.nautilus.host import NtTradingNodeHost

    old_shape = OrderFactory(
        trader_id=TraderId(NtTradingNodeHost._trader_id(OBSERVED_INSTANCE_ID)),
        strategy_id=StrategyId("SuperTrendStrategy-000"),
        clock=TestClock(),
        cache=Cache(database=None),
        use_uuid_client_order_ids=False,
        use_hyphens_in_client_order_ids=True,
    )

    generated = old_shape.generate_client_order_id().value

    assert len(generated) == 44
    assert generated.endswith("-dcb00e520b45569e83b0-000-1")

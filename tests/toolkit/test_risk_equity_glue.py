# tests/test_risk_equity_glue.py
"""Branch coverage for EquityProvider.get_risk_equity glue (A2-risk).

Uses SimpleNamespace stubs + unbound method calls to avoid instantiating the
Cython Strategy base. The fail-safe *decision* lives in the pure
resolve_risk_equity (test_risk_equity.py); here we verify the glue wires
portfolio.equity()/missing_price_instruments() into it and degrades to free
balance on every unavailable path, including an unexpected exception
(the risk getter must never crash the callbacks it feeds).

The glue moved off NautilusTradingStrategy._get_risk_equity into
EquityProvider.get_risk_equity; the stub now stands in for an EquityProvider
instance with an injected ``_strategy`` plus its own get_actual_balance /
fallback_capital methods.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("msgspec")

from custos_toolkit_nautilus.adapter.coordinators import EquityProvider

FREE = Decimal("8000")
FALLBACK = Decimal("5000")  # fallback_capital() return; distinct from FREE
QUOTE = object()  # opaque currency sentinel; used as the equity-dict key


def _make_stub(
    *,
    pairs=("BTC-USDT",),
    instrument=...,
    equity_dict=None,
    missing=(),
    equity_raises=False,
    balance_raises=False,
):
    if instrument is ...:
        instrument = SimpleNamespace(quote_currency=QUOTE)
    instrument_id = SimpleNamespace(venue="BINANCE")
    cache = MagicMock()
    cache.instrument.return_value = instrument
    portfolio = MagicMock()
    if equity_raises:
        portfolio.equity.side_effect = RuntimeError("boom")
    else:
        portfolio.equity.return_value = equity_dict if equity_dict is not None else {}
    portfolio.missing_price_instruments.return_value = list(missing)

    strategy = SimpleNamespace(
        config=SimpleNamespace(trading=SimpleNamespace(pairs=list(pairs))),
        _contexts={},
        _derive_instrument_id_for_pair=lambda _p: instrument_id,
        cache=cache,
        portfolio=portfolio,
        log=MagicMock(),
    )

    def _balance():
        if balance_raises:
            raise RuntimeError("balance boom")
        return FREE

    stub = SimpleNamespace(
        _strategy=strategy,
        get_actual_balance=_balance,
        fallback_capital=lambda: FALLBACK,
        _last_good_risk_equity=None,
    )
    # Binds the real _primary_instrument_id by reading stub._strategy, so this covers the
    stub._primary_instrument_id = lambda: EquityProvider._primary_instrument_id(stub)
    return stub


def _call(stub) -> Decimal:
    return EquityProvider.get_risk_equity(stub)


def test_valid_equity_returned():
    money = SimpleNamespace(as_decimal=lambda: Decimal("9500.50"))
    stub = _make_stub(equity_dict={QUOTE: money}, missing=())
    assert _call(stub) == Decimal("9500.50")


def test_no_pairs_falls_back_to_free():
    stub = _make_stub(pairs=())
    assert _call(stub) == FREE
    stub._strategy.log.warning.assert_called_once()  # never silent


def test_instrument_none_falls_back_to_free():
    stub = _make_stub(instrument=None)
    assert _call(stub) == FREE
    stub._strategy.log.warning.assert_called_once()  # never silent


def test_balance_lookup_exception_falls_back_to_fallback_capital():
    # Even the free-balance lookup must not crash the risk getter.
    stub = _make_stub(balance_raises=True)
    assert _call(stub) == FALLBACK
    stub._strategy.log.warning.assert_called_once()


def test_understated_falls_back_to_free():
    money = SimpleNamespace(as_decimal=lambda: Decimal("9500"))
    stub = _make_stub(equity_dict={QUOTE: money}, missing=("BTC-USDT",))
    assert _call(stub) == FREE
    stub._strategy.log.warning.assert_called_once()  # never silent


def test_missing_quote_currency_falls_back_to_free():
    other = object()
    money = SimpleNamespace(as_decimal=lambda: Decimal("9500"))
    stub = _make_stub(equity_dict={other: money}, missing=())
    assert _call(stub) == FREE
    stub._strategy.log.warning.assert_called_once()  # never silent


def test_equity_lookup_exception_falls_back_to_free():
    stub = _make_stub(equity_raises=True)
    assert _call(stub) == FREE
    stub._strategy.log.warning.assert_called_once()


def test_last_good_mark_floors_understated_equity():
    """#5: a remembered reliable mark must floor a later understated tick so the
    optimistic free balance cannot relax the risk thresholds."""
    money = SimpleNamespace(as_decimal=lambda: Decimal("8000"))
    stub = _make_stub(equity_dict={QUOTE: money}, missing=())  # reliable mark 8000
    assert _call(stub) == Decimal("8000")  # remembered as last-good

    # Now the venue can't price the position (understated) and free is optimistic-high.
    stub._strategy.portfolio.missing_price_instruments.return_value = ["BTC-USDT"]
    stub.get_actual_balance = lambda: Decimal("10000")
    assert _call(stub) == Decimal("8000")  # conservative last-good, not optimistic 10000


# --- the risk-equity reliability flag that gates entries closed ---


def test_reliable_equity_sets_reliable_flag():
    money = SimpleNamespace(as_decimal=lambda: Decimal("9500"))
    stub = _make_stub(equity_dict={QUOTE: money}, missing=())
    _call(stub)
    assert EquityProvider.is_risk_equity_reliable(stub) is True


def test_understated_clears_reliable_flag():
    money = SimpleNamespace(as_decimal=lambda: Decimal("9500"))
    stub = _make_stub(equity_dict={QUOTE: money}, missing=("BTC-USDT",))  # understated
    _call(stub)
    assert EquityProvider.is_risk_equity_reliable(stub) is False


def test_free_fallback_paths_are_unreliable():
    # no pairs -> free fallback -> unreliable
    stub = _make_stub(pairs=())
    _call(stub)
    assert EquityProvider.is_risk_equity_reliable(stub) is False
    # equity lookup exception -> free fallback -> unreliable
    stub2 = _make_stub(equity_raises=True)
    _call(stub2)
    assert EquityProvider.is_risk_equity_reliable(stub2) is False

# tests/test_equity_provider.py
"""EquityProvider against a mock strategy.

The component reads the capital figures and was lifted off the strategy class. A
SimpleNamespace and MagicMock stand in for the injected strategy. These cover the
sizing figure (get_effective_capital: fixed_capital takes initial_capital, compound
takes the real balance), the risk figure (get_risk_equity: equity when healthy, a free
balance fallback with a warning when understated or raising) and the last-resort value.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit_nautilus.adapter.coordinators import EquityProvider  # noqa: E402

QUOTE = object()  # An opaque quote-currency sentinel, used as the balances/equity key


def _strategy(
    *,
    capital_mode="compound",
    initial_capital=5000,
    pairs=("BTC-USDT",),
    instrument=...,
    balances=None,
    equity_dict=None,
    missing=(),
):
    if instrument is ...:
        instrument = SimpleNamespace(quote_currency=QUOTE)
    instrument_id = SimpleNamespace(venue="BINANCE")
    account = MagicMock()
    account.balances.return_value = balances
    cache = MagicMock()
    cache.instrument.return_value = instrument
    portfolio = MagicMock()
    portfolio.account.return_value = account
    portfolio.equity.return_value = equity_dict if equity_dict is not None else {}
    portfolio.missing_price_instruments.return_value = list(missing)
    return SimpleNamespace(
        config=SimpleNamespace(
            position=SimpleNamespace(capital_mode=capital_mode, initial_capital=initial_capital),
            trading=SimpleNamespace(pairs=list(pairs)),
        ),
        _contexts={},
        _derive_instrument_id_for_pair=lambda _p: instrument_id,
        portfolio=portfolio,
        cache=cache,
        log=MagicMock(),
    )


def _money(value):
    return SimpleNamespace(free=SimpleNamespace(as_decimal=lambda: Decimal(value)))


# --- get_effective_capital ------------------------------------------------


def test_effective_capital_fixed_capital_returns_initial_capital():
    s = _strategy(capital_mode="fixed_capital", initial_capital=7000)
    assert EquityProvider(s).get_effective_capital() == Decimal("7000")


def test_effective_capital_compound_uses_actual_balance():
    s = _strategy(capital_mode="compound", balances={QUOTE: _money("8000")})
    assert EquityProvider(s).get_effective_capital() == Decimal("8000")


# --- fallback_capital ------------------------------------------------------


def test_fallback_capital_returns_config_initial_capital():
    s = _strategy(initial_capital=5000)
    assert EquityProvider(s).fallback_capital() == Decimal("5000")


# --- get_actual_balance ----------------------------------------------------


def test_actual_balance_uses_quote_currency():
    s = _strategy(balances={QUOTE: _money("8000")})
    assert EquityProvider(s).get_actual_balance() == Decimal("8000")


def test_actual_balance_no_pairs_returns_fallback():
    s = _strategy(pairs=(), initial_capital=5000)
    assert EquityProvider(s).get_actual_balance() == Decimal("5000")


def test_actual_balance_instrument_none_returns_fallback():
    s = _strategy(instrument=None, balances={QUOTE: _money("8000")}, initial_capital=5000)
    assert EquityProvider(s).get_actual_balance() == Decimal("5000")
    s.log.warning.assert_called()  # never silent


# --- get_risk_equity -------------------------------------------------------


def test_risk_equity_happy_returns_equity():
    eq_money = SimpleNamespace(as_decimal=lambda: Decimal("9500.50"))
    s = _strategy(balances={QUOTE: _money("8000")}, equity_dict={QUOTE: eq_money}, missing=())
    assert EquityProvider(s).get_risk_equity() == Decimal("9500.50")


def test_risk_equity_understated_falls_back_to_free():
    eq_money = SimpleNamespace(as_decimal=lambda: Decimal("9500"))
    s = _strategy(
        balances={QUOTE: _money("8000")}, equity_dict={QUOTE: eq_money}, missing=("BTC-USDT",)
    )
    assert EquityProvider(s).get_risk_equity() == Decimal("8000")
    s.log.warning.assert_called_once()  # never silent


def test_risk_equity_balance_exception_falls_back_to_fallback_capital():
    # The risk getter must survive the free-balance read raising, and fall back — never silently.
    s = _strategy(initial_capital=5000)
    s.portfolio.account.side_effect = RuntimeError("balance boom")
    assert EquityProvider(s).get_risk_equity() == Decimal("5000")
    s.log.warning.assert_called()

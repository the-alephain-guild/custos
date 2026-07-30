"""Tests for the toolkit's nautilus indicators."""

import pytest

pytest.importorskip("nautilus_trader")


def test_supertrend_importable_from_toolkit():
    """SuperTrend is importable from the toolkit's nautilus adapter."""
    from custos_toolkit_nautilus.adapter.indicators import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)
    assert indicator.length == 10
    assert indicator.multiplier == 3.0


def test_supertrend_has_snapshot_support():
    """The toolkit's SuperTrend supports snapshotting."""
    from custos_toolkit.warmup.protocol import SnapshotSupport
    from custos_toolkit_nautilus.adapter.indicators import SuperTrend

    indicator = SuperTrend(length=10, multiplier=3.0)
    assert isinstance(indicator, SnapshotSupport)


def test_supertrend_column_names_match_pandas_ta_default_multiplier():
    """The primary column-match path must hit exactly for an integer multiplier.

    pandas_ta names the column 'SUPERT_10_3.0' for multiplier=3.0; building
    'SUPERT_10_3' by stripping the trailing zero misses for every integer multiplier
    and leaves the fragile prefix fallback carrying every bar. The expected names come
    from a live run rather than a hand-built literal, so the builder is checked against
    the naming it has to agree with instead of against a second copy of its own logic.

    That run uses the vendored pandas_ta, because the adapter imports the vendored copy
    at runtime and the installed one is absent here by design. The vendored copy is
    therefore the naming this repository must actually agree with.
    """
    import pandas as pd
    from custos_toolkit_nautilus._vendor import pandas_ta as ta
    from custos_toolkit_nautilus.adapter.indicators.supertrend import _supertrend_column_names

    length, multiplier = 10, 3.0
    n = 40
    df = pd.DataFrame(
        {
            "high": [10.0 + i * 0.2 for i in range(n)],
            "low": [9.0 + i * 0.2 for i in range(n)],
            "close": [9.5 + i * 0.2 for i in range(n)],
        }
    )
    real = ta.supertrend(df["high"], df["low"], df["close"], length=length, multiplier=multiplier)
    st_col, dir_col, long_col, short_col = _supertrend_column_names(length, multiplier)
    for col in (st_col, dir_col, long_col, short_col):
        assert col in real.columns, f"{col} not in pandas_ta columns {list(real.columns)}"

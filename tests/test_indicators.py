import numpy as np
import pandas as pd
import pytest
from futures.indicators import rma, indicators, confirmed_pivots
from tests.fixtures import candles


def test_wilder_sma_seed_and_recursion():
    result = rma(pd.Series([1., 2., 3., 4., 5.]), 3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2:].tolist() == pytest.approx([2, 8/3, 31/9])


@pytest.mark.parametrize("series,expected", [(np.arange(1., 601.), 100), (np.arange(600., 0., -1), 0), (np.full(600, 100.), 50)])
def test_rsi_extremes(series, expected):
    assert indicators(candles(series, "15m")).rsi.iloc[-1] == expected


def test_relative_volume_excludes_signal():
    d = candles(np.linspace(50, 60, 600), "15m")
    d.loc[d.index[-1], "volume"] = 200
    assert indicators(d).relative_volume.iloc[-1] == 2


def test_vwap_resets_at_midnight_utc():
    d = candles(np.linspace(50, 60, 600), "15m")
    out = indicators(d)
    boundaries = out[out.open_time % 86400000 == 0]
    for _, row in boundaries.iterrows():
        assert row.vwap == pytest.approx((row.high + row.low + row.close) / 3)


def test_pivot_available_only_after_two_right_bars():
    d = pd.DataFrame({"high": [1, 2, 5, 2, 1], "low": [0, 0, 0, 0, 0]})
    assert confirmed_pivots(d.iloc[:4]) == []
    assert confirmed_pivots(d) == [5]


def test_future_candles_do_not_rewrite_past_indicator():
    d = candles(np.linspace(50, 60, 600), "15m")
    short, full = indicators(d.iloc[:550]), indicators(d)
    for col in ("ema50", "rsi", "atr", "adx", "vwap", "relative_volume"):
        assert short[col].iloc[-1] == pytest.approx(full[col].iloc[549])

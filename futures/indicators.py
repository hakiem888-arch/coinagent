"""Closed-candle indicators with explicit Wilder seeding and UTC VWAP."""
import numpy as np
import pandas as pd


def rma(series, length=14):
    values = np.asarray(series, dtype=float)
    output = np.full(len(values), np.nan)
    seed = []
    current = None
    for i, value in enumerate(values):
        if not np.isfinite(value):
            seed, current = [], None
            continue
        if current is None:
            seed.append(value)
            if len(seed) == length:
                current = float(np.mean(seed))
        else:
            current = ((length - 1) * current + value) / length
        if current is not None:
            output[i] = current
    return pd.Series(output, index=series.index)


def indicators(frame):
    d = frame.copy()
    for n in (20, 50, 200):
        d[f"ema{n}"] = d.close.ewm(span=n, adjust=False, min_periods=n).mean()
    delta = d.close.diff()
    gain, loss = rma(delta.clip(lower=0)), rma((-delta).clip(lower=0))
    ratio = gain / loss.replace(0, np.nan)
    d["rsi"] = 100 - 100 / (1 + ratio)
    d.loc[(loss == 0) & (gain > 0), "rsi"] = 100.0
    d.loc[(gain == 0) & (loss > 0), "rsi"] = 0.0
    d.loc[(gain == 0) & (loss == 0), "rsi"] = 50.0
    d["tr"] = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(),
                         (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = rma(d.tr)
    up, down = d.high.diff(), -d.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    plus.iloc[0] = minus.iloc[0] = np.nan
    plus_di = 100 * rma(plus) / d.atr.replace(0, np.nan)
    minus_di = 100 * rma(minus) / d.atr.replace(0, np.nan)
    denom = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / denom.replace(0, np.nan)
    dx = dx.where(denom != 0, 0.0)
    d["adx"] = rma(dx)
    d["relative_volume"] = d.volume / d.volume.shift(1).rolling(20).mean().replace(0, np.nan)
    session = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.floor("D")
    weighted = ((d.high + d.low + d.close) / 3) * d.volume
    d["vwap"] = weighted.groupby(session).cumsum() / d.volume.groupby(session).cumsum().replace(0, np.nan)
    return d


def confirmed_pivots(frame, lookback=100):
    """A pivot becomes available only after two right-hand candles close."""
    out = []
    highs, lows = frame.high.to_numpy(), frame.low.to_numpy()
    for i in range(max(2, len(frame) - lookback), len(frame) - 2):
        adjacent = [i - 2, i - 1, i + 1, i + 2]
        if highs[i] > max(highs[j] for j in adjacent):
            out.append(float(highs[i]))
        if lows[i] < min(lows[j] for j in adjacent):
            out.append(float(lows[i]))
    return out

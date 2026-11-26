"""Closed-candle indicators with explicit Wilder seeding, UTC VWAP, Bollinger Bands, Stoch RSI,
MACD (12,26,9), Donchian20, EMA9/20/21/50/200, +DI/-DI, Normalized EMA50 Slope, and Confirmed Swing Pivots.
"""
import math
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
    for n in (9, 20, 21, 50, 200):
        d[f"ema{n}"] = d.close.ewm(span=n, adjust=False, min_periods=n).mean()
    delta = d.close.diff()
    gain, loss = rma(delta.clip(lower=0)), rma((-delta).clip(lower=0))
    ratio = gain / loss.replace(0, np.nan)
    d["rsi"] = 100 - 100 / (1 + ratio)
    d.loc[(loss == 0) & (gain > 0), "rsi"] = 100.0
    d.loc[(gain == 0) & (loss > 0), "rsi"] = 0.0
    d.loc[(gain == 0) & (loss == 0), "rsi"] = 50.0
    d["tr"] = pd.concat(
        [
            d.high - d.low,
            (d.high - d.close.shift()).abs(),
            (d.low - d.close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["atr"] = rma(d.tr)
    up, down = d.high.diff(), -d.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    if len(d) > 0:
        plus.iloc[0] = minus.iloc[0] = np.nan
    plus_di = 100 * rma(plus) / d.atr.replace(0, np.nan)
    minus_di = 100 * rma(minus) / d.atr.replace(0, np.nan)
    d["plus_di"] = plus_di
    d["minus_di"] = minus_di
    denom = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / denom.replace(0, np.nan)
    dx = dx.where(denom != 0, 0.0)
    d["adx"] = rma(dx)

    # MACD (12, 26, 9)
    ema12 = d.close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = d.close.ewm(span=26, adjust=False, min_periods=26).mean()
    d["macd"] = ema12 - ema26
    d["macd_signal"] = d["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]

    # RVOL20: current closed candle volume divided by mean of preceding 20 completed candles (excluding current)
    d["relative_volume"] = d.volume / d.volume.shift(1).rolling(20, min_periods=20).mean().replace(0, np.nan)
    d["rvol20"] = d["relative_volume"]

    # Donchian20: highest high and lowest low of preceding 20 completed candles (excluding current candle)
    d["donchian20_high"] = d.high.shift(1).rolling(20, min_periods=20).max()
    d["donchian20_low"] = d.low.shift(1).rolling(20, min_periods=20).min()

    # Normalized EMA50 slope: (EMA50_now - EMA50_four_closed_bars_ago) / ATR14_now
    d["ema50_slope_norm"] = (d["ema50"] - d["ema50"].shift(4)) / d["atr"].replace(0, np.nan)

    # Session VWAP resetting at 00:00 UTC using HLC3 weighted by candle volume
    session = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.floor("D")
    weighted = ((d.high + d.low + d.close) / 3) * d.volume
    d["vwap"] = weighted.groupby(session).cumsum() / d.volume.groupby(session).cumsum().replace(0, np.nan)

    # Bollinger Bands (20, 2.0, population std ddof=0) + bb_width
    bb_mid = d.close.rolling(20, min_periods=20).mean()
    bb_std = d.close.rolling(20, min_periods=20).std(ddof=0)
    d["bb_mid"] = bb_mid
    d["bb_upper"] = bb_mid + 2.0 * bb_std
    d["bb_lower"] = bb_mid - 2.0 * bb_std
    d["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["bb_mid"].replace(0, np.nan)

    # Stochastic RSI (14, 14, 3, 3): never default missing/non-finite RSI to a favorable neutral value
    rsi_min = d["rsi"].rolling(14, min_periods=14).min()
    rsi_max = d["rsi"].rolling(14, min_periods=14).max()
    rsi_range = (rsi_max - rsi_min).replace(0, np.nan)
    stoch_raw = (d["rsi"] - rsi_min) / rsi_range * 100.0
    d["stoch_rsi_k"] = stoch_raw.rolling(3, min_periods=3).mean()
    d["stoch_rsi_d"] = d["stoch_rsi_k"].rolling(3, min_periods=3).mean()
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


def confirmed_swing_pivots(frame, lookback=100, left=2, right=2):
    """Return structured confirmed swing high/low pivots (requires `left` closed bars left and `right` closed bars right).
    No pivot at index `i` is ever visible before bar `i + right` has closed (zero lookahead / zero backdating).
    """
    out = []
    if frame is None or len(frame) < left + right + 1:
        return out
    highs = frame.high.to_numpy(dtype=float)
    lows = frame.low.to_numpy(dtype=float)
    open_times = frame.open_time.to_numpy(dtype=np.int64) if "open_time" in frame.columns else np.arange(len(frame))
    close_times = frame.close_time.to_numpy(dtype=np.int64) if "close_time" in frame.columns else np.arange(len(frame))
    rsi_vals = frame["rsi"].to_numpy(dtype=float) if "rsi" in frame.columns else np.full(len(frame), np.nan)

    start_i = max(left, len(frame) - int(lookback))
    end_i = len(frame) - right
    for i in range(start_i, end_i):
        neighbors = [j for j in range(i - left, i + right + 1) if j != i]
        hi_val = highs[i]
        lo_val = lows[i]
        rsi_v = float(rsi_vals[i]) if math.isfinite(rsi_vals[i]) else float("nan")
        if math.isfinite(hi_val) and all(math.isfinite(highs[j]) and hi_val > highs[j] for j in neighbors):
            out.append(
                {
                    "index": int(i),
                    "confirmed_at_index": int(i + right),
                    "open_time": int(open_times[i]),
                    "close_time": int(close_times[i]),
                    "confirmed_close_time": int(close_times[i + right]),
                    "kind": "HIGH",
                    "price": float(hi_val),
                    "rsi": rsi_v,
                }
            )
        if math.isfinite(lo_val) and all(math.isfinite(lows[j]) and lo_val < lows[j] for j in neighbors):
            out.append(
                {
                    "index": int(i),
                    "confirmed_at_index": int(i + right),
                    "open_time": int(open_times[i]),
                    "close_time": int(close_times[i]),
                    "confirmed_close_time": int(close_times[i + right]),
                    "kind": "LOW",
                    "price": float(lo_val),
                    "rsi": rsi_v,
                }
            )
    return out

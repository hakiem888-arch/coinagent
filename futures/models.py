"""Minimal market data constants and OHLCV validation for CoinAgentX v7."""
import pandas as pd

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def validate_ohlcv_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate OHLCV integrity, drop invalid rows, deduplicate by open_time, and sort ascending."""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df = frame.copy()
    req = ("open_time", "open", "high", "low", "close", "volume", "close_time")
    for col in req:
        if col not in df.columns:
            raise ValueError(f"Kolom OHLCV wajib tidak ditemukan: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=list(req))
    valid_mask = (
        (df["open"] > 0)
        & (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["close_time"] > df["open_time"])
    )
    df = df.loc[valid_mask].copy()
    if df.empty:
        return df.reset_index(drop=True)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    return df.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)

"""Chronological Historical M1/M5/M15 Replay, Walk-Forward Evaluation, and Threshold Calibration Engine (v6.0.5 Stage 5).

Strict Anti-Lookahead & Integrity Rules:
1. Accepts ONLY authentic historical Futures OHLCV datasets (`instrument_kind` in USD_M_PERPETUAL / BYBIT_LINEAR_PERPETUAL).
   Rejects SPOT_REFERENCE datasets as Futures performance proof.
2. Replays strictly chronologically candle-by-candle on M1, aggregating running M5/M15 intrabar states
   solely from M1 candles with `close_time <= replay_ts_ms`, and official closed M15/M5 candles strictly
   with `close_time < replay_ts_ms` (zero future leakage).
3. Does NOT claim intra-candle tick order when given M1 OHLCV bars (explicitly disclosed in report metadata).
4. Splits data strictly chronologically: 60% train/calibration, 20% validation, 20% out-of-sample test (no random shuffle)
   plus expanding walk-forward folds.
5. Never presents synthetic test fixtures as production performance proof.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .intrabar_stream import validate_ohlcv_dataframe
from .models import INTERVAL_MS, IntrabarCandle, MonitorSnapshot
from .predictive_watchlist import (
    PredictiveConfig,
    PredictiveLifecycleManager,
    STAGE_CONFIRMED,
    STAGE_DIRECTION_CHANGED,
    STAGE_EARLY_WATCH,
    STAGE_MISSED_ENTRY,
    STAGE_PRE_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_SIGNAL_INVALIDATED,
    evaluate_early_signal,
)


def aggregate_ohlcv_timeframe(m1_df: pd.DataFrame, target_tf: str, now_ms: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronologically aggregate M1 candles available at `now_ms` into (closed_df, live_df) for `target_tf`.
    Strictly filters `open_time <= now_ms` and marks a bar closed ONLY when `bucket_close_time < now_ms`.
    """
    step = INTERVAL_MS[target_tf]
    avail = m1_df[m1_df["open_time"] <= int(now_ms)].copy()
    if avail.empty:
        empty = pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "is_closed"])
        return empty, empty
    avail["bucket"] = (avail["open_time"] // step) * step
    grouped = avail.groupby("bucket", sort=True)
    rows = []
    for bucket_open, g in grouped:
        b_open = int(bucket_open)
        b_close = b_open + step - 1
        is_closed = bool(b_close < int(now_ms))
        rows.append(
            {
                "open_time": b_open,
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
                "volume": float(g["volume"].sum()),
                "close_time": b_close,
                "is_closed": is_closed,
            }
        )
    live_df = pd.DataFrame(rows)
    closed_df = live_df[live_df["is_closed"]].reset_index(drop=True)
    return closed_df, live_df


def chronological_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Strict chronological 60% train / 20% validation / 20% out-of-sample test split (NO random shuffle)."""
    ordered = df.sort_values("open_time").reset_index(drop=True)
    n = len(ordered)
    i_train = int(n * 0.60)
    i_val = int(n * 0.80)
    return ordered.iloc[:i_train].reset_index(drop=True), ordered.iloc[i_train:i_val].reset_index(drop=True), ordered.iloc[i_val:].reset_index(drop=True)


def walk_forward_windows(df: pd.DataFrame, n_folds: int = 3) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Expanding chronological walk-forward folds (train -> out-of-sample test)."""
    ordered = df.sort_values("open_time").reset_index(drop=True)
    n = len(ordered)
    if n < 60 or n_folds < 1:
        return []
    fold_size = max(15, n // (n_folds + 2))
    folds = []
    for k in range(n_folds):
        train_end = fold_size * (k + 2)
        test_end = min(n, train_end + fold_size)
        if train_end >= n or test_end <= train_end:
            break
        folds.append((ordered.iloc[:train_end].reset_index(drop=True), ordered.iloc[train_end:test_end].reset_index(drop=True)))
    return folds


def load_historical_futures_csv(path: Path | str, *, instrument_kind: str = "USD_M_PERPETUAL") -> pd.DataFrame:
    """Load and validate an authentic historical Futures M1 OHLCV CSV file."""
    if instrument_kind == "SPOT_REFERENCE":
        raise ValueError("Spot Reference dataset dilarang digunakan sebagai pengganti backtest Futures.")
    df = pd.read_csv(path)
    return validate_ohlcv_dataframe(df)


def replay_predictive_m1_series(
    m1_df: pd.DataFrame,
    *,
    symbol: str = "BTCUSDT",
    provider: str = "Binance Futures",
    instrument_kind: str = "USD_M_PERPETUAL",
    config: Optional[PredictiveConfig] = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 2.0,
    funding_rate_per_trade: float = 0.0003,
    horizon_bars: int = 15,
    step_bars: int = 1,
    is_synthetic_fixture: bool = False,
) -> dict[str, Any]:
    """Replay an M1 Futures OHLCV series chronologically with zero lookahead and compute all Stage 5 metrics."""
    if instrument_kind == "SPOT_REFERENCE":
        raise ValueError("Spot data tidak boleh digunakan untuk evaluasi replay Futures.")
    cfg = config or PredictiveConfig()
    df = validate_ohlcv_dataframe(m1_df)
    if len(df) < 60:
        return {
            "ok": False,
            "error": "Minimal 60 bar M1 dibutuhkan untuk replay kronologis.",
            "is_synthetic_fixture": bool(is_synthetic_fixture),
        }

    mgr = PredictiveLifecycleManager(cfg)
    active_sig = None
    recorded_signals: list[dict[str, Any]] = []
    stage_buckets: dict[str, list[dict[str, Any]]] = {
        STAGE_PRE_WATCH: [],
        STAGE_EARLY_WATCH: [],
        STAGE_SETUP_FORMING: [],
        STAGE_CONFIRMED: [],
    }
    invalidation_count = 0
    direction_reversal_count = 0
    missed_entry_count = 0
    confirmed_count = 0
    equity = 1000.0
    peak_equity = equity
    max_dd_pct = 0.0
    trade_pnls: list[float] = []
    total_fees = 0.0
    total_slippage = 0.0
    total_funding = 0.0

    # Start after sufficient warmup so M1/M5/M15 features are strictly causal
    start_idx = min(max(30, len(df) // 4), len(df) - 5)
    for idx in range(start_idx, len(df), max(1, int(step_bars))):
        row = df.iloc[idx]
        now_ms = int(row["close_time"])
        px = float(row["close"])

        m1_slice = df.iloc[: idx + 1].copy()
        m1_slice["is_closed"] = m1_slice["close_time"] < now_ms
        m5_closed, m5_live = aggregate_ohlcv_timeframe(m1_slice, "5m", now_ms)
        m15_closed, m15_live = aggregate_ohlcv_timeframe(m1_slice, "15m", now_ms)

        snap = MonitorSnapshot(
            symbol=symbol,
            provider=provider,
            instrument_kind=instrument_kind,
            exchange_timestamp_ms=now_ms,
            local_receive_ms=now_ms,
            last_price=px,
            mark_price=px,
            index_price=px,
            bid=px * 0.9999,
            ask=px * 1.0001,
            frames_closed={"1m": m1_slice[m1_slice["is_closed"]], "5m": m5_closed, "15m": m15_closed},
            frames_live={"1m": m1_slice, "5m": m5_live, "15m": m15_live},
            data_freshness_ms=0,
            is_stale=False,
            transport="REPLAY_OHLCV_M1",
        )
        cand = evaluate_early_signal(snap, now_ms=now_ms, config=cfg)
        tr = mgr.process_early_update(active_sig, cand, current_price=px, now_ms=now_ms)
        if tr is None:
            continue

        for ev in tr.events:
            et = ev["event_type"]
            if et == STAGE_SIGNAL_INVALIDATED:
                invalidation_count += 1
            elif et == STAGE_DIRECTION_CHANGED:
                direction_reversal_count += 1
            elif et == STAGE_MISSED_ENTRY:
                missed_entry_count += 1

        sig = tr.signal
        active_sig = sig if sig.signal_state == "ACTIVE" else None

        # Measure forward excursion strictly for post-hoc evaluation (NEVER passed to signal engine)
        if tr.events and sig.stage in stage_buckets:
            fut = df.iloc[idx + 1 : min(len(df), idx + 1 + horizon_bars)]
            if not fut.empty and px > 0:
                if sig.direction == "LONG":
                    mfe_pct = max(0.0, (float(fut["high"].max()) - px) / px * 100.0)
                    mae_pct = max(0.0, (px - float(fut["low"].min())) / px * 100.0)
                    hit_trig = fut[fut["high"] >= sig.trigger_price]
                else:
                    mfe_pct = max(0.0, (px - float(fut["low"].min())) / px * 100.0)
                    mae_pct = max(0.0, (float(fut["high"].max()) - px) / px * 100.0)
                    hit_trig = fut[fut["low"] <= sig.trigger_price]
                lead_s = (int(hit_trig.iloc[0]["open_time"]) - now_ms) / 1000.0 if not hit_trig.empty else None
                end_px = float(fut["close"].iloc[-1])
                raw_ret = ((end_px - px) / px) if sig.direction == "LONG" else ((px - end_px) / px)
                notional = equity * 0.25
                fee_cost = notional * fee_rate * 2.0
                slip_cost = notional * (slippage_bps / 10000.0) * 2.0
                fund_cost = notional * funding_rate_per_trade
                net_pnl = notional * raw_ret - fee_cost - slip_cost - fund_cost
                rec = {
                    "signal_id": sig.signal_id,
                    "stage": sig.stage,
                    "direction": sig.direction,
                    "early_score": sig.early_score,
                    "at_ms": now_ms,
                    "lead_time_seconds": max(0.0, lead_s) if lead_s is not None else None,
                    "favorable_excursion_pct": round(mfe_pct, 4),
                    "adverse_excursion_pct": round(mae_pct, 4),
                    "reached_trigger": not hit_trig.empty,
                    "net_paper_pnl": round(net_pnl, 4),
                }
                stage_buckets[sig.stage].append(rec)
                recorded_signals.append(rec)
                if sig.stage == STAGE_SETUP_FORMING:
                    trade_pnls.append(net_pnl)
                    total_fees += fee_cost
                    total_slippage += slip_cost
                    total_funding += fund_cost
                    equity += net_pnl
                    peak_equity = max(peak_equity, equity)
                    dd = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
                    max_dd_pct = max(max_dd_pct, dd)

    def _summarize_bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {
                "count": 0,
                "reached_trigger_rate": 0.0,
                "false_alert_rate": 0.0,
                "avg_lead_time_seconds": 0.0,
                "avg_favorable_excursion_pct": 0.0,
                "avg_adverse_excursion_pct": 0.0,
            }
        leads = [x["lead_time_seconds"] for x in items if x["lead_time_seconds"] is not None]
        reached = sum(1 for x in items if x["reached_trigger"])
        false_alerts = len(items) - reached
        return {
            "count": len(items),
            "reached_trigger_rate": round(reached / len(items), 4),
            "false_alert_rate": round(false_alerts / len(items), 4),
            "avg_lead_time_seconds": round(float(np.mean(leads)), 2) if leads else 0.0,
            "avg_favorable_excursion_pct": round(float(np.mean([x["favorable_excursion_pct"] for x in items])), 4),
            "avg_adverse_excursion_pct": round(float(np.mean([x["adverse_excursion_pct"] for x in items])), 4),
        }

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    total_early = len(recorded_signals)

    return {
        "ok": True,
        "symbol": symbol,
        "provider": provider,
        "instrument_kind": instrument_kind,
        "granularity_note": "M1 OHLCV chronological replay only; intra-bar tick ordering is NOT assumed.",
        "is_synthetic_fixture": bool(is_synthetic_fixture),
        "bars_replayed": int(len(df) - start_idx),
        "date_range_ms": [int(df["open_time"].iloc[0]), int(df["close_time"].iloc[-1])],
        "total_signals": total_early,
        "early_to_confirmed_rate": round(confirmed_count / total_early, 4) if total_early else 0.0,
        "invalidation_rate": round(invalidation_count / total_early, 4) if total_early else 0.0,
        "direction_reversal_rate": round(direction_reversal_count / total_early, 4) if total_early else 0.0,
        "missed_entry_rate": round(missed_entry_count / total_early, 4) if total_early else 0.0,
        "by_stage": {st: _summarize_bucket(items) for st, items in stage_buckets.items()},
        "paper_metrics": {
            "trades": len(trade_pnls),
            "net_paper_pnl": round(sum(trade_pnls), 4),
            "win_rate": round(len(wins) / len(trade_pnls), 4) if trade_pnls else 0.0,
            "expectancy": round(sum(trade_pnls) / len(trade_pnls), 4) if trade_pnls else 0.0,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "total_fees": round(total_fees, 4),
            "total_slippage": round(total_slippage, 4),
            "total_funding": round(total_funding, 4),
        },
    }


def calibrate_and_evaluate_walk_forward(
    m1_df: pd.DataFrame,
    *,
    symbol: str = "BTCUSDT",
    provider: str = "Binance Futures",
    instrument_kind: str = "USD_M_PERPETUAL",
    candidate_pre_thresholds: tuple[float, ...] = (48.0, 50.0, 52.0),
    is_synthetic_fixture: bool = False,
) -> dict[str, Any]:
    """Run strict chronological 60/20/20 split + validation calibration + out-of-sample & walk-forward test."""
    train_df, val_df, test_df = chronological_split(m1_df)
    best_th = 50.0
    best_score = -1e9
    val_trials = []
    for th in candidate_pre_thresholds:
        cfg = PredictiveConfig(pre_watch_threshold=th)
        res = replay_predictive_m1_series(
            val_df,
            symbol=symbol,
            provider=provider,
            instrument_kind=instrument_kind,
            config=cfg,
            step_bars=3,
            is_synthetic_fixture=is_synthetic_fixture,
        )
        pw_rate = res.get("by_stage", {}).get(STAGE_PRE_WATCH, {}).get("reached_trigger_rate", 0.0)
        sf_rate = res.get("by_stage", {}).get(STAGE_SETUP_FORMING, {}).get("reached_trigger_rate", 0.0)
        obj = pw_rate * 0.4 + sf_rate * 0.6 - res.get("invalidation_rate", 0.0) * 0.5
        val_trials.append({"pre_watch_threshold": th, "objective": round(obj, 4), "summary": res})
        if obj > best_score:
            best_score = obj
            best_th = th

    oos_cfg = PredictiveConfig(pre_watch_threshold=best_th)
    oos_result = replay_predictive_m1_series(
        test_df,
        symbol=symbol,
        provider=provider,
        instrument_kind=instrument_kind,
        config=oos_cfg,
        step_bars=2,
        is_synthetic_fixture=is_synthetic_fixture,
    )
    wf_results = []
    for f_idx, (_tr, _te) in enumerate(walk_forward_windows(m1_df, n_folds=3), start=1):
        wf_results.append(
            {
                "fold": f_idx,
                "test_bars": len(_te),
                "result": replay_predictive_m1_series(
                    _te,
                    symbol=symbol,
                    provider=provider,
                    instrument_kind=instrument_kind,
                    config=oos_cfg,
                    step_bars=3,
                    is_synthetic_fixture=is_synthetic_fixture,
                ),
            }
        )
    return {
        "split_sizes": {"train_60pct": len(train_df), "validation_20pct": len(val_df), "test_oos_20pct": len(test_df)},
        "calibrated_pre_watch_threshold": best_th,
        "validation_trials": val_trials,
        "out_of_sample_result": oos_result,
        "walk_forward_folds": wf_results,
        "is_synthetic_fixture": bool(is_synthetic_fixture),
        "production_proof_valid": not bool(is_synthetic_fixture),
    }

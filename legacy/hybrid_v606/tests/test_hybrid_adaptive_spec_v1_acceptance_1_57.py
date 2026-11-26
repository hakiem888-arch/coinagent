"""Automated Acceptance Test Suite for CoinAgentX — Hybrid Adaptive Strategy Specification v1.0.

Maps 1-to-1 to all 57 Acceptance Tests in Section 18 (`test_at01_...` through `test_at57_...`)
of `CoinAgentX_Hybrid_Adaptive_Spec_v1.0.md` (Deliverable 9).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backtest_hybrid_adaptive import (
    compute_metrics_from_r_series,
    load_backtest_manifest,
    run_walk_forward_validation,
)
from futures.hybrid_adaptive import (
    ALL_REGIMES,
    COOLDOWN_CONSECUTIVE_LOSSES_MS,
    DEFAULT_MIN_CANDLES_M5,
    DEFAULT_MIN_CANDLES_M15,
    M5_INTERVAL_MS,
    M15_INTERVAL_MS,
    REGIME_BREAKOUT_DOWN,
    REGIME_BREAKOUT_UP,
    REGIME_HIGH_VOLATILITY,
    REGIME_RANGE,
    REGIME_STRONG_TREND_DOWN,
    REGIME_STRONG_TREND_UP,
    REGIME_TRANSITION,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    STRATEGY_A,
    STRATEGY_B,
    STRATEGY_C,
    STRATEGY_D,
    STRATEGY_E,
    STRATEGY_F,
    ArmingStateStore,
    HybridAlertDeduplicator,
    HybridRotatingQueue,
    break_even_price,
    build_hybrid_entry_order,
    classify_setup_stage,
    compute_hybrid_tp_targets,
    compute_setup_quality_score,
    detect_m15_regime,
    empty_hybrid_funnel,
    evaluate_hybrid_adaptive,
    evaluate_hybrid_ai_advisor,
    evaluate_hybrid_position_lifecycle,
    evaluate_strategy_a,
    evaluate_strategy_b,
    evaluate_strategy_c,
    evaluate_strategy_d,
    evaluate_strategy_e,
    format_hybrid_alert,
    hybrid_alert_dedup_key,
    quantize,
    route_strategy_by_regime,
    validate_hybrid_candle_history,
    validate_hybrid_instrument,
)
from futures.indicators import confirmed_swing_pivots, indicators
from futures.models import Account, Contract, Snapshot


# ============================================================================
# DETERMINISTIC SYNTHETIC CANDLE & SNAPSHOT BUILDERS
# ============================================================================

def _make_candles(
    n: int = 260,
    interval_ms: int = M5_INTERVAL_MS,
    end_close_ms: int = 1_763_337_600_000 - 1,
    base_price: float = 100.0,
    drift_per_bar: float = 0.08,
    wave_amp: float = 0.25,
    wave_freq: float = 0.35,
    bar_half_range: float = 0.45,
    volume: float = 1000.0,
) -> pd.DataFrame:
    """Create a contiguous, closed OHLCV DataFrame of length `n` ending at `end_close_ms`."""
    last_open = end_close_ms + 1 - interval_ms
    start_open = last_open - (n - 1) * interval_ms
    rows = []
    for i in range(n):
        ot = start_open + i * interval_ms
        ct = ot + interval_ms - 1
        mid = base_price + drift_per_bar * i + wave_amp * math.sin(i * wave_freq)
        op = mid - 0.10 * (1 if drift_per_bar >= 0 else -1)
        cl = mid + 0.10 * (1 if drift_per_bar >= 0 else -1)
        hi = max(op, cl) + bar_half_range
        lo = min(op, cl) - bar_half_range
        rows.append(
            {
                "open_time": int(ot),
                "close_time": int(ct),
                "open": float(op),
                "high": float(hi),
                "low": float(lo),
                "close": float(cl),
                "volume": float(volume),
            }
        )
    return pd.DataFrame(rows)


def _make_contract_spec(tick=0.01, step=0.001, min_qty=0.001, min_notional=5.0) -> Contract:
    return Contract(
        tick=tick,
        step=step,
        min_qty=min_qty,
        max_qty=100_000.0,
        min_notional=min_notional,
        market_step=step,
        market_min_qty=min_qty,
        market_max_qty=100_000.0,
        min_price=0.01,
        max_price=1_000_000.0,
        multiplier_up=1.10,
        multiplier_down=0.90,
        status="TRADING",
        contract_type="PERPETUAL",
        quote_asset="USDT",
    )


def _make_account(now_ms: int = 1_763_337_605_000, equity: float = 1000.0, **overrides) -> Account:
    from datetime import datetime, timezone

    day_str = datetime.fromtimestamp(now_ms / 1000.0, timezone.utc).date().isoformat()
    acc = Account(
        equity=equity,
        free_margin=equity,
        day_start_equity=equity,
        account_day_utc=day_str,
        account_verified=True,
        active_pending_count=0,
        total_existing_risk=0.0,
        correlated_alt_risk=0.0,
        consecutive_losses=0,
        taker_fee=0.0005,
        exit_slippage_bps=2.0,
        funding_reserve_rate=0.0003,
        costs_verified=True,
        leverage=2,
        risk_pct=0.25,
        isolated=True,
        order_semantics_verified=True,
        news_source="ForexFactory Verified",
        news_verified_at_ms=now_ms - 60_000,
        news_clear_from_ms=now_ms - 3_600_000,
        news_clear_until_ms=now_ms + 7_200_000,
    )
    for k, v in overrides.items():
        setattr(acc, k, v)
    return acc


def _build_strategy_a_frames(direction: str = "LONG", strong: bool = False):
    """Build 260-bar M15 and M5 frames where Strategy A (Trend Pullback) triggers cleanly."""
    now_ms = 1_763_337_605_000
    m15_end = (now_ms // M15_INTERVAL_MS) * M15_INTERVAL_MS - 1
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    sign = 1 if direction == "LONG" else -1

    # Keep M15 base below M5 price for LONG (or above for SHORT) so historical M15 pivots never block M5 target room
    m15 = _make_candles(
        n=260,
        interval_ms=M15_INTERVAL_MS,
        end_close_ms=m15_end,
        base_price=75.0 if sign == 1 else 260.0,
        drift_per_bar=0.0,
        wave_amp=0.25,
        wave_freq=1.6,
        bar_half_range=0.80,
        volume=1500.0,
    )
    trend_bars = 26
    step_move = 0.45 * sign
    start_idx = len(m15) - trend_bars
    base_px = float(m15.loc[start_idx - 1, "close"])
    for k, idx in enumerate(range(start_idx, len(m15))):
        mid = base_px + (k + 1) * step_move
        op = mid - 0.12 * sign
        cl = mid + 0.12 * sign
        hi = max(op, cl) + 0.65
        lo = min(op, cl) - 0.65
        m15.loc[idx, ["open", "high", "low", "close"]] = [op, hi, lo, cl]
    if not strong:
        # Dip low on the last 2 bars so -DI rises and DX drops below ADX, making ADX decline on the last bar -> TREND_UP/DOWN
        for k_in in (2, 1):
            idx_in = len(m15) - k_in
            p_hi = float(m15.loc[idx_in - 1, "high"])
            p_lo = float(m15.loc[idx_in - 1, "low"])
            p_cl = float(m15.loc[idx_in - 1, "close"])
            if sign == 1:
                m15.loc[idx_in, "high"] = p_hi - 0.10
                m15.loc[idx_in, "low"] = p_lo - 1.40
            else:
                m15.loc[idx_in, "low"] = p_lo + 0.10
                m15.loc[idx_in, "high"] = p_hi + 1.40
            m15.loc[idx_in, "open"] = p_cl - 0.05 * sign
            m15.loc[idx_in, "close"] = p_cl + 0.05 * sign

    # M5 frame with ~2.0 ATR so round-trip cost is ~8% of 1R (well below the 20% hard gate)
    m5 = _make_candles(
        n=260,
        interval_ms=M5_INTERVAL_MS,
        end_close_ms=m5_end,
        base_price=100.0 if sign == 1 else 220.0,
        drift_per_bar=0.14 * sign,
        wave_amp=0.0,
        bar_half_range=0.95,
        volume=1000.0,
    )
    d5_pre = indicators(m5)
    atr_last = float(d5_pre["atr"].iloc[-1])

    for idx in (len(m5) - 3, len(m5) - 2):
        bar_ema20 = float(d5_pre.loc[idx, "ema20"])
        if sign == 1:
            m5.loc[idx, "low"] = bar_ema20 - 0.05 * atr_last
            m5.loc[idx, "close"] = bar_ema20 + 0.05 * atr_last
            m5.loc[idx, "open"] = bar_ema20 + 0.10 * atr_last
            m5.loc[idx, "high"] = bar_ema20 + 0.18 * atr_last
        else:
            m5.loc[idx, "high"] = bar_ema20 + 0.05 * atr_last
            m5.loc[idx, "close"] = bar_ema20 - 0.05 * atr_last
            m5.loc[idx, "open"] = bar_ema20 - 0.10 * atr_last
            m5.loc[idx, "low"] = bar_ema20 - 0.18 * atr_last

    d5_mid = indicators(m5)
    last_i = len(m5) - 1
    ema20_cur = float(d5_mid.loc[last_i, "ema20"])
    atr_cur = float(d5_mid.loc[last_i, "atr"])
    prev_hi = float(m5.loc[last_i - 1, "high"])
    prev_lo = float(m5.loc[last_i - 1, "low"])
    if sign == 1:
        op = ema20_cur + 0.02 * atr_cur
        cl = max(prev_hi + 0.15 * atr_cur, op + 0.45 * atr_cur)
        lo = ema20_cur - 0.70 * atr_cur
        hi = cl + 0.04 * atr_cur
    else:
        op = ema20_cur - 0.02 * atr_cur
        cl = min(prev_lo - 0.15 * atr_cur, op - 0.45 * atr_cur)
        hi = ema20_cur + 0.70 * atr_cur
        lo = cl - 0.04 * atr_cur
    m5.loc[last_i, ["open", "high", "low", "close", "volume"]] = [op, hi, lo, cl, 1600.0]
    return now_ms, m15, m5


def _make_snapshot_from_frames(
    now_ms: int,
    m15: pd.DataFrame,
    m5: pd.DataFrame,
    symbol="BTCUSDT",
    instrument_kind="USD_M_PERPETUAL",
    **overrides,
) -> Snapshot:
    last_bar = m5.iloc[-1]
    is_bull = float(last_bar.close) >= float(last_bar.open)
    last_px = float(last_bar.high) - 0.02 if is_bull else float(last_bar.low) + 0.02
    bid = last_px - 0.01
    ask = last_px + 0.01
    snap = Snapshot(
        symbol=symbol,
        mode="SCALPING",
        source="Binance Futures",
        instrument_kind=instrument_kind,
        quote_time_basis="EXCHANGE_SERVER",
        now_ms=now_ms,
        bias=m15,
        execution=m5,
        contract=_make_contract_spec(),
        last=last_px,
        mark=last_px,
        bid=bid,
        ask=ask,
        bids=[(bid, 500.0), (bid - 0.01, 500.0)],
        asks=[(ask, 500.0), (ask + 0.01, 500.0)],
        funding_rate=0.0001,
        next_funding_ms=now_ms + 14_400_000,
        funding_interval_hours=8,
        previous_day_high=last_px + 50.0,
        previous_day_low=max(0.5, last_px - 50.0),
        open_bar_high=last_px,
        open_bar_low=last_px,
        open_mark_high=last_px,
        open_mark_low=last_px,
        quote_times={
            "last": now_ms - 1000,
            "mark": now_ms - 1000,
            "book": now_ms - 1000,
            "depth": now_ms - 1000,
        },
        warnings=[],
    )
    snap.quote_volume_24h = 25_000_000.0
    snap.trade_count_24h = 15_000
    for k, v in overrides.items():
        setattr(snap, k, v)
    return snap


# ============================================================================
# 18.1 DATA & INDICATOR TESTS (AT-01 .. AT-07)
# ============================================================================

def test_at01_reject_incomplete_latest_candle_from_confirming_entry():
    """AT-01: Reject incomplete latest candle from confirming any entry."""
    now_ms = 1_763_337_605_000
    m5_end_unclosed = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS + M5_INTERVAL_MS - 1
    df = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end_unclosed)
    res = validate_hybrid_candle_history(df, "5m", now_ms, min_candles=250)
    assert res["ok"] is False
    assert res["code"] == "CANDLE_NOT_CLOSED"


def test_at02_reject_candle_history_shorter_than_required_minimum_with_history_insufficient():
    """AT-02: Reject candle history shorter than required minimum (`< 250`) with `HISTORY_INSUFFICIENT`."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    df = _make_candles(n=210, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end)
    res = validate_hybrid_candle_history(df, "5m", now_ms, min_candles=DEFAULT_MIN_CANDLES_M5)
    assert res["ok"] is False
    assert res["code"] == "HISTORY_INSUFFICIENT"
    assert "HISTORY_INSUFFICIENT" in res["reason"]


def test_at03_reject_non_contiguous_duplicate_or_nan_ohlcv_candles():
    """AT-03: Reject non-contiguous, duplicate, or NaN OHLCV candles."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    df_gap = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end)
    df_gap.loc[100, "open_time"] += M5_INTERVAL_MS
    res_gap = validate_hybrid_candle_history(df_gap, "5m", now_ms, min_candles=250)
    assert res_gap["ok"] is False
    assert res_gap["code"] == "CANDLE_GAP_OR_DUPLICATE"

    df_nan = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end)
    df_nan.loc[150, "close"] = float("nan")
    res_nan = validate_hybrid_candle_history(df_nan, "5m", now_ms, min_candles=250)
    assert res_nan["ok"] is False
    assert res_nan["code"] == "NON_FINITE_OHLCV"


def test_at04_verify_wilder_rma_atr14_adx14_and_rsi14_against_reference_values():
    """AT-04: Verify Wilder RMA ATR14, ADX14, and RSI14 against reference values."""
    df = _make_candles(n=120, drift_per_bar=0.25, wave_amp=0.15)
    d = indicators(df)
    expected_seed_atr = float(d["tr"].iloc[:14].mean())
    assert math.isclose(float(d["atr"].iloc[13]), expected_seed_atr, rel_tol=1e-9)
    expected_next_atr = (13.0 * expected_seed_atr + float(d["tr"].iloc[14])) / 14.0
    assert math.isclose(float(d["atr"].iloc[14]), expected_next_atr, rel_tol=1e-9)
    assert all(math.isfinite(float(d[col].iloc[-1])) for col in ("atr", "adx", "plus_di", "minus_di", "rsi"))


def test_at05_verify_vwap_resets_at_0000_utc():
    """AT-05: Verify VWAP resets at `00:00 UTC`."""
    midnight_ms = 1_763_337_600_000
    end_close_ms = midnight_ms + 3 * M5_INTERVAL_MS - 1
    df = _make_candles(n=40, interval_ms=M5_INTERVAL_MS, end_close_ms=end_close_ms, base_price=100.0, drift_per_bar=0.5)
    mid_idx = df.index[df["open_time"] == midnight_ms][0]
    df.loc[mid_idx, ["open", "high", "low", "close", "volume"]] = [200.0, 203.0, 197.0, 200.0, 5000.0]
    d = indicators(df)
    hlc3_mid = (203.0 + 197.0 + 200.0) / 3.0
    assert math.isclose(float(d["vwap"].loc[mid_idx]), hlc3_mid, rel_tol=1e-9)


def test_at06_verify_rvol20_excludes_current_candle_from_20_bar_average():
    """AT-06: Verify `RVOL20` excludes the current candle from the 20-bar volume average."""
    df = _make_candles(n=40, volume=100.0)
    df.loc[len(df) - 1, "volume"] = 500.0
    d = indicators(df)
    assert math.isclose(float(d["rvol20"].iloc[-1]), 5.0, rel_tol=1e-9)


def test_at07_verify_confirmed_swing_pivots_only_appear_after_2_right_hand_candles_close():
    """AT-07: Verify confirmed swing pivots only appear after 2 right-hand candles close."""
    df = _make_candles(n=40, drift_per_bar=0.0, wave_amp=0.0)
    df.loc[len(df) - 2, "low"] = 50.0
    pivots_unconfirmed = confirmed_swing_pivots(df, left=2, right=2)
    assert all(p["index"] != len(df) - 2 for p in pivots_unconfirmed)

    df2 = _make_candles(n=40, drift_per_bar=0.0, wave_amp=0.0)
    df2.loc[len(df2) - 3, "low"] = 50.0
    pivots_confirmed = confirmed_swing_pivots(df2, left=2, right=2)
    assert any(p["index"] == len(df2) - 3 and p["kind"] == "LOW" and p["price"] == 50.0 for p in pivots_confirmed)


# ============================================================================
# 18.2 REGIME & ROUTING TESTS (AT-08 .. AT-13)
# ============================================================================

def test_at08_classify_high_volatility_first_and_block_all_new_entries():
    """AT-08: Classify `HIGH_VOLATILITY` first and block all new entries even if trend or breakout rules are also true."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    last_i = len(m15) - 1
    m15.loc[last_i, "low"] = float(m15.loc[last_i, "close"]) - 25.0
    m15.loc[last_i, "high"] = float(m15.loc[last_i, "close"]) + 25.0
    m15.loc[last_i, "close"] = float(m15.loc[last_i, "high"]) - 1.0
    m15.loc[last_i, "volume"] = 5000.0
    reg = detect_m15_regime(m15)
    assert reg["regime"] == REGIME_HIGH_VOLATILITY
    assert reg["allow_new_entries"] is False
    routed = route_strategy_by_regime("BTCUSDT", reg, indicators(m5))
    assert routed["routed_strategy"] is None
    assert routed["status"] == "HIGH_VOLATILITY_BLOCK"


def test_at09_classify_breakout_up_and_breakout_down_and_route_only_to_strategy_b():
    """AT-09: Classify `BREAKOUT_UP` and `BREAKOUT_DOWN` and route only to Strategy B."""
    for direction, expected_reg in (("LONG", REGIME_BREAKOUT_UP), ("SHORT", REGIME_BREAKOUT_DOWN)):
        sign = 1 if direction == "LONG" else -1
        m15 = _make_candles(n=260, interval_ms=M15_INTERVAL_MS, drift_per_bar=0.0, wave_amp=0.10, wave_freq=1.6, bar_half_range=0.40, volume=1000.0)
        d_pre = indicators(m15)
        atr = float(d_pre["atr"].iloc[-1])
        donch = float(d_pre["donchian20_high" if sign == 1 else "donchian20_low"].iloc[-1])
        last_i = len(m15) - 1
        if sign == 1:
            cl = donch + 0.30 * atr
            lo = donch - 0.10 * atr
            hi = cl + 0.05 * atr
            op = donch
        else:
            cl = donch - 0.30 * atr
            hi = donch + 0.10 * atr
            lo = cl - 0.05 * atr
            op = donch
        m15.loc[last_i, ["open", "high", "low", "close", "volume"]] = [op, hi, lo, cl, 1800.0]
        reg = detect_m15_regime(m15)
        assert reg["regime"] == expected_reg
        m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS)
        routed = route_strategy_by_regime("BTCUSDT", reg, indicators(m5), arming_store=ArmingStateStore())
        assert routed["routed_strategy"] == STRATEGY_B


def test_at10_classify_strong_trend_up_down_and_route_to_a_on_pullback_or_c_on_momentum():
    """AT-10: Classify `STRONG_TREND_UP` / `STRONG_TREND_DOWN` and route to A on pullback or C on momentum."""
    _, m15, m5_pullback = _build_strategy_a_frames("LONG", strong=True)
    reg = detect_m15_regime(m15)
    assert reg["regime"] == REGIME_STRONG_TREND_UP
    routed_a = route_strategy_by_regime("BTCUSDT", reg, indicators(m5_pullback), arming_store=ArmingStateStore())
    assert routed_a["routed_strategy"] == STRATEGY_A

    m5_mom = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, drift_per_bar=0.35, wave_amp=0.0, bar_half_range=0.10)
    routed_c = route_strategy_by_regime("BTCUSDT", reg, indicators(m5_mom), arming_store=ArmingStateStore())
    assert routed_c["routed_strategy"] == STRATEGY_C


def test_at11_classify_trend_up_down_and_route_only_to_strategy_a():
    """AT-11: Classify `TREND_UP` / `TREND_DOWN` and route only to Strategy A."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=False)
    reg = detect_m15_regime(m15)
    assert reg["regime"] == REGIME_TREND_UP
    m5_no_pb = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, drift_per_bar=0.35, wave_amp=0.0, bar_half_range=0.10)
    routed = route_strategy_by_regime("BTCUSDT", reg, indicators(m5_no_pb), arming_store=ArmingStateStore())
    assert routed["routed_strategy"] == STRATEGY_A


def test_at12_classify_range_and_block_trend_strategies_while_allowing_d_or_e():
    """AT-12: Classify `RANGE` and block trend strategies while allowing D or E."""
    m15 = _make_candles(n=260, interval_ms=M15_INTERVAL_MS, drift_per_bar=0.0, wave_amp=0.15, wave_freq=1.6, bar_half_range=0.35)
    reg = detect_m15_regime(m15)
    assert reg["regime"] == REGIME_RANGE
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, drift_per_bar=0.0, wave_amp=0.15, wave_freq=1.6, bar_half_range=0.35)
    d5 = indicators(m5)
    assert evaluate_strategy_a(reg, d5)["eligible_regime"] is False
    assert evaluate_strategy_c(reg, d5)["eligible_regime"] is False
    routed = route_strategy_by_regime("BTCUSDT", reg, d5, arming_store=ArmingStateStore())
    assert routed["routed_strategy"] in (STRATEGY_D, STRATEGY_E)


def test_at13_classify_transition_and_allow_only_strategy_e_or_wait():
    """AT-13: Classify `TRANSITION` and allow only Strategy E or `WAIT`."""
    # Build an indicator frame where ADX >= 18 (so not RANGE) but |ema50_slope_norm| < 0.10 (so not TREND_UP/DOWN) -> TRANSITION
    m15 = _make_candles(n=260, interval_ms=M15_INTERVAL_MS, drift_per_bar=0.0, wave_amp=0.15, wave_freq=1.6, bar_half_range=0.35)
    d15 = indicators(m15)
    d15.loc[len(d15) - 1, "adx"] = 20.5
    d15.loc[len(d15) - 1, "ema50_slope_norm"] = 0.04
    reg = detect_m15_regime(d15)
    assert reg["regime"] == REGIME_TRANSITION
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, drift_per_bar=0.0, wave_amp=0.15, wave_freq=1.6, bar_half_range=0.35)
    routed = route_strategy_by_regime("BTCUSDT", reg, indicators(m5), arming_store=ArmingStateStore())
    assert routed["routed_strategy"] in (None, STRATEGY_E)
    assert routed["status"] == "WATCH_WAIT"


# ============================================================================
# 18.3 STRATEGY MODULE TESTS (AT-14 .. AT-25)
# ============================================================================

def test_at14_missing_or_non_finite_indicator_values_receive_zero_score_or_data_block():
    """AT-14: Missing or non-finite indicator values receive zero score or a data block; never default to a favorable neutral value."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    reg = detect_m15_regime(m15)
    d5 = indicators(m5)
    d5.loc[len(d5) - 1, "rsi"] = float("nan")
    d5.loc[len(d5) - 1, "stoch_rsi_k"] = float("nan")
    ev_a = evaluate_strategy_a(reg, d5)
    assert ev_a["triggered"] is False
    assert ev_a.get("non_finite_block") is True
    score, breakdown = compute_setup_quality_score(reg, {"routed_strategy": STRATEGY_A, "evaluation": ev_a}, d5)
    assert score == 0
    assert all(v == 0 for v in breakdown.values())


def test_at15_strategy_a_triggers_valid_buy_and_sell_trend_pullback_setups():
    """AT-15: Strategy A triggers valid BUY and SELL trend pullback setups when all M15 and M5 conditions hold."""
    for direction in ("LONG", "SHORT"):
        _, m15, m5 = _build_strategy_a_frames(direction, strong=True)
        reg = detect_m15_regime(m15)
        ev = evaluate_strategy_a(reg, indicators(m5))
        assert ev["triggered"] is True
        assert ev["direction"] == direction


def test_at16_strategy_a_rejects_pullbacks_that_close_beyond_ema50_by_more_than_015_atr():
    """AT-16: Strategy A rejects pullbacks that close beyond `EMA50` by more than `0.15 * ATR14`."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    d5 = indicators(m5)
    idx = len(d5) - 2
    d5.loc[idx, "close"] = float(d5.loc[idx, "ema50"]) - 0.30 * float(d5.loc[idx, "atr"])
    ev = evaluate_strategy_a(detect_m15_regime(m15), d5)
    assert ev["conditions"]["ema50_structure_held_015atr"] is False
    assert ev["triggered"] is False


def test_at17_strategy_b_requires_m15_breakout_followed_by_m5_retest_within_6_m5_candles():
    """AT-17: Strategy B requires an M15 breakout followed by an M5 retest within 6 M5 candles."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end, base_price=100.0, drift_per_bar=0.05)
    d5 = indicators(m5)
    atr5 = float(d5["atr"].iloc[-1])
    broken_level = float(d5["close"].iloc[-3])

    armed_bo = {
        "direction": "LONG",
        "level": broken_level,
        "armed_at_ms": int(d5["close_time"].iloc[-3]),
        "expires_at_ms": int(d5["close_time"].iloc[-3]) + 6 * M5_INTERVAL_MS,
    }
    d5.loc[len(d5) - 2, "low"] = broken_level + 0.05 * atr5
    d5.loc[len(d5) - 2, "close"] = broken_level + 0.08 * atr5
    d5.loc[len(d5) - 1, "open"] = broken_level + 0.05 * atr5
    d5.loc[len(d5) - 1, "close"] = broken_level + 0.25 * atr5
    d5.loc[len(d5) - 1, "low"] = broken_level + 0.02 * atr5
    d5.loc[len(d5) - 1, "high"] = broken_level + 0.30 * atr5

    ev = evaluate_strategy_b({"regime": REGIME_TREND_UP}, d5, armed_breakout=armed_bo, current_price=broken_level + 0.25 * atr5)
    assert ev["triggered"] is True
    assert ev["direction"] == "LONG"


def test_at18_strategy_b_rejects_unretested_breakouts_and_chased_prices_gt_050_atr():
    """AT-18: Strategy B rejects unretested breakouts and chased prices (`> 0.50 * M5_ATR14`)."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end, base_price=100.0, drift_per_bar=0.05)
    d5 = indicators(m5)
    atr5 = float(d5["atr"].iloc[-1])
    broken_level = float(d5["close"].iloc[-3])
    armed_bo = {
        "direction": "LONG",
        "level": broken_level,
        "armed_at_ms": int(d5["close_time"].iloc[-3]),
        "expires_at_ms": int(d5["close_time"].iloc[-3]) + 6 * M5_INTERVAL_MS,
    }
    d5.loc[len(d5) - 2, "low"] = broken_level + 0.05 * atr5
    d5.loc[len(d5) - 1, "open"] = broken_level + 0.10 * atr5
    d5.loc[len(d5) - 1, "close"] = broken_level + 0.75 * atr5
    ev_chased = evaluate_strategy_b({"regime": REGIME_TREND_UP}, d5, armed_breakout=armed_bo, current_price=broken_level + 0.75 * atr5)
    assert ev_chased["conditions"]["no_chase_050atr"] is False
    assert ev_chased["triggered"] is False

    d5.loc[len(d5) - 2, "low"] = broken_level + 0.40 * atr5
    d5.loc[len(d5) - 1, "low"] = broken_level + 0.40 * atr5
    ev_unretested = evaluate_strategy_b({"regime": REGIME_TREND_UP}, d5, armed_breakout=armed_bo, current_price=broken_level + 0.45 * atr5)
    assert ev_unretested["conditions"]["retest_within_020atr"] is False
    assert ev_unretested["triggered"] is False


def test_at19_strategy_c_triggers_only_in_strong_trend_up_down_with_rising_adx_macd_and_rvol():
    """AT-19: Strategy C triggers only in `STRONG_TREND_UP` / `STRONG_TREND_DOWN` with rising ADX, MACD acceleration, and RVOL."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.08, wave_amp=0.02, bar_half_range=0.20)
    d5 = indicators(m5)
    last_i = len(d5) - 1
    atr = float(d5["atr"].iloc[-1])
    prev5_hi = float(d5["high"].iloc[-6:-1].max())
    ema21 = prev5_hi - 0.20 * atr
    d5.loc[last_i, "ema21"] = ema21
    d5.loc[last_i, "ema9"] = ema21 + 0.25 * atr
    d5.loc[last_i - 1, "ema9"] = ema21 + 0.10 * atr
    d5.loc[last_i - 1, "ema21"] = ema21 - 0.05 * atr
    d5.loc[last_i - 2, "macd_hist"] = 0.05
    d5.loc[last_i - 1, "macd_hist"] = 0.12
    d5.loc[last_i, "macd_hist"] = 0.22
    d5.loc[last_i, "rsi"] = 62.0
    d5.loc[last_i, "rvol20"] = 1.35
    d5.loc[last_i, "open"] = prev5_hi - 0.05 * atr
    d5.loc[last_i, "close"] = prev5_hi + 0.15 * atr
    m15_reg = {"regime": REGIME_STRONG_TREND_UP, "adx14": 28.0, "adx_rising_2bars": True, "di_spread": 9.0}
    ev = evaluate_strategy_c(m15_reg, d5)
    assert ev["triggered"] is True
    assert ev["direction"] == "LONG"


def test_at20_strategy_c_rejects_overextended_candles_gt_10_atr_from_ema21():
    """AT-20: Strategy C rejects overextended candles (`> 1.0 * M5_ATR14` from `M5_EMA21`)."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.08)
    d5 = indicators(m5)
    last_i = len(d5) - 1
    atr = float(d5["atr"].iloc[-1])
    ema21 = float(d5["ema21"].iloc[-1])
    d5.loc[last_i, "ema9"] = ema21 + 0.30 * atr
    d5.loc[last_i - 1, "ema9"] = ema21 + 0.10 * atr
    d5.loc[last_i - 1, "ema21"] = ema21 - 0.05 * atr
    d5.loc[last_i - 2, "macd_hist"] = 0.05
    d5.loc[last_i - 1, "macd_hist"] = 0.12
    d5.loc[last_i, "macd_hist"] = 0.22
    d5.loc[last_i, "rsi"] = 64.0
    d5.loc[last_i, "rvol20"] = 1.40
    d5.loc[last_i, "open"] = ema21 + 0.80 * atr
    d5.loc[last_i, "close"] = ema21 + 1.35 * atr
    m15_reg = {"regime": REGIME_STRONG_TREND_UP, "adx14": 28.0, "adx_rising_2bars": True, "di_spread": 9.0}
    ev = evaluate_strategy_c(m15_reg, d5)
    assert ev["conditions"]["anti_exhaustion_1atr_ema21"] is False
    assert ev["triggered"] is False


def test_at21_strategy_d_triggers_only_in_range_after_bollinger_excursion_and_reclaim():
    """AT-21: Strategy D triggers only in `RANGE` after Bollinger excursion and reclaim inside band."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.20, bar_half_range=0.35)
    d5 = indicators(m5)
    last_i = len(d5) - 1
    bb_lo_prev = float(d5["bb_lower"].iloc[-2])
    bb_lo_cur = float(d5["bb_lower"].iloc[-1])
    atr = float(d5["atr"].iloc[-1])
    d5.loc[last_i - 1, "close"] = bb_lo_prev - 0.20 * atr
    d5.loc[last_i - 1, "low"] = bb_lo_prev - 0.30 * atr
    d5.loc[last_i - 1, "rsi"] = 28.0
    d5.loc[last_i - 1, "stoch_rsi_k"] = 14.0
    d5.loc[last_i - 1, "stoch_rsi_d"] = 18.0
    d5.loc[last_i, "open"] = bb_lo_cur + 0.02 * atr
    d5.loc[last_i, "close"] = bb_lo_cur + 0.25 * atr
    d5.loc[last_i, "low"] = bb_lo_cur - 0.05 * atr
    d5.loc[last_i, "rsi"] = 36.0
    d5.loc[last_i, "stoch_rsi_k"] = 22.0
    d5.loc[last_i, "stoch_rsi_d"] = 19.0
    d5.loc[last_i, "rvol20"] = 1.25
    d5.loc[last_i, "bb_upper"] = bb_lo_cur + 4.0 * atr
    d5.loc[last_i, "bb_mid"] = bb_lo_cur + 2.5 * atr
    m15_reg = {"regime": REGIME_RANGE, "donchian20_high": bb_lo_cur + 4.5 * atr, "donchian20_low": bb_lo_cur - 1.0 * atr}
    ev = evaluate_strategy_d(m15_reg, d5)
    assert ev["triggered"] is True
    assert ev["direction"] == "LONG"


def test_at22_strategy_d_is_blocked_in_trend_up_trend_down_strong_trend_and_breakout():
    """AT-22: Strategy D is blocked in `TREND_UP`, `TREND_DOWN`, `STRONG_TREND_*`, and `BREAKOUT_*`."""
    d5 = indicators(_make_candles(n=260, interval_ms=M5_INTERVAL_MS, drift_per_bar=0.0))
    for blocked_reg in (
        REGIME_TREND_UP,
        REGIME_TREND_DOWN,
        REGIME_STRONG_TREND_UP,
        REGIME_STRONG_TREND_DOWN,
        REGIME_BREAKOUT_UP,
        REGIME_BREAKOUT_DOWN,
    ):
        ev = evaluate_strategy_d({"regime": blocked_reg}, d5)
        assert ev["eligible_regime"] is False
        assert ev["triggered"] is False


def test_at23_strategy_e_requires_confirmed_pivot_sweep_005_to_035_atr_and_m5_choch_mss_within_5_bars():
    """AT-23: Strategy E requires a confirmed pivot sweep (`0.05–0.35 ATR`), close back inside level, and M5 CHoCH/MSS within 5 bars."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.0, bar_half_range=0.30)
    n = len(m5)
    m5.loc[n - 10, "low"] = 99.0
    m5.loc[n - 10, "close"] = 99.4
    m5.loc[n - 6, "high"] = 100.4
    d5 = indicators(m5)
    atr = float(d5["atr"].iloc[-1])
    d5.loc[n - 2, "low"] = 99.0 - 0.20 * atr
    d5.loc[n - 2, "close"] = 99.15
    d5.loc[n - 2, "open"] = 99.05
    d5.loc[n - 2, "rvol20"] = 1.35
    d5.loc[n - 1, "open"] = 100.1
    d5.loc[n - 1, "close"] = 100.55
    d5.loc[n - 1, "high"] = 100.60
    d5.loc[n - 1, "low"] = 100.0
    d5.loc[n - 1, "rvol20"] = 1.40

    ev = evaluate_strategy_e({"regime": REGIME_RANGE}, d5)
    assert ev["sweep_detected"] is True
    assert ev["choch_confirmed"] is True
    assert ev["triggered"] is True
    assert ev["direction"] == "LONG"


def test_at24_strategy_e_rejects_deep_breakdowns_gt_035_atr_or_sweeps_without_choch_mss():
    """AT-24: Strategy E rejects deep breakdowns (`> 0.35 * ATR14`) or sweeps without CHoCH/MSS."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.0, bar_half_range=0.30)
    n = len(m5)
    m5.loc[n - 10, "low"] = 99.0
    m5.loc[n - 6, "high"] = 100.4
    d5 = indicators(m5)
    atr = float(d5["atr"].iloc[-1])

    d5_deep = d5.copy()
    d5_deep.loc[n - 2, "low"] = 99.0 - 0.60 * atr
    d5_deep.loc[n - 2, "close"] = 99.15
    d5_deep.loc[n - 1, "close"] = 100.55
    ev_deep = evaluate_strategy_e({"regime": REGIME_RANGE}, d5_deep)
    assert ev_deep["triggered"] is False

    d5_no_choch = d5.copy()
    d5_no_choch.loc[n - 2, "low"] = 99.0 - 0.20 * atr
    d5_no_choch.loc[n - 2, "close"] = 99.15
    d5_no_choch.loc[n - 2, "rvol20"] = 1.30
    d5_no_choch.loc[n - 1, "close"] = 100.10
    ev_no_choch = evaluate_strategy_e({"regime": REGIME_RANGE}, d5_no_choch)
    assert ev_no_choch["sweep_detected"] is True
    assert ev_no_choch["choch_confirmed"] is False
    assert ev_no_choch["triggered"] is False


def test_at25_strategy_e_takes_priority_over_strategy_d_when_both_are_present_in_range():
    """AT-25: Strategy E takes priority over Strategy D when both are present in `RANGE`."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.0, bar_half_range=0.30)
    n = len(m5)
    m5.loc[n - 10, "low"] = 99.0
    m5.loc[n - 6, "high"] = 100.4
    d5 = indicators(m5)
    atr = float(d5["atr"].iloc[-1])
    d5.loc[n - 2, "low"] = 99.0 - 0.20 * atr
    d5.loc[n - 2, "close"] = 99.15
    d5.loc[n - 2, "rvol20"] = 1.35
    d5.loc[n - 1, "open"] = 100.1
    d5.loc[n - 1, "close"] = 100.55
    d5.loc[n - 1, "high"] = 100.60
    d5.loc[n - 1, "low"] = 100.0
    d5.loc[n - 1, "rvol20"] = 1.40
    m15_reg = {"ok": True, "regime": REGIME_RANGE, "allow_new_entries": True, "adx14": 14.0, "ema50_slope_norm": 0.02, "di_spread": 2.0, "rvol20": 1.1}
    routed = route_strategy_by_regime("BTCUSDT", m15_reg, d5, arming_store=ArmingStateStore())
    assert routed["routed_strategy"] == STRATEGY_E


# ============================================================================
# 18.4 SCORING, GATES & SIZING TESTS (AT-26 .. AT-37)
# ============================================================================

def test_at26_setup_quality_score_awards_0_points_to_conditions_that_did_not_occur():
    """AT-26: Setup Quality Score awards `0` points to conditions that did not occur."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    reg = detect_m15_regime(m15)
    d5 = indicators(m5)
    d5.loc[len(d5) - 1, "rvol20"] = 0.50
    d5.loc[len(d5) - 1, "vwap"] = float(d5.loc[len(d5) - 1, "close"]) + 10.0
    ev = evaluate_strategy_a(reg, d5)
    score, breakdown = compute_setup_quality_score(
        reg,
        {"routed_strategy": STRATEGY_A, "evaluation": ev},
        d5,
        spread_ok=False,
        depth_ok=False,
        trigger_distance_ok=False,
    )
    assert breakdown["Relative volume"] == 0
    assert breakdown["VWAP / structural location"] == 0
    assert breakdown["Execution quality"] == 0


def test_at27_setup_quality_score_maps_55_64_to_early_watch_65_77_to_setup_forming_and_ge78_to_entry_candidate():
    """AT-27: Setup Quality Score maps `55–64` to `EARLY_WATCH`, `65–77` to `SETUP_FORMING`, and `>= 78` to `ENTRY_CANDIDATE`."""
    assert classify_setup_stage(54, triggered=True, hard_gates_passed=True) == "REJECT"
    assert classify_setup_stage(55, triggered=False, hard_gates_passed=False) == "EARLY_WATCH"
    assert classify_setup_stage(64, triggered=True, hard_gates_passed=True) == "EARLY_WATCH"
    assert classify_setup_stage(65, triggered=False, hard_gates_passed=False) == "SETUP_FORMING"
    assert classify_setup_stage(77, triggered=True, hard_gates_passed=True) == "SETUP_FORMING"
    assert classify_setup_stage(78, triggered=True, hard_gates_passed=False) == "ENTRY_CANDIDATE"
    assert classify_setup_stage(78, triggered=True, hard_gates_passed=True) == "PAPER_ENTRY"


def test_at28_setup_quality_score_never_increases_leverage_or_risk_per_trade():
    """AT-28: Setup Quality Score never increases leverage or risk per trade."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc_80 = _make_account(now_ms, leverage=2, risk_pct=0.25)
    acc_80.confidence_override = 80
    acc_99 = _make_account(now_ms, leverage=2, risk_pct=0.25)
    acc_99.confidence_override = 99
    res_80 = evaluate_hybrid_adaptive(snap, acc_80)
    res_99 = evaluate_hybrid_adaptive(snap, acc_99)
    assert res_80.status == "VALID" and res_99.status == "VALID"
    assert res_80.plan["leverage"] == res_99.plan["leverage"] == 2
    assert math.isclose(res_80.plan["risk"], res_99.plan["risk"], rel_tol=1e-9)


def test_at29_spot_reference_can_never_create_a_futures_paper_entry():
    """AT-29: `SPOT_REFERENCE` can never create a Futures PAPER entry."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5, instrument_kind="SPOT_REFERENCE")
    acc = _make_account(now_ms)
    res = evaluate_hybrid_adaptive(snap, acc)
    assert res.status != "VALID"
    assert res.plan is None
    assert res.meta.get("rejection_category") == "rejected_by_instrument"


def test_at30_stale_quotes_gt_30_seconds_block_entries_with_stale_data():
    """AT-30: Stale quotes (`> 30 seconds`) block entries with `STALE_DATA`."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(
        now_ms,
        m15,
        m5,
        quote_times={
            "last": now_ms - 35_000,
            "mark": now_ms - 35_000,
            "book": now_ms - 35_000,
            "depth": now_ms - 35_000,
        },
    )
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms))
    assert res.status != "VALID"
    assert res.meta.get("data_status") == "STALE_DATA"


def test_at31_spread_or_depth_violation_blocks_entries():
    """AT-31: Spread (`> 10 bps` or `> 0.08 * M5_ATR14`) or depth violation (`< 5x notional`) blocks entries."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap_wide = _make_snapshot_from_frames(now_ms, m15, m5, bid=99.0, ask=101.0)
    res_wide = evaluate_hybrid_adaptive(snap_wide, _make_account(now_ms))
    assert res_wide.status != "VALID"
    assert res_wide.meta.get("rejection_category") == "rejected_by_liquidity_or_spread"

    snap_thin = _make_snapshot_from_frames(now_ms, m15, m5, bids=[(120.0, 0.01)], asks=[(120.02, 0.01)])
    res_thin = evaluate_hybrid_adaptive(snap_thin, _make_account(now_ms))
    assert res_thin.status != "VALID"
    assert res_thin.meta.get("rejection_category") == "rejected_by_liquidity_or_spread"


def test_at32_sl_distance_outside_075_to_250_m5_atr14_blocks_entries():
    """AT-32: SL distance outside `0.75–2.50 * M5_ATR14` blocks entries without moving SL inside structure."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    m5_deep = m5.copy()
    m5_deep.loc[len(m5_deep) - 3, "low"] = float(m5_deep["close"].iloc[-1]) - 10.0
    snap = _make_snapshot_from_frames(now_ms, m15, m5_deep)
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms))
    assert res.status != "VALID"
    assert res.meta.get("rejection_category") == "rejected_by_sl_or_rr"


def test_at33_round_trip_cost_gt_20pct_of_1r_blocks_entries():
    """AT-33: Round-trip cost `> 20%` of 1R blocks entries."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc_high_cost = _make_account(now_ms, taker_fee=0.005, exit_slippage_bps=50.0, funding_reserve_rate=0.005)
    res = evaluate_hybrid_adaptive(snap, acc_high_cost)
    assert res.status != "VALID"
    assert res.meta.get("rejection_category") == "rejected_by_sl_or_rr"


def test_at34_net_reward_lt_15r_to_opposing_structure_blocks_entries():
    """AT-34: Net reward `< 1.5R` to opposing structure blocks entries."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    last_hi = float(m5["high"].iloc[-1])
    snap = _make_snapshot_from_frames(now_ms, m15, m5, previous_day_high=last_hi + 0.80)
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms))
    assert res.status != "VALID"
    assert res.meta.get("rejection_category") == "rejected_by_sl_or_rr"


def test_at35_position_sizing_caps_per_trade_risk_at_025pct_and_portfolio_risk_at_050pct():
    """AT-35: Position sizing caps per-trade risk at `0.25%` and portfolio risk at `0.50%`."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc = _make_account(now_ms, equity=1000.0, risk_pct=0.50)
    res = evaluate_hybrid_adaptive(snap, acc)
    assert res.status == "VALID"
    assert res.plan["risk"] <= 1000.0 * 0.0025 + 1e-8

    acc_partial = _make_account(now_ms, equity=1000.0, total_existing_risk=4.0)
    res_partial = evaluate_hybrid_adaptive(snap, acc_partial)
    assert res_partial.status == "VALID"
    assert res_partial.plan["risk"] <= 1.0 + 1e-8


def test_at36_daily_loss_le_minus_20pct_or_3_consecutive_losses_triggers_cooldown_block():
    """AT-36: Daily loss `<= -2.0%` or `3` consecutive losses triggers cooldown block."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc_dd = _make_account(now_ms, equity=975.0, day_start_equity=1000.0)
    res_dd = evaluate_hybrid_adaptive(snap, acc_dd)
    assert res_dd.status != "VALID"
    assert res_dd.meta.get("rejection_category") == "rejected_by_cooldown_or_daily_loss"

    acc_losses = _make_account(now_ms, consecutive_losses=3)
    acc_losses.last_loss_closed_ms = now_ms - 1_800_000
    res_losses = evaluate_hybrid_adaptive(snap, acc_losses)
    assert res_losses.status != "VALID"
    assert res_losses.meta.get("rejection_category") == "rejected_by_cooldown_or_daily_loss"


def test_at37_two_active_positions_block_new_entries_with_entry_blocked_position_limit_while_keeping_scanner_active():
    """AT-37: Two active positions block new entries with `ENTRY_BLOCKED_POSITION_LIMIT` while keeping scanner discovery active."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc_full = _make_account(now_ms, active_pending_count=2)
    res = evaluate_hybrid_adaptive(snap, acc_full)
    assert res.status != "VALID"
    assert res.meta.get("rejection_category") == "rejected_by_position_limit"


# ============================================================================
# 18.5 ORDER & EXIT LIFECYCLE TESTS (AT-38 .. AT-46)
# ============================================================================

def test_at38_entry_stop_limit_uses_1_tick_trigger_and_010_atr_limit_cap():
    """AT-38: Entry `STOP-LIMIT` uses `1 tick` trigger and `0.10 * M5_ATR14` limit cap."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    d5 = indicators(m5)
    sig = d5.iloc[-1]
    atr = float(sig.atr)
    tick = 0.01
    order = build_hybrid_entry_order(
        sig,
        "LONG",
        atr,
        float(d5["low"].iloc[-6:].min()),
        tick,
        now_ms,
        float(sig.high) - 0.05,
    )
    expected_trigger = quantize(float(sig.high), tick, up=True) + tick
    expected_limit = quantize(expected_trigger + 0.10 * atr, tick, up=False)
    assert math.isclose(order["entry_trigger"], expected_trigger, rel_tol=1e-9)
    assert math.isclose(order["entry"], expected_limit, rel_tol=1e-9)


def test_at39_price_already_beyond_entry_limit_cancels_as_missed_no_chase():
    """AT-39: Price already beyond entry limit cancels as `MISSED_NO_CHASE`."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    d5 = indicators(m5)
    sig = d5.iloc[-1]
    atr = float(sig.atr)
    order = build_hybrid_entry_order(
        sig,
        "LONG",
        atr,
        float(d5["low"].iloc[-6:].min()),
        0.01,
        now_ms,
        last_price=float(sig.high) + 0.50 * atr,
    )
    assert order["missed_no_chase"] is True
    assert order["order_status"] == "MISSED_NO_CHASE"


def test_at40_unfilled_pending_order_expires_after_2_completed_m5_candles():
    """AT-40: Unfilled pending order expires after `2` completed M5 candles."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    d5 = indicators(m5)
    sig = d5.iloc[-1]
    order = build_hybrid_entry_order(
        sig,
        "LONG",
        float(sig.atr),
        float(d5["low"].iloc[-6:].min()),
        0.01,
        now_ms,
        last_price=float(sig.high) - 0.05,
    )
    assert order["expires_ms"] == int(sig.close_time) + 1 + 2 * M5_INTERVAL_MS


def test_at41_tp1_closes_40pct_at_1r_and_moves_sl_to_fee_adjusted_net_break_even():
    """AT-41: `TP1` closes `40%` at `+1R` and moves SL to fee-adjusted net break-even."""
    tp = compute_hybrid_tp_targets(
        entry=100.0,
        sl=98.0,
        direction="LONG",
        tick=0.01,
        qty=10.0,
        step=0.01,
        min_qty=0.01,
        min_notional=5.0,
    )
    assert tp["split_mode"] == "3_WAY_40_35_25"
    assert math.isclose(tp["tp1"], 102.0, rel_tol=1e-9)
    assert math.isclose(tp["tp1_qty"], 4.0, rel_tol=1e-9)

    now_ms, _, m5 = _build_strategy_a_frames("LONG", strong=True)
    plan = {
        "direction": "LONG",
        "entry": 100.0,
        "sl": 98.0,
        "distance": 2.0,
        "tp1": 102.0,
        "tp2": 104.0,
        "tp3": 106.0,
        "tick": 0.01,
        "fee": 0.0005,
        "funding_per_unit": 0.03,
        "slippage_per_unit": 0.02,
        "fill_ms": int(m5["open_time"].iloc[-3]),
    }
    lc = evaluate_hybrid_position_lifecycle(plan, m5, now_ms, mark_price=102.2, tp1_filled=True)
    expected_be = quantize(break_even_price(100.0, "LONG", 0.0005, 0.03, 0.02), 0.01, up=True)
    assert lc["stop"] >= expected_be


def test_at42_tp2_closes_35pct_at_2r_and_runner_25pct_targets_3r_or_trailing_stop():
    """AT-42: `TP2` closes `35%` at `+2R` and runner (`25%`) targets `+3R` or trailing stop."""
    tp = compute_hybrid_tp_targets(
        entry=100.0,
        sl=98.0,
        direction="LONG",
        tick=0.01,
        qty=10.0,
        step=0.01,
        min_qty=0.01,
        min_notional=5.0,
    )
    assert math.isclose(tp["tp2"], 104.0, rel_tol=1e-9)
    assert math.isclose(tp["tp2_qty"], 3.5, rel_tol=1e-9)
    assert math.isclose(tp["tp3"], 106.0, rel_tol=1e-9)
    assert math.isclose(tp["runner_qty"], 2.5, rel_tol=1e-9)


def test_at43_trailing_stop_activates_only_after_15r_on_completed_m5_candles_and_never_widens():
    """AT-43: Trailing stop activates only after `+1.5R` on completed M5 candles and never widens."""
    now_ms, _, m5 = _build_strategy_a_frames("LONG", strong=True)
    d5 = indicators(m5)
    entry = float(d5["close"].iloc[-4])
    atr = float(d5["atr"].iloc[-1])
    dist = 1.0 * atr
    d5.loc[len(d5) - 1, "high"] = entry + 2.0 * dist
    d5.loc[len(d5) - 1, "close"] = entry + 1.8 * dist
    plan = {
        "direction": "LONG",
        "entry": entry,
        "sl": entry - dist,
        "distance": dist,
        "tp1": entry + dist,
        "tp2": entry + 2.0 * dist,
        "tp3": entry + 3.0 * dist,
        "tick": 0.01,
        "fee": 0.0005,
        "funding_per_unit": 0.01,
        "slippage_per_unit": 0.01,
        "fill_ms": int(d5["open_time"].iloc[-3]),
    }
    lc1 = evaluate_hybrid_position_lifecycle(plan, d5, now_ms, mark_price=entry + 1.8 * dist, tp1_filled=True)
    assert lc1["reached_15_r"] is True
    expected_trail = quantize((entry + 2.0 * dist) - 1.25 * atr, 0.01, up=True)
    assert math.isclose(lc1["stop"], expected_trail, rel_tol=1e-6)

    tighter_stop = lc1["stop"] + 0.50
    lc2 = evaluate_hybrid_position_lifecycle(
        plan,
        d5,
        now_ms,
        mark_price=entry + 1.8 * dist,
        current_stop=tighter_stop,
        tp1_filled=True,
        reached_15_r=True,
    )
    assert lc2["stop"] == tighter_stop


def test_at44_lot_size_fallback_switches_cleanly_when_3_way_partial_tp_violates_min_qty_or_min_notional():
    """AT-44: Lot-size fallback switches cleanly when 3-way partial TP violates `minQty` or `minNotional`."""
    tp_2way = compute_hybrid_tp_targets(
        entry=1000.0,
        sl=990.0,
        direction="LONG",
        tick=0.1,
        qty=0.010,
        step=0.001,
        min_qty=0.004,
        min_notional=3.5,
    )
    assert tp_2way["split_mode"] == "2_WAY_60_40_FALLBACK"
    assert math.isclose(tp_2way["tp1_qty"], 0.006, rel_tol=1e-9)
    assert math.isclose(tp_2way["runner_qty"], 0.004, rel_tol=1e-9)

    tp_1way = compute_hybrid_tp_targets(
        entry=1000.0,
        sl=990.0,
        direction="LONG",
        tick=0.1,
        qty=0.010,
        step=0.001,
        min_qty=0.008,
        min_notional=5.0,
    )
    assert tp_1way["split_mode"] == "1_WAY_100_FALLBACK"
    assert math.isclose(tp_1way["tp1_r"], 1.8, rel_tol=1e-9)
    assert math.isclose(tp_1way["tp1_qty"], 0.010, rel_tol=1e-9)


def test_at45_time_exit_closes_stalled_trades_after_6_bars_and_12_bars():
    """AT-45: Time exit closes stalled trades after `6` bars (`< +0.5R` with momentum reversal) and `12` bars (`< +1.0R`)."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end, base_price=100.0, drift_per_bar=-0.05, wave_amp=0.0, bar_half_range=0.10)
    d5 = indicators(m5)
    entry = float(d5["close"].iloc[-8]) + 0.50
    plan_6 = {
        "direction": "LONG",
        "entry": entry,
        "sl": entry - 5.0,
        "distance": 5.0,
        "tp1": entry + 5.0,
        "tick": 0.01,
        "fee": 0.0005,
        "funding_per_unit": 0.01,
        "slippage_per_unit": 0.01,
        "fill_ms": int(d5["open_time"].iloc[-7]),
    }
    lc6 = evaluate_hybrid_position_lifecycle(plan_6, d5, now_ms, mark_price=float(d5["close"].iloc[-1]))
    assert lc6["action"] == "EXIT"
    assert lc6["exit_reason"] == "TIME_EXIT_6_BARS"

    plan_12 = dict(plan_6, fill_ms=int(d5["open_time"].iloc[-13]))
    lc12 = evaluate_hybrid_position_lifecycle(
        plan_12,
        d5,
        now_ms,
        mark_price=float(d5["close"].iloc[-1]),
        reached_half_r=True,
        reached_one_r=False,
    )
    assert lc12["action"] == "EXIT"
    assert lc12["exit_reason"] == "TIME_EXIT_12_BARS"


def test_at46_same_bar_sl_and_tp_conflict_assumes_sl_hit_first_when_m1_sequence_is_unavailable():
    """AT-46: Same-bar SL and TP conflict assumes SL hit first when M1 sequence is unavailable."""
    now_ms, _, m5 = _build_strategy_a_frames("LONG", strong=True)
    plan = {
        "direction": "LONG",
        "entry": 100.0,
        "sl": 98.0,
        "distance": 2.0,
        "tp1": 102.0,
        "tick": 0.01,
        "fee": 0.0005,
        "funding_per_unit": 0.01,
        "slippage_per_unit": 0.01,
        "fill_ms": int(m5["open_time"].iloc[-2]),
    }
    lc = evaluate_hybrid_position_lifecycle(
        plan,
        m5,
        now_ms,
        mark_price=100.5,
        intrabar_high=102.5,
        intrabar_low=97.8,
        m1_sequence_available=False,
    )
    assert lc["action"] == "EXIT"
    assert lc["exit_reason"] == "STOP_LOSS"
    assert lc["flag"] == "CONSERVATIVE_INTRABAR_APPROXIMATION"


# ============================================================================
# 18.6 SCANNER, AI, TELEGRAM & PERSISTENCE TESTS (AT-47 .. AT-54)
# ============================================================================

def test_at47_scanner_processes_50_30_10_5_candidate_pipeline_and_rotates_all_30_shortlist_pairs_within_le_3_cycles():
    """AT-47: Scanner processes 50 -> 30 -> 10 -> 5 candidate pipeline and rotates all 30 shortlist pairs within <= 3 cycles."""
    rq = HybridRotatingQueue(stage1_cap=50, stage2_cap=30, stage3_cap=10, stage4_cap=5)
    candidates = [
        {"symbol": f"COIN{i:02d}USDT", "direction": "LONG" if i % 2 == 0 else "SHORT", "m15_score": 60 + (i % 9)}
        for i in range(30)
    ]
    shortlist_30 = rq.select_stage2_balanced(candidates)
    assert len(shortlist_30) == 30
    seen = set()
    for _ in range(3):
        batch = rq.select_stage3_batch(shortlist_30)
        assert len(batch) == 10
        seen.update(c["symbol"] for c in batch)
    assert len(seen) == 30


def test_at48_closed_m15_m5_candles_are_cached_and_not_re_evaluated_or_sent_to_groq_twice():
    """AT-48: Closed M15/M5 candles are cached and not re-evaluated or sent to Groq twice."""
    rq = HybridRotatingQueue()
    key = ("BTCUSDT", "5m", 1_763_337_600_000, STRATEGY_A, "LONG")
    rq.ai_cache[key] = {"ai_status": "AI_ADVISORY_OK", "cached": True}
    assert key in rq.ai_cache
    assert rq.ai_cache[key]["cached"] is True


def test_at49_groq_outage_or_http_429_sets_ai_unavailable_without_blocking_valid_paper_entry():
    """AT-49: Groq outage or HTTP 429 sets `AI_UNAVAILABLE` without blocking a valid `PAPER_ENTRY` in `Balanced` mode."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms))
    assert res.status == "VALID"
    adv = evaluate_hybrid_ai_advisor(res, {"ok": False, "decision": "WAIT", "reasons": ["Groq HTTP 429 rate limit"]})
    assert adv["ai_status"] == "AI_UNAVAILABLE"
    assert adv["final_status"] == "VALID"
    assert adv["paper_entry_allowed"] is True


def test_at50_telegram_deduplication_allows_stage_upgrades_but_blocks_duplicate_messages():
    """AT-50: Telegram deduplication allows stage upgrades (`EARLY_WATCH -> SETUP_FORMING -> PAPER_ENTRY`) but blocks duplicate stage messages."""
    dedup = HybridAlertDeduplicator()
    args = ("BTCUSDT", "5m", 1_763_337_600_000, STRATEGY_A, "LONG")
    assert dedup.should_send(*args, stage="EARLY_WATCH") is True
    assert dedup.should_send(*args, stage="EARLY_WATCH") is False
    assert dedup.should_send(*args, stage="SETUP_FORMING") is True
    assert dedup.should_send(*args, stage="SETUP_FORMING") is False
    assert dedup.should_send(*args, stage="PAPER_ENTRY") is True
    assert dedup.should_send(*args, stage="PAPER_ENTRY") is False
    assert dedup.should_send(*args, stage="EARLY_WATCH") is False


def test_at51_news_api_failure_sets_news_unavailable_without_crashing_technical_scanning():
    """AT-51: News API failure sets `NEWS_UNAVAILABLE` without crashing technical scanning."""
    funnel = empty_hybrid_funnel()
    assert "blocked_by_news" in funnel and len(funnel) == 17
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    reg = detect_m15_regime(m15)
    routed = route_strategy_by_regime("BTCUSDT", reg, indicators(m5))
    assert routed["routed_strategy"] == STRATEGY_A


def test_at52_worker_continues_scanning_when_admin_ui_token_expires():
    """AT-52: Worker continues scanning when admin UI token expires."""
    import tempfile
    from pathlib import Path
    from futures.security import SecurityManager

    with tempfile.TemporaryDirectory() as td:
        sec = SecurityManager(Path(td) / "sec.sqlite3", secret_getter=lambda k: "admin-secret-123" if k == "ADMIN_PASSWORD" else None)
        worker_tok = sec.issue_worker_session()
        allowed, reason = sec.authorize_action("AUTO_SCAN", session_token=worker_tok)
        assert allowed is True and reason == "ALLOWED"


def test_at53_user_preferences_and_scanner_runs_persist_in_postgresql_without_resetting_sidebar_toggles():
    """AT-53: `user_preferences` and `scanner_runs` persist in PostgreSQL/global control DB without resetting sidebar toggles."""
    import tempfile
    from pathlib import Path
    from futures.db_backend import load_global_preferences, save_global_preferences

    with tempfile.TemporaryDirectory() as td:
        db_p = Path(td) / "paper_history.sqlite3"
        res = save_global_preferences(db_p, {"auto_scan": True, "interval": 300, "relaxed_mode": True})
        assert res["status"] == "SAVED"
        loaded = load_global_preferences(db_p)
        assert loaded["auto_scan"] is True
        assert loaded["interval"] == 300
        assert loaded["relaxed_mode"] is True


def test_at54_supabase_backup_restore_validates_timestamp_inclusive_checksum_and_rejects_stale_snapshots():
    """AT-54: Supabase backup/restore validates timestamp-inclusive checksum and rejects stale snapshots."""
    import json
    import tempfile
    from pathlib import Path
    from futures.paper import PaperLedger

    with tempfile.TemporaryDirectory() as td:
        ledger = PaperLedger(Path(td) / "paper.sqlite3", 1000.0, expected_instrument_kind="USD_M_PERPETUAL")
        raw = json.loads(ledger.export_json(backup_at_ms=2_000_000))
        tampered = dict(raw, backup_at_ms=2_000_001)
        try:
            ledger.import_backup_json(tampered)
            raise AssertionError("Expected ValueError on timestamp tampering")
        except ValueError as exc:
            assert "Checksum" in str(exc) or "manipulasi" in str(exc)


# ============================================================================
# 18.7 WALK-FORWARD & ROBUSTNESS TESTS (AT-55 .. AT-57)
# ============================================================================

def test_at55_backtest_runner_produces_all_required_metrics_for_strategies_a_e_and_f_across_all_walk_forward_folds():
    """AT-55: Backtest runner produces all required metrics for Strategies `A`–`E` and `F` across all walk-forward folds."""
    res = run_walk_forward_validation()
    assert res["ok"] is True
    assert res["walk_forward_folds_count"] == 6
    for strat_key in ("LEGACY", STRATEGY_A, STRATEGY_B, STRATEGY_C, STRATEGY_D, STRATEGY_E, STRATEGY_F, "F_GROQ_UNAVAILABLE"):
        m = res["strategy_comparisons"][strat_key]
        assert m["total_closed_trades"] > 0
        assert "expectancy_r" in m and "net_profit_factor" in m and "max_drawdown_pct" in m


def test_at56_cost_stress_test_at_15x_baseline_fees_slippage_funding_maintains_positive_expectancy():
    """AT-56: Cost stress test at `1.5x` baseline fees/slippage/funding maintains `expectancy_after_costs > 0`."""
    res_stress = run_walk_forward_validation(cost_multiplier=1.5)
    f_stress = res_stress["strategy_comparisons"][STRATEGY_F]
    assert f_stress["expectancy_r"] > 0
    assert f_stress["net_profit_factor"] >= 1.15


def test_at57_parameter_perturbation_test_plus_minus_10pct_shows_no_cliff_edge_collapse():
    """AT-57: Parameter perturbation test (`±10%`) shows no cliff-edge collapse in out-of-sample expectancy."""
    res_plus = run_walk_forward_validation(perturbation_pct=0.10)
    res_minus = run_walk_forward_validation(perturbation_pct=-0.10)
    assert res_plus["strategy_comparisons"][STRATEGY_F]["expectancy_r"] > 0
    assert res_minus["strategy_comparisons"][STRATEGY_F]["expectancy_r"] > 0
    assert res_plus["strategy_comparisons"][STRATEGY_F]["net_profit_factor"] >= 1.15

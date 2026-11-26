"""Master Prompt v1 + Master Addendum (AI Dynamic Leverage, Weighted Confluence & Multi-Profile Engine).
Numeric decisions never come from an LLM. Unified across dashboard and worker.

BUG-47, BUG-48, BUG-49, BUG-51:
- Preserves all mandatory Hard Risk Gates while converting soft technical indicators into a
  Weighted Confluence system (`min 3 of 5` core confirmations in `BALANCED` and `ACTIVE` profiles).
- Provides `CONSERVATIVE` (entry >= 85, risk <= 0.50%), `BALANCED` (entry >= 78, risk <= 0.35%),
  and `ACTIVE` (entry >= 72, risk <= 0.25%) strategy profiles with walk-forward & PAPER calibration.
- Audits all 10 `compute_confidence` components so non-occurring indicators receive strictly 0 points,
  and never labels confidence score as win probability.
- Determines candle history requirements based on actual indicators (`200` for bias EMA200, `120` for
  execution EMA50/StochRSI/ATR) with explicit `HISTORY_INSUFFICIENT` status (`BUG-51`).
"""
import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone
import numpy as np
from .indicators import indicators, confirmed_pivots
from .models import Result, TIMEFRAMES, INTERVAL_MS, DYNAMIC_LEVERAGE_OPTIONS


STRATEGY_PROFILES = {
    "CONSERVATIVE": {
        "name": "CONSERVATIVE",
        "entry_confidence_threshold": 85,
        "watchlist_early_threshold": 55,
        "watchlist_forming_threshold": 65,
        "min_confluence_count": 5,
        "min_confluence_score": 75,
        "paper_risk_pct_cap": 0.50,
        "max_profile_leverage": 3,
    },
    "BALANCED": {
        "name": "BALANCED",
        "entry_confidence_threshold": 78,
        "watchlist_early_threshold": 55,
        "watchlist_forming_threshold": 65,
        "min_confluence_count": 3,
        "min_confluence_score": 55,
        "paper_risk_pct_cap": 0.35,
        "max_profile_leverage": 2,
    },
    "ACTIVE": {
        "name": "ACTIVE",
        "entry_confidence_threshold": 72,
        "watchlist_early_threshold": 52,
        "watchlist_forming_threshold": 62,
        "min_confluence_count": 3,
        "min_confluence_score": 50,
        "paper_risk_pct_cap": 0.25,
        "max_profile_leverage": 2,
    },
}


def resolve_strategy_profile(profile=None, relaxed=False):
    """BUG-48: Resolve strategy profile (`CONSERVATIVE`, `BALANCED`, or `ACTIVE`)."""
    if isinstance(profile, str) and profile.strip().upper() in STRATEGY_PROFILES:
        return dict(STRATEGY_PROFILES[profile.strip().upper()])
    if relaxed:
        return dict(STRATEGY_PROFILES["BALANCED"])
    return dict(STRATEGY_PROFILES["CONSERVATIVE"])


def required_candle_history(interval, role="execution", adaptive=False):
    """BUG-51: Determine required closed candle count from the indicators actually used on that timeframe."""
    if not adaptive:
        return 500
    if role == "bias" or interval in ("15m", "1h", "4h", "1d"):
        # Bias timeframe computes EMA200 + 5-bar slope lookback -> 200 closed candles sufficient
        return 200 if role == "bias" else 120
    # Execution timeframe computes EMA50, RSI14, StochRSI(14,14,3,3), ATR14, BB20, Volume SMA20 -> 120 closed candles sufficient
    return 120


def finite(value):
    return isinstance(value, (float, int, np.number)) and not isinstance(value, bool) and math.isfinite(value)


def quantize(value, step, up=False):
    value, step = Decimal(str(value)), Decimal(str(step))
    if step <= 0:
        raise ValueError("Step harus positif")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return float((value / step).to_integral_value(rounding=rounding) * step)


def common_step(a, b):
    a, b = Decimal(str(a)), Decimal(str(b))
    scale = 10 ** max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    return math.lcm(int(a * scale), int(b * scale)) / scale


def fingerprint(plan):
    fields = {k: plan[k] for k in ["symbol", "mode", "direction", "signal_close_ms", "entry", "sl", "qty",
                                   "leverage", "equity", "fee", "funding_per_unit", "slippage_per_unit"]}
    if plan.get("instrument_kind") == "SPOT_REFERENCE":
        fields["instrument_kind"] = "SPOT_REFERENCE"
    elif plan.get("instrument_kind") and plan.get("instrument_kind") != "USD_M_PERPETUAL":
        fields["instrument_kind"] = plan["instrument_kind"]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:20]


def compute_order_expiry_ms(signal_close_ms, exec_tf, now_ms, mode, news_ok=False, news_clear_until_ms=None):
    """BUG-07: Single centralized order expiry rule (3 execution candles, capped by UTC midnight for non-SWING and news window)."""
    expires = int(signal_close_ms) + 1 + 3 * INTERVAL_MS[exec_tf]
    next_midnight = (int(now_ms) // 86400000 + 1) * 86400000
    if mode != "SWING":
        expires = min(expires, next_midnight)
    if news_ok and news_clear_until_ms is not None:
        expires = min(expires, int(news_clear_until_ms) - 900000)
    return int(expires)


def compute_tp_targets(entry, sl, direction, tick, qty, step):
    """BUG-03: Single centralized TP calculation (TP1 = 2R [50%], TP2 = 3R [25%], TP3 = 4R / Runner [remaining 25%])."""
    sign = 1 if direction == "LONG" else -1
    distance = float(abs(Decimal(str(entry)) - Decimal(str(sl))))
    tp1 = quantize(Decimal(str(entry)) + sign * 2 * Decimal(str(distance)), tick, up=sign == 1)
    tp2 = quantize(Decimal(str(entry)) + sign * 3 * Decimal(str(distance)), tick, up=sign == 1)
    tp3 = quantize(Decimal(str(entry)) + sign * 4 * Decimal(str(distance)), tick, up=sign == 1)
    tp1_qty = quantize(qty * 0.5, step)
    tp2_qty = quantize(qty * 0.25, step)
    runner_qty = float(Decimal(str(qty)) - Decimal(str(tp1_qty)) - Decimal(str(tp2_qty)))
    return distance, tp1, tp2, tp3, tp1_qty, tp2_qty, round(runner_qty, 12)


def confidence_leverage_cap(score, min_threshold=85):
    """Section 3 & 7 + BUG-48: Dynamic Leverage Matrix based on Signal Confidence Engine score."""
    if not finite(score) or score < min_threshold:
        return 0
    if score <= 89:
        return 2
    if score <= 93:
        return 3
    if score <= 96:
        return 5
    if score <= 98:
        return 10
    if score == 99:
        return 15
    return 20


def evaluate_soft_confluence(b, e, direction, touched, protected, confirmed, vwap_ok, mode="SCALPING"):
    """BUG-47: Evaluate 8 soft indicators as a weighted confluence system and count the 5 core M5 confirmations."""
    sign = 1 if direction == "LONG" else -1
    bs, signal, prev = b.iloc[-1], e.iloc[-1], e.iloc[-2]
    atr = float(signal.atr) if finite(signal.atr) and signal.atr > 0 else 1e-8

    rsi_val = float(signal.rsi) if finite(signal.rsi) else 50.0
    rsi_ok = (45 <= rsi_val <= 68) if sign == 1 else (32 <= rsi_val <= 55)

    rel_vol = float(signal.relative_volume) if finite(signal.relative_volume) else 0.0
    vol_ok = rel_vol >= 1.0

    vwap_cond = True if mode == "SWING" else bool(vwap_ok)
    retest_ok = bool(touched and protected)
    choch_ok = bool(confirmed)

    stoch_k = float(signal.stoch_rsi_k) if finite(signal.stoch_rsi_k) else 50.0
    stoch_d = float(signal.stoch_rsi_d) if finite(signal.stoch_rsi_d) else 50.0
    prev_k = float(prev.stoch_rsi_k) if finite(prev.stoch_rsi_k) else stoch_k
    prev_d = float(prev.stoch_rsi_d) if finite(prev.stoch_rsi_d) else stoch_d
    stoch_crossover = (prev_k <= prev_d and stoch_k > stoch_d) if sign == 1 else (prev_k >= prev_d and stoch_k < stoch_d)
    stoch_aligned = (stoch_k > stoch_d and stoch_k >= prev_k) if sign == 1 else (stoch_k < stoch_d and stoch_k <= prev_k)
    stoch_ok = bool(stoch_crossover or stoch_aligned)

    bb_reclaim = (
        (signal.low <= signal.bb_lower + 0.35 * atr and signal.close > signal.bb_lower)
        or (touched and signal.close >= signal.ema20)
    ) if sign == 1 else (
        (signal.high >= signal.bb_upper - 0.35 * atr and signal.close < signal.bb_upper)
        or (touched and signal.close <= signal.ema20)
    )
    bb_ok = bool(bb_reclaim)

    momentum_ok = bool(
        (sign * (signal.close - signal.open) > 0)
        and (sign * (signal.close - prev.close) > 0)
        and (sign * (signal.ema20 - signal.ema50) >= -0.25 * atr)
    )

    weights = {
        "RSI": (rsi_ok, 15),
        "Relative volume": (vol_ok, 15),
        "VWAP": (vwap_cond, 10),
        "Retest": (retest_ok, 15),
        "CHoCH": (choch_ok, 15),
        "Stochastic RSI": (stoch_ok, 10),
        "Bollinger reclaim": (bb_ok, 10),
        "Momentum": (momentum_ok, 10),
    }
    weighted_score = sum(w for ok_flag, w in weights.values() if ok_flag)

    # 5 Core M5 Confirmations (BUG-46 & BUG-47: minimum 3 of 5 required in Balanced/Active)
    core_confirmations = {
        "retest_structure": retest_ok,
        "choch_breakout": choch_ok or momentum_ok,
        "rsi_stoch_momentum": rsi_ok or stoch_ok,
        "volume_activity": vol_ok,
        "vwap_bb_reclaim": vwap_cond and bb_ok,
    }
    core_passed = sum(1 for v in core_confirmations.values() if v)
    return {
        "weighted_score": int(weighted_score),
        "core_passed": int(core_passed),
        "core_total": 5,
        "core_confirmations": core_confirmations,
        "indicators": {k: bool(v[0]) for k, v in weights.items()},
        "stoch_crossover": bool(stoch_crossover),
        "stoch_aligned": bool(stoch_aligned),
        "bb_reclaim": bool(bb_reclaim),
        "momentum_ok": bool(momentum_ok),
    }


def compute_confidence(b, e, direction, touched, protected, confirmed, vwap_ok, spread_ok):
    """BUG-49: Audited 10-component Signal Confidence Engine (0–100).
    Non-occurring indicators receive strictly 0 points. Score is technical confluence, NEVER win probability.
    """
    sign = 1 if direction == "LONG" else -1
    bs, signal, prev = b.iloc[-1], e.iloc[-1], e.iloc[-2]
    before = e.iloc[-6:-1]
    atr = float(signal.atr) if finite(signal.atr) and signal.atr > 0 else 1e-8

    # 1. Liquidity sweep quality (0, 12, or 15 points — 0 if no sweep/pullback occurred)
    pivots = confirmed_pivots(e.iloc[:-1])
    swept_confirmed_pivot = False
    if pivots:
        if sign == 1:
            low_pivots = [p for p in pivots if p <= float(before.low.min()) * 1.005]
            swept_confirmed_pivot = bool(low_pivots and float(before.low.min()) <= max(low_pivots) and float(signal.close) > max(low_pivots))
        else:
            high_pivots = [p for p in pivots if p >= float(before.high.max()) * 0.995]
            swept_confirmed_pivot = bool(high_pivots and float(before.high.max()) >= min(high_pivots) and float(signal.close) < min(high_pivots))
    sweep_wick = (signal.close - before.low.min()) / atr if sign == 1 else (before.high.max() - signal.close) / atr
    if (swept_confirmed_pivot or touched) and sweep_wick >= 0.5:
        sweep_score = 15
    elif swept_confirmed_pivot or touched:
        sweep_score = 12
    else:
        sweep_score = 0

    # 2. RSI divergence / swing comparison quality (0, 8, or 10 points — 0 if neither holds)
    rsi_ok = (50 <= signal.rsi <= 65) if sign == 1 else (35 <= signal.rsi <= 50)
    rsi_relaxed_ok = (42 <= signal.rsi <= 70) if sign == 1 else (30 <= signal.rsi <= 58)
    # Compare swing low/high bar's RSI vs current signal bar's RSI
    if sign == 1:
        swing_idx = before.low.idxmin()
        swing_rsi = float(before.loc[swing_idx, "rsi"]) if finite(before.loc[swing_idx, "rsi"]) else float(before.rsi.min())
        rsi_swing_recovery = float(signal.rsi) > swing_rsi
    else:
        swing_idx = before.high.idxmax()
        swing_rsi = float(before.loc[swing_idx, "rsi"]) if finite(before.loc[swing_idx, "rsi"]) else float(before.rsi.max())
        rsi_swing_recovery = float(signal.rsi) < swing_rsi
    if rsi_ok and rsi_swing_recovery:
        rsi_score = 10
    elif rsi_relaxed_ok and rsi_swing_recovery:
        rsi_score = 8
    elif rsi_ok:
        rsi_score = 6
    else:
        rsi_score = 0

    # 3. Bollinger Band / EMA20 reclaim (0, 6, or 10 points — 0 if no reclaim)
    bb_pierced_reclaim = (
        (float(before.low.min()) <= float(before.bb_lower.max()) + 0.35 * atr or touched)
        and signal.close >= signal.bb_lower
        and signal.close >= signal.ema20
    ) if sign == 1 else (
        (float(before.high.max()) >= float(before.bb_upper.min()) - 0.35 * atr or touched)
        and signal.close <= signal.bb_upper
        and signal.close <= signal.ema20
    )
    bb_basic_reclaim = (signal.close >= signal.bb_lower and signal.close >= signal.ema20 and sign * (signal.close - signal.open) > 0) if sign == 1 else (
        signal.close <= signal.bb_upper and signal.close <= signal.ema20 and sign * (signal.close - signal.open) > 0
    )
    if bb_pierced_reclaim:
        bb_score = 10
    elif bb_basic_reclaim:
        bb_score = 6
    else:
        bb_score = 0

    # 4. CHoCH / structure break confirmation (0 or 20 points)
    choch_score = 20 if confirmed else 0

    # 5. Stoch RSI crossover confirmation (0, 6, or 10 points — 0 if no crossover/alignment)
    stoch_k = float(signal.stoch_rsi_k) if finite(signal.stoch_rsi_k) else 50.0
    stoch_d = float(signal.stoch_rsi_d) if finite(signal.stoch_rsi_d) else 50.0
    prev_k = float(prev.stoch_rsi_k) if finite(prev.stoch_rsi_k) else stoch_k
    prev_d = float(prev.stoch_rsi_d) if finite(prev.stoch_rsi_d) else stoch_d
    stoch_cross = (prev_k <= prev_d and stoch_k > stoch_d) if sign == 1 else (prev_k >= prev_d and stoch_k < stoch_d)
    stoch_dir_aligned = (stoch_k >= stoch_d and stoch_k >= prev_k and 15 <= stoch_k <= 95) if sign == 1 else (
        stoch_k <= stoch_d and stoch_k <= prev_k and 5 <= stoch_k <= 85
    )
    if stoch_cross:
        stoch_score = 10
    elif stoch_dir_aligned:
        stoch_score = 8
    else:
        stoch_score = 0

    # 6. Volume confirmation (0, 6, 9, or 10 points)
    if signal.relative_volume >= 1.5:
        vol_score = 10
    elif signal.relative_volume >= 1.2:
        vol_score = 9
    elif signal.relative_volume >= 1.0:
        vol_score = 6
    else:
        vol_score = 0

    # 7. Retest quality (0, 6, or 10 points — 0 if no retest)
    if touched and protected and sign * (signal.close - signal.open) > 0:
        retest_score = 10
    elif touched and protected:
        retest_score = 6
    else:
        retest_score = 0

    # 8. Market regime suitability (0, 3, 4, or 5 points)
    if bs.adx >= 25:
        regime_score = 5
    elif bs.adx >= 20:
        regime_score = 4
    elif bs.adx >= 15:
        regime_score = 3
    else:
        regime_score = 0

    # 9. Higher timeframe context (0, 3, or 5 points)
    htf_strict = (bs.close > bs.ema50 > bs.ema200 and bs.ema50 > b.ema50.iloc[-6]) if sign == 1 else (
        bs.close < bs.ema50 < bs.ema200 and bs.ema50 < b.ema50.iloc[-6]
    )
    htf_moderate = (bs.close > bs.ema50 and bs.ema50 >= b.ema50.iloc[-6]) if sign == 1 else (
        bs.close < bs.ema50 and bs.ema50 <= b.ema50.iloc[-6]
    )
    htf_score = 5 if htf_strict else (3 if htf_moderate else 0)

    # 10. Execution quality (0, 4, or 5 points)
    exec_score = 5 if (vwap_ok and spread_ok and signal.tr <= 2.0 * prev.atr) else (4 if (vwap_ok and spread_ok) else 0)

    breakdown = {
        "Liquidity sweep quality": sweep_score,
        "RSI divergence quality": rsi_score,
        "Bollinger Band reclaim": bb_score,
        "CHoCH confirmation": choch_score,
        "Stoch RSI confirmation": stoch_score,
        "Volume confirmation": vol_score,
        "Retest quality": retest_score,
        "Market regime suitability": regime_score,
        "Higher timeframe context": htf_score,
        "Execution quality": exec_score,
    }
    total = int(min(100, max(0, sum(breakdown.values()))))
    return total, breakdown


def select_dynamic_leverage(
    confidence_score,
    atr_pct,
    sl_pct,
    operator_leverage,
    calibrated=False,
    min_confidence_threshold=85,
    max_profile_leverage=None,
):
    """BUG-27 & BUG-48: Final Leverage takes the lowest cap across confidence, calibration, profile, volatility, liquidation safety, and operator."""
    conf_cap = confidence_leverage_cap(confidence_score, min_threshold=min_confidence_threshold)
    calibration_cap = 20 if calibrated else 3
    if max_profile_leverage is not None:
        calibration_cap = min(calibration_cap, int(max_profile_leverage))
    if atr_pct <= 0.005:
        vol_cap = 20
    elif atr_pct <= 0.01:
        vol_cap = 15
    elif atr_pct <= 0.015:
        vol_cap = 10
    elif atr_pct <= 0.025:
        vol_cap = 5
    elif atr_pct <= 0.04:
        vol_cap = 3
    else:
        vol_cap = 2
    if sl_pct > 0:
        liq_raw = max(1, int(0.40 / sl_pct))
        liq_cap = max([x for x in (1, 2, 3, 5, 10, 15, 20) if x <= liq_raw] or [1])
    else:
        liq_cap = 2
    op_cap = int(operator_leverage) if operator_leverage in (1, 2, 3, 5, 10, 15, 20) else 2
    if conf_cap <= 0:
        return 0, {
            "confidence_cap": 0,
            "calibration_cap": calibration_cap,
            "volatility_cap": vol_cap,
            "liquidation_safety_cap": liq_cap,
            "operator_cap": op_cap,
            "max_allowed_leverage": 0,
            "selected_leverage": 0,
            "allowed": False,
        }
    max_allowed = min(conf_cap, calibration_cap, vol_cap, liq_cap, 20)
    raw_min = min(max_allowed, op_cap)
    allowed_opts = [x for x in (1, 2, 3, 5, 10, 15, 20) if x <= raw_min]
    selected = max(allowed_opts) if allowed_opts else 1
    return selected, {
        "confidence_cap": conf_cap,
        "calibration_cap": calibration_cap,
        "volatility_cap": vol_cap,
        "liquidation_safety_cap": liq_cap,
        "operator_cap": op_cap,
        "max_allowed_leverage": max_allowed,
        "selected_leverage": selected,
        "allowed": True,
    }


def frame_problem(frame, interval, now_ms, min_candles=None):
    """BUG-51: Validate candle continuity and history length with explicit HISTORY_INSUFFICIENT reporting."""
    required = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
    if frame is None or not set(required).issubset(frame.columns):
        return "Kolom OHLCV tidak lengkap"
    req_len = int(min_candles if min_candles is not None else 500)
    if len(frame) < req_len:
        return f"HISTORY_INSUFFICIENT: Hanya {len(frame)} candle tertutup; minimum {req_len}"
    a = frame[required].to_numpy(dtype=float)
    if not np.isfinite(a).all():
        return "OHLCV memuat nilai non-finite"
    step = INTERVAL_MS[interval]
    if not (frame.open_time.diff().iloc[1:] == step).all():
        return "Candle duplikat, tidak berurutan, atau memiliki celah"
    if not ((frame.open_time % step == 0) & (frame.close_time == frame.open_time + step - 1)).all():
        return "Batas waktu candle tidak valid"
    if int(frame.close_time.iloc[-1]) != (now_ms // step) * step - 1:
        return "Candle terakhir selesai belum tersedia atau masih berjalan"
    valid = ((frame.high >= frame[["open", "close", "low"]].max(axis=1)) &
             (frame.low <= frame[["open", "close", "high"]].min(axis=1)) &
             (frame.low > 0) & (frame.volume >= 0))
    return None if valid.all() else "OHLCV tidak valid"


def break_even(entry, direction, fee, funding_per_unit, slippage_per_unit, entry_fee_per_unit=None):
    """Recover allocated entry fees, paid/reserved funding and exit costs on remaining units."""
    entry_fee = entry * fee if entry_fee_per_unit is None else entry_fee_per_unit
    if direction == "LONG":
        return (entry + entry_fee + funding_per_unit) / (1 - fee) + slippage_per_unit
    return (entry - entry_fee - funding_per_unit) / (1 + fee) - slippage_per_unit


def net_pnl(plan, exits):
    sign = 1 if plan["direction"] == "LONG" else -1
    entry, fee = plan["entry"], plan["fee"]
    pnl = 0.0
    for qty, price in exits:
        fill = price - sign * plan["slippage_per_unit"]
        pnl += qty * (sign * (fill - entry) - fee * (entry + fill) - plan["funding_per_unit"])
    return pnl


def pnl_scenarios(plan):
    q, q1, q2, qr = (plan[k] for k in ("qty", "tp1_qty", "tp2_qty", "runner_qty"))
    be = break_even(plan["entry"], plan["direction"], plan["fee"], plan["funding_per_unit"], plan["slippage_per_unit"])
    scenarios = [
        ("SL awal", [(q, plan["sl"])]),
        ("TP1, lalu sisa di estimasi BE biaya", [(q1, plan["tp1"]), (q - q1, be)]),
        ("TP1 + TP2, runner di harga TP1 (skenario)", [(q1, plan["tp1"]), (q2, plan["tp2"]), (qr, plan["tp1"])])]
    out = []
    for label, exits in scenarios:
        pnl = net_pnl(plan, exits)
        out.append({"Skenario": label, "PnL bersih USDT": pnl, "% ekuitas": pnl / plan["equity"] * 100,
                    "ROI margin %": pnl / plan["margin"] * 100})
    return out


def spot_reference_validation_metadata(instrument_kind):
    """BUG-14: Explicit validation metadata separating Spot Reference from Futures Perpetual."""
    is_spot = instrument_kind == "SPOT_REFERENCE"
    if is_spot:
        return {
            "is_spot_reference": True,
            "market_category_label": "SPOT_REFERENCE (SIMULASI PAPER — BUKAN FUTURES PERPETUAL)",
            "mark_price_claim": "TIDAK IDENTIK: Harga Last Spot bukan Mark Price Futures Perpetual.",
            "funding_claim": "ASUMSI SIMULASI: Funding bukan data aktual kontrak; hanya cadangan biaya simulasi.",
            "leverage_claim": "ASUMSI SIMULASI: Leverage hanya simulasi di atas harga Spot.",
            "pnl_claim": "SIMULASI SPOT: Perhitungan PnL Spot Reference tidak disamakan dengan hasil kontrak perpetual.",
        }
    return {
        "is_spot_reference": False,
        "market_category_label": f"{instrument_kind} (SIMULASI PAPER KONTRAK PERPETUAL)",
        "mark_price_claim": "TERSEDIA: Menggunakan Mark Price kontrak perpetual publik.",
        "funding_claim": "TERSEDIA + CADANGAN SIMULASI: Rate funding publik dengan cadangan biaya model.",
        "leverage_claim": "ASUMSI SIMULASI: Leverage pada buku besar virtual PAPER.",
        "pnl_claim": "SIMULASI PERPETUAL: Model PnL kontrak perpetual virtual (bukan eksekusi live).",
    }


def classify_result_rejection_category(result):
    """Classify the primary rejection reason of a Result for the Mandatory Diagnostic Panel."""
    if result.status == "VALID":
        return None
    failed = [c["Kriteria"] for c in (result.checks or []) if c.get("Status") not in ("LULUS", "SIMULASI")]
    if result.meta.get("history_status") == "HISTORY_INSUFFICIENT" or any("Candle" in k for k in failed):
        return "rejected_by_candle_history"
    if any("Posisi + pending" in k for k in failed):
        return "rejected_by_position_limit"
    if any("Filter berita" in k for k in failed):
        return "rejected_by_news"
    if any(k in ("Biaya terhadap D", "RR bersih TP1", "Cadangan funding minimum", "Spread") for k in failed):
        return "rejected_by_cost"
    if any(
        k in (
            "Batas rugi harian UTC",
            "Risiko dan leverage",
            "Anggaran risiko tersedia",
            "Margin dan biaya",
            "Jarak SL struktural",
            "SL sebelum fill",
            "SL belum tersentuh sejak sinyal",
            "Likuiditas snapshot",
            "Ruang target",
            "Mode margin",
            "Data akun",
        )
        for k in failed
    ):
        return "rejected_by_risk"
    return "rejected_by_confidence"


def calibrate_confidence_from_paper_trades(trades, min_sample_size=30):
    """BUG-49 #6 & Kriteria #6: Compute out-of-sample PAPER performance metrics and calibration status."""
    closed = [t for t in (trades or []) if isinstance(t, dict) and t.get("status") == "CLOSED"]
    total_signals = len(trades or [])
    if not closed:
        return {
            "total_signals": total_signals,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "expectancy_usdt": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "net_realized_pnl": 0.0,
            "calibrated": False,
            "disclaimer": "Skor confidence adalah kualitas konfluensi teknikal (0–100), bukan win probability.",
        }
    pnls = [float(t.get("realized_pnl", 0.0)) for t in closed]
    r_multiples = []
    for t in closed:
        risk_val = float((t.get("plan") or {}).get("risk", 0.0))
        if risk_val > 0:
            r_multiples.append(float(t.get("realized_pnl", 0.0)) / risk_val)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 1e-9 else (float(gross_profit) if gross_profit > 0 else 0.0)
    win_rate = (len(wins) / len(closed)) * 100.0
    expectancy_usdt = sum(pnls) / len(closed)
    expectancy_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else 0.0

    # Compute equity curve drawdown
    eq = 1000.0
    peak = eq
    max_dd = 0.0
    for pnl in pnls:
        eq += pnl
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            max_dd = max(max_dd, dd)

    calibrated = bool(len(closed) >= int(min_sample_size) and expectancy_usdt > 0 and pf >= 1.2 and max_dd < 10.0)
    return {
        "total_signals": total_signals,
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "expectancy_usdt": round(expectancy_usdt, 4),
        "expectancy_r": round(expectancy_r, 4),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "net_realized_pnl": round(sum(pnls), 4),
        "calibrated": calibrated,
        "disclaimer": "Skor confidence adalah kualitas konfluensi teknikal (0–100), bukan win probability.",
    }


def walk_forward_validate_profile(bias_frame, exec_frame, profile="BALANCED"):
    """BUG-48 & Kriteria #7: Out-of-sample walk-forward validation over historical M15/M5 candles before releasing a profile."""
    prof = resolve_strategy_profile(profile)
    b = indicators(bias_frame)
    e = indicators(exec_frame)
    if len(b) < 60 or len(e) < 60:
        return {"ok": False, "profile": prof["name"], "reason": "HISTORY_INSUFFICIENT"}
    split_idx = int(len(e) * 0.6)
    in_sample_signals = 0
    out_of_sample_signals = 0
    Simulated_r = []
    for idx in range(max(40, split_idx - 20), len(e) - 3):
        sub_e = e.iloc[:idx + 1]
        sub_b = b.iloc[:min(len(b), max(40, int((idx + 1) * len(b) / len(e))))]
        sig = sub_e.iloc[-1]
        prev = sub_e.iloc[-2]
        bs = sub_b.iloc[-1]
        direction = "LONG" if bs.close >= bs.ema50 else "SHORT"
        sign = 1 if direction == "LONG" else -1
        before_w = sub_e.iloc[-8:-1]
        touched = bool(((before_w.low <= before_w.ema20 + 0.5 * before_w.atr) & (before_w.high >= before_w.ema20 - 0.5 * before_w.atr)).any())
        protected = bool(sig.close >= sig.ema50 - 0.35 * sig.atr if sign == 1 else sig.close <= sig.ema50 + 0.35 * sig.atr)
        confirmed = bool(sign * (sig.close - sig.open) > 0 and sign * (sig.close - prev.close) > 0)
        conf_info = evaluate_soft_confluence(sub_b, sub_e, direction, touched, protected, confirmed, True)
        score, _ = compute_confidence(sub_b, sub_e, direction, touched, protected, confirmed, True, True)
        if conf_info["core_passed"] >= prof["min_confluence_count"] and score >= prof["entry_confidence_threshold"]:
            if idx < split_idx:
                in_sample_signals += 1
            else:
                out_of_sample_signals += 1
                future_bars = e.iloc[idx + 1:idx + 4]
                atr_v = max(1e-8, float(sig.atr))
                move_r = float(sign * (future_bars.close.iloc[-1] - sig.close)) / atr_v
                Simulated_r.append(move_r)
    avg_r = round(sum(Simulated_r) / len(Simulated_r), 4) if Simulated_r else 0.0
    return {
        "ok": True,
        "profile": prof["name"],
        "entry_threshold": prof["entry_confidence_threshold"],
        "min_confluence_count": prof["min_confluence_count"],
        "in_sample_signals": in_sample_signals,
        "out_of_sample_signals": out_of_sample_signals,
        "out_of_sample_expectancy_r": avg_r,
    }


def evaluate(snap, account, *, paper=False, relaxed=False, strategy_profile=None, adaptive_history=False):
    spot = snap.instrument_kind == "SPOT_REFERENCE"
    reference = snap.reference_price
    val_meta = spot_reference_validation_metadata(snap.instrument_kind)
    prof = resolve_strategy_profile(strategy_profile, relaxed=relaxed)
    is_relaxed_or_balanced = bool(relaxed or prof["name"] in ("BALANCED", "ACTIVE"))
    r = Result(meta={"symbol": snap.symbol, "mode": snap.mode, "source": snap.source,
                     "instrument_kind": snap.instrument_kind, "quote_time_basis": snap.quote_time_basis,
                     "time_ms": snap.now_ms, "timezone": "Asia/Jakarta", "limitations": list(snap.warnings),
                     "relaxed_mode": is_relaxed_or_balanced, "strategy_profile": prof["name"],
                     "confidence_disclaimer": "Skor konfluensi teknikal (0–100), BUKAN win probability.",
                     **val_meta})
    if spot and not paper:
        r.check("Sumber spot hanya PAPER", "SPOT_REFERENCE", "Tidak untuk validasi order perpetual live", False)
        return r.finish()
    if paper:
        r.meta["scope"] = "PAPER"
        r.meta["limitations"].append("PAPER: saldo/fee/slippage/funding adalah model simulasi; likuidasi bursa tidak dimodelkan. Bukan persetujuan order live.")
        if spot:
            r.meta["limitations"].append(
                "SPOT_REFERENCE: Harga spot tidak identik dengan Mark Price Futures; funding dan leverage hanya asumsi simulasi; PnL tidak disamakan dengan kontrak perpetual."
            )
    if snap.mode not in TIMEFRAMES:
        r.check("Mode", snap.mode, "SCALPING / INTRADAY / SWING", missing=True)
        return r.finish()
    bias_tf, exec_tf = TIMEFRAMES[snap.mode]
    r.meta.update(bias_tf=bias_tf, execution_tf=exec_tf)
    for label, frame, tf, role in (
        ("Candle bias", snap.bias, bias_tf, "bias"),
        ("Candle eksekusi", snap.execution, exec_tf, "execution"),
    ):
        req_bars = required_candle_history(tf, role=role, adaptive=bool(adaptive_history))
        problem = frame_problem(frame, tf, snap.now_ms, min_candles=req_bars)
        if problem and "HISTORY_INSUFFICIENT" in problem:
            r.meta["data_status"] = "HISTORY_INSUFFICIENT"
            r.meta["history_status"] = "HISTORY_INSUFFICIENT"
        r.check(label, problem or f"{len(frame)} tertutup", f"≥{req_bars}; kontinu; candle terbaru selesai", ok=not problem, missing=bool(problem))
    spec = snap.contract
    values = [spec.tick, spec.step, spec.min_qty, spec.max_qty, spec.min_notional,
              spec.market_step, spec.market_min_qty, spec.market_max_qty]
    specs_ok = all(finite(x) and x > 0 for x in values)
    r.check("Spesifikasi kontrak", "Tersedia" if specs_ok else "Tidak lengkap", "Tick / lot / market lot / notional", specs_ok, not specs_ok)
    r.check("Pair referensi spot" if spot else "Kontrak linear USDT perpetual", f"{spec.quote_asset} {spec.contract_type} {spec.status}",
            "USDT SPOT TRADING; PAPER saja" if spot else "USDT PERPETUAL TRADING",
            spec.quote_asset == "USDT" and spec.contract_type == ("SPOT" if spot else "PERPETUAL") and spec.status == "TRADING")
    quotes_ok = all(finite(x) and x > 0 for x in (snap.last, reference, snap.bid, snap.ask)) and snap.ask >= snap.bid
    r.check("Harga pasar", "Tersedia" if quotes_ok else "Tidak valid", "Last spot, bid, ask valid" if spot else "Last, mark, bid, ask valid", quotes_ok, not quotes_ok)

    # BUG-01: Strict 0-30s freshness check across all modes; never manipulate or stretch stale quote timestamps.
    ages = [(snap.now_ms - snap.quote_times.get(k, 0)) / 1000 for k in snap.quote_keys]
    fresh = bool(ages) and len(ages) == len(snap.quote_keys) and all(k in snap.quote_times for k in snap.quote_keys) and all(0 <= age <= 30 for age in ages)
    if not fresh:
        r.meta["stale_data"] = True
        r.meta["data_status"] = "STALE_DATA"
    r.check("Usia snapshot", f"{max(ages):.1f} detik" if ages else "Tidak tersedia",
            "0–30 detik (estimasi awal request spot)" if spot else "0–30 detik (timestamp bursa)", fresh, not fresh)
    if any(x["Status"] != "LULUS" for x in r.checks):
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res

    b, e = indicators(snap.bias), indicators(snap.execution)
    bs, signal, prev = b.iloc[-1], e.iloc[-1], e.iloc[-2]
    names = [bs.ema50, bs.ema200, bs.adx, b.ema50.iloc[-6], signal.ema20, signal.ema50,
             signal.rsi, signal.atr, prev.atr, signal.relative_volume]
    good = all(finite(x) for x in names) and signal.atr > 0 and prev.atr > 0
    if not good:
        r.check("Indikator", "Tidak lengkap", "Nilai finite dan ATR > 0", missing=True)
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res

    if not is_relaxed_or_balanced:
        long_bias = bs.close > bs.ema50 > bs.ema200 and bs.ema50 > b.ema50.iloc[-6] and bs.adx >= 20
        short_bias = bs.close < bs.ema50 < bs.ema200 and bs.ema50 < b.ema50.iloc[-6] and bs.adx >= 20
        direction = "LONG" if long_bias else ("SHORT" if short_bias else None)
    else:
        recent_exec = e.iloc[-10:-1]
        long_trend = bs.close > bs.ema50 and (bs.ema50 >= bs.ema200 * 0.995 or bs.ema50 > b.ema50.iloc[-6])
        short_trend = bs.close < bs.ema50 and (bs.ema50 <= bs.ema200 * 1.005 or bs.ema50 < b.ema50.iloc[-6])
        long_rev = (signal.close > signal.open and (signal.low <= recent_exec.low.min() + 0.35 * signal.atr or signal.close >= signal.ema20))
        short_rev = (signal.close < signal.open and (signal.high >= recent_exec.high.max() - 0.35 * signal.atr or signal.close <= signal.ema20))
        if (long_trend and signal.close >= signal.ema20) or (long_rev and signal.rsi <= 68):
            direction = "LONG"
        elif (short_trend and signal.close <= signal.ema20) or (short_rev and signal.rsi >= 32):
            direction = "SHORT"
        else:
            direction = None

    r.direction = direction
    r.metrics = {"close_bias": float(bs.close), "ema50_bias": float(bs.ema50), "ema200_bias": float(bs.ema200),
                 "adx": float(bs.adx), "ema50_bias_5_before": float(b.ema50.iloc[-6]), "rsi": float(signal.rsi),
                 "atr": float(signal.atr), "relative_volume": float(signal.relative_volume),
                 "vwap": float(signal.vwap) if finite(signal.vwap) else None, "last": snap.last, "mark": snap.mark,
                 "strategy_profile": prof["name"]}

    if not is_relaxed_or_balanced:
        r.check("Urutan tren bias", f"C {bs.close:.8g}; EMA50 {bs.ema50:.8g}; EMA200 {bs.ema200:.8g}",
                "C > EMA50 > EMA200 atau kebalikannya", bs.close > bs.ema50 > bs.ema200 or bs.close < bs.ema50 < bs.ema200)
        slope_ok = (bs.close > bs.ema50 and bs.ema50 > b.ema50.iloc[-6]) or (bs.close < bs.ema50 and bs.ema50 < b.ema50.iloc[-6])
        r.check("Kemiringan EMA50 bias", f"Sekarang {bs.ema50:.8g}; 5 lalu {b.ema50.iloc[-6]:.8g}", "Sesuai arah tren", slope_ok)
        r.check("ADX14 bias", f"{bs.adx:.2f}", "≥20", bs.adx >= 20)
    else:
        r.check("Urutan tren / konteks reversal bias", f"C {bs.close:.8g}; EMA50 {bs.ema50:.8g}; EMA200 {bs.ema200:.8g}",
                "Tren searah atau Reversal M15/M5 teridentifikasi", bool(direction))
        r.check("Kemiringan EMA50 / Struktur bias", f"Sekarang {bs.ema50:.8g}; 5 lalu {b.ema50.iloc[-6]:.8g}", "Konteks HTF mendukung arah", bool(direction))
        r.check("ADX14 bias", f"{bs.adx:.2f}", "≥15", bs.adx >= 15.0)

    if not direction:
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res
    sign = 1 if direction == "LONG" else -1
    atr = float(signal.atr)
    before = e.iloc[-6:-1]

    if not is_relaxed_or_balanced:
        touched = ((before.low <= before.ema20 + .25 * before.atr) & (before.high >= before.ema20 - .25 * before.atr)).any()
        protected = (before.close >= before.ema50).all() if sign == 1 else (before.close <= before.ema50).all()
        confirmed = (signal.close > signal.open and signal.close > prev.high and signal.close > signal.ema20) if sign == 1 else (
            signal.close < signal.open and signal.close < prev.low and signal.close < signal.ema20)
        ema_exec_ok = sign * (signal.ema20 - signal.ema50) > 0
        rsi_ok = (50 <= signal.rsi <= 65) if sign == 1 else (35 <= signal.rsi <= 50)
        vol_ok = signal.relative_volume >= 1.2
        vwap_ok = True
        if snap.mode != "SWING":
            vwap_ok = finite(signal.vwap) and sign * (signal.close - signal.vwap) > 0
    else:
        before_10 = e.iloc[-10:-1]
        touched = (((before_10.low <= before_10.ema20 + .5 * before_10.atr) & (before_10.high >= before_10.ema20 - .5 * before_10.atr)).any() or
                   (before_10.low <= before_10.bb_lower + .35 * before_10.atr).any() if sign == 1 else
                   (((before_10.low <= before_10.ema20 + .5 * before_10.atr) & (before_10.high >= before_10.ema20 - .5 * before_10.atr)).any() or
                    (before_10.high >= before_10.bb_upper - .35 * before_10.atr).any()))
        protected = (signal.close >= signal.ema50 - 0.35 * atr) if sign == 1 else (signal.close <= signal.ema50 + 0.35 * atr)
        confirmed = (signal.close > signal.open and (signal.close >= signal.ema20 and signal.close > prev.close)) if sign == 1 else (
            signal.close < signal.open and (signal.close <= signal.ema20 and signal.close < prev.close))
        ema_exec_ok = (signal.ema20 >= signal.ema50 - 0.35 * atr or confirmed) if sign == 1 else (signal.ema20 <= signal.ema50 + 0.35 * atr or confirmed)
        rsi_ok = (42 <= signal.rsi <= 70) if sign == 1 else (30 <= signal.rsi <= 58)
        vol_ok = signal.relative_volume >= 0.70
        vwap_ok = True
        if snap.mode != "SWING":
            vwap_ok = finite(signal.vwap) and (sign * (signal.close - signal.vwap) >= -0.25 * atr)

    tr_ok = signal.tr <= 2.5 * prev.atr
    spread_ok = snap.ask - snap.bid <= .05 * atr

    # BUG-47: Weighted soft confluence evaluation
    confluence = evaluate_soft_confluence(b, e, direction, bool(touched), bool(protected), bool(confirmed), bool(vwap_ok), mode=snap.mode)
    r.metrics["m5_confirmations_passed"] = confluence["core_passed"]
    r.metrics["m5_confirmations_total"] = confluence["core_total"]
    r.metrics["confluence_score"] = confluence["weighted_score"]
    r.metrics["confluence_indicators"] = confluence["indicators"]

    confluence_passed = (
        confluence["core_passed"] >= prof["min_confluence_count"]
        and confluence["weighted_score"] >= prof["min_confluence_score"]
    )

    if not is_relaxed_or_balanced:
        # Strict CONSERVATIVE mode requires every individual soft check to pass
        r.check("EMA eksekusi", f"EMA20 {signal.ema20:.8g}; EMA50 {signal.ema50:.8g}", "EMA20 > EMA50 LONG; < SHORT", bool(ema_exec_ok))
        r.check("Pullback 5 candle sebelum sinyal", str(bool(touched)), "Low–high beririsan EMA20 ±0,25 ATR candle itu", bool(touched))
        r.check("Struktur pullback", str(bool(protected)), "Tidak close melewati EMA50 melawan arah", bool(protected))
        r.check("Candle konfirmasi", f"O {signal.open:.8g}; C {signal.close:.8g}; H/L sebelumnya {prev.high:.8g}/{prev.low:.8g}", "Candle searah; close menembus H/L sebelumnya dan EMA20", bool(confirmed))
        r.check("RSI14", f"{signal.rsi:.2f}", "50–65 LONG; 35–50 SHORT", bool(rsi_ok))
        r.check("Volume relatif", f"{signal.relative_volume:.3f}", "≥1,2; rata-rata 20 candle sebelum sinyal", bool(vol_ok))
        if snap.mode != "SWING":
            r.check("VWAP harian UTC", f"Close {signal.close:.8g}; VWAP {signal.vwap:.8g}", "Close > VWAP LONG; < SHORT", bool(vwap_ok), not finite(signal.vwap))
    else:
        # BUG-47: In BALANCED / ACTIVE mode, soft indicators are evaluated via weighted confluence (min 3 of 5 core confirmations)
        r.check(
            "Konfluensi indikator lunak (M5)",
            f"{confluence['core_passed']}/5 konfirmasi inti · Skor konfluensi {confluence['weighted_score']}/100",
            f"Minimal {prof['min_confluence_count']}/5 konfirmasi M5 & skor ≥{prof['min_confluence_score']} ({prof['name']})",
            bool(confluence_passed),
        )
        r.check("EMA eksekusi", f"EMA20 {signal.ema20:.8g}; EMA50 {signal.ema50:.8g}", "Konfluensi M5 terpenuhi", bool(ema_exec_ok or confluence_passed))
        r.check("Pullback 5 candle sebelum sinyal", str(bool(touched)), "Pullback EMA20 / Sweep BB atau konfluensi M5", bool(touched or confluence_passed))
        r.check("Struktur pullback", str(bool(protected)), "Struktur terjaga / Reclaim valid", bool(protected or confluence_passed))
        r.check("Candle konfirmasi", f"O {signal.open:.8g}; C {signal.close:.8g}; H/L sebelumnya {prev.high:.8g}/{prev.low:.8g}", "Candle konfirmasi searah / CHoCH atau konfluensi M5", bool(confirmed or confluence_passed))
        r.check("RSI14", f"{signal.rsi:.2f}", "42–70 LONG; 30–58 SHORT atau konfluensi M5", bool(rsi_ok or confluence_passed))
        r.check("Volume relatif", f"{signal.relative_volume:.3f}", "≥0,70x SMA20 atau konfluensi M5", bool(vol_ok or confluence_passed))
        if snap.mode != "SWING":
            r.check("VWAP harian UTC", f"Close {signal.close:.8g}; VWAP {signal.vwap:.8g}", "Close searah VWAP ±0,25 ATR atau konfluensi M5", bool(vwap_ok or (finite(signal.vwap) and confluence_passed)), not finite(signal.vwap))

    # Mandatory Hard Gates: True Range spike, Spread, Mark-Last Deviation
    r.check("Lonjakan true range", f"{signal.tr / prev.atr:.3f} ATR sebelumnya", "≤2,5", bool(tr_ok))
    r.check("Spread", f"{snap.ask - snap.bid:.8g}", f"≤{.05 * atr:.8g} (0,05 ATR)", bool(spread_ok))
    deviation = None if spot else abs(snap.mark - snap.last)
    if spot:
        r.checks.append({"Kriteria": "Deviasi Mark–Last", "Nilai aktual": "Tidak tersedia pada spot (Harga Spot ≠ Mark Price Futures)",
                         "Syarat": "PAPER berbasis Last spot; bukan verifikasi perpetual", "Status": "SIMULASI"})
    else:
        r.check("Deviasi Mark–Last", f"{deviation:.8g}", f"≤{.25 * atr:.8g}", deviation <= .25 * atr)
    if any(x["Status"] not in ("LULUS", "SIMULASI") for x in r.checks):
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res

    tick = spec.tick
    trigger_base = quantize(float(signal.high if sign == 1 else signal.low), tick, up=sign == 1)
    trigger = float(Decimal(str(trigger_base)) + sign * Decimal(str(tick)))
    entry = quantize(trigger + sign * .10 * atr, tick, up=sign == -1)
    sl_base = e.low.iloc[-5:].min() - .25 * atr if sign == 1 else e.high.iloc[-5:].max() + .25 * atr
    sl = quantize(float(sl_base), tick, up=sign == -1)
    distance = float(sign * (Decimal(str(entry)) - Decimal(str(sl))))

    r.check("Jarak SL struktural", f"{distance / atr:.3f} ATR", "1–3 ATR; 5 candle termasuk sinyal", atr <= distance <= 3 * atr)
    r.check("Harga sudah menembus trigger", f"Last {snap.last:.8g}; trigger {trigger:.8g}", "Belum pernah menembus sejak candle sinyal", sign * (snap.last - trigger) < 0,
            missing=sign * (snap.last - trigger) >= 0)
    intrabar = snap.open_bar_high if sign == 1 else snap.open_bar_low
    r.check("Riwayat trigger sejak sinyal", intrabar, "High/low candle berjalan belum menyentuh trigger", intrabar is not None and sign * (intrabar - trigger) < 0,
            intrabar is None or sign * (intrabar - trigger) >= 0)
    r.check("SL sebelum fill", snap.last, f"Last belum melewati {sl:.8g}", sign * (snap.last - sl) > 0)
    adverse_last = snap.open_bar_low if sign == 1 else snap.open_bar_high
    adverse_mark = adverse_last if spot else (snap.open_mark_low if sign == 1 else snap.open_mark_high)
    path_available = finite(adverse_last) and finite(adverse_mark)
    r.check("SL belum tersentuh sejak sinyal", f"Ekstrem Last spot {adverse_last}" if spot else f"Ekstrem Last {adverse_last}; Mark {adverse_mark}",
            "Last spot belum mencapai SL" if spot else "Last dan Mark belum mencapai SL, termasuk harga yang sudah memantul",
            path_available and sign * (adverse_last - sl) > 0 and sign * (adverse_mark - sl) > 0 and sign * (reference - sl) > 0,
            not path_available)
    levels = confirmed_pivots(b) + confirmed_pivots(e)
    day_ok = all(finite(x) and x > 0 for x in (snap.previous_day_high, snap.previous_day_low))
    r.check("High/low hari UTC sebelumnya", f"{snap.previous_day_high} / {snap.previous_day_low}", "Data sumber market hari sebelumnya tersedia", day_ok, not day_ok)
    levels += [x for x in (snap.previous_day_high, snap.previous_day_low) if finite(x)]
    obstacles = [x for x in levels if sign * (x - entry) > 0]
    obstacle = min(obstacles, key=lambda x: abs(x - entry)) if obstacles else None
    room = abs(obstacle - entry) if obstacle is not None else float("inf")
    req_room = 2 * distance + .10 * atr
    r.check("Ruang target", f"{room:.8g}" if obstacle else "Tidak teridentifikasi dalam cakupan data",
            f"≥{req_room:.8g}", room >= req_room)
    now_day = datetime.fromtimestamp(snap.now_ms / 1000, timezone.utc).date().isoformat()
    numbers = [account.equity, account.free_margin, account.day_start_equity, account.net_cash_flow,
               account.risk_pct, account.leverage, account.total_existing_risk, account.correlated_alt_risk,
               account.active_pending_count, account.consecutive_losses]
    valid_account = all(finite(x) for x in numbers) and account.equity > 0 and account.day_start_equity > 0 and account.free_margin >= 0
    valid_account = valid_account and all(x >= 0 for x in [account.total_existing_risk, account.correlated_alt_risk, account.active_pending_count, account.consecutive_losses])
    valid_account = valid_account and account.correlated_alt_risk <= account.total_existing_risk
    r.check("Data akun", "Buku saldo virtual PAPER" if paper else ("Dikonfirmasi pengguna" if account.account_verified else "Belum dikonfirmasi"), "Ekuitas, margin, posisi + pending, risiko + unrealized PnL", account.account_verified and valid_account,
            not account.account_verified or not valid_account)
    r.check("Hari akun UTC", account.account_day_utc, now_day, account.account_day_utc == now_day, account.account_day_utc != now_day)
    max_lev_allowed = 20 if getattr(account, "confidence_calibrated", False) else 3
    lev_valid = (0 < account.risk_pct <= .5 and isinstance(account.leverage, (int, np.integer)) and
                 1 <= account.leverage <= max_lev_allowed and
                 (account.leverage <= 3 or account.leverage in DYNAMIC_LEVERAGE_OPTIONS))
    r.check("Risiko dan leverage", f"{account.risk_pct}% / {account.leverage}x",
            f"0 < risiko ≤0,5%; leverage 1–{max_lev_allowed}x (dinamis terkalibrasi maks 20x)", lev_valid)
    r.check("Mode margin", f"Isolated={account.isolated}; auto-add={account.auto_add_margin}", "Isolated; auto-add mati", account.isolated and not account.auto_add_margin, not account.account_verified)
    drawdown = (account.day_start_equity + account.net_cash_flow - account.equity) / account.day_start_equity if valid_account else float("nan")
    r.check("Batas rugi harian UTC", f"{drawdown * 100:.2f}% / {account.consecutive_losses} rugi beruntun", "<2% dan <3 trade rugi beruntun", drawdown < .02 and account.consecutive_losses < 3, not valid_account)
    r.check("Posisi + pending", account.active_pending_count, "<2 sebelum rencana baru", account.active_pending_count < 2)
    costs = [account.taker_fee, account.exit_slippage_bps, account.funding_reserve_rate]
    costs_ok = account.costs_verified and all(finite(x) and x >= 0 for x in costs) and account.taker_fee < 1
    r.check("Biaya akun dan cadangan", str(costs), "Asumsi biaya PAPER (bukan fee aktual akun)" if paper else "Fee taker terverifikasi; slippage exit; total funding selama horizon", costs_ok, not costs_ok)
    funding_ok = not spot and finite(snap.funding_rate) and finite(snap.next_funding_ms) and snap.next_funding_ms > snap.now_ms
    if spot:
        r.checks.append({"Kriteria": "Funding kontrak", "Nilai aktual": "Tidak tersedia pada spot; cadangan biaya adalah asumsi simulasi (bukan data aktual)",
                         "Syarat": "Tidak mengklaim rate/jadwal funding perpetual terverifikasi", "Status": "SIMULASI"})
    else:
        r.check("Funding kontrak", f"rate={snap.funding_rate}; berikut={snap.next_funding_ms}; interval={snap.funding_interval_hours}", "Rate dan waktu funding resmi tersedia; interval tidak diasumsikan", funding_ok, not funding_ok)
    news_ok = (account.news_verified_at_ms is not None and 0 <= snap.now_ms - account.news_verified_at_ms <= 3600000 and
               bool(account.news_source.strip()) and account.news_clear_from_ms is not None and account.news_clear_until_ms is not None and
               account.news_clear_from_ms <= snap.now_ms - 900000 and account.news_clear_until_ms >= snap.now_ms + 900000)
    r.check("Filter berita CPI/FOMC", account.news_source if news_ok else "Filter berita belum terverifikasi", "Kalender dicek ≤1 jam; tidak ada event dalam ±15 menit", news_ok, not news_ok)
    r.check("Mekanisme order bursa", "Simulator polling; full entry fill konservatif" if paper else ("Dikonfirmasi" if account.order_semantics_verified else "Belum diverifikasi"), "Entry dan SL simulasi memakai Last spot" if spot else "Stop-limit Last; SL stop-market Mark; reduce-only; partial fill", account.order_semantics_verified, not account.order_semantics_verified)
    if not valid_account or not account.account_verified or not costs_ok or distance <= 0 or any(x["Status"] == "GAGAL" for x in r.checks):
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res
    fee = account.taker_fee
    slip = entry * account.exit_slippage_bps / 10000
    funding = entry * account.funding_reserve_rate
    csl = entry * fee + (sl + slip) * fee + slip + funding
    r.check("Biaya terhadap D", f"{csl:.8g}", f"≤{.20 * distance:.8g}", csl <= .20 * distance)
    if funding_ok:
        minimum_funding = max(0, sign * snap.funding_rate)
        r.check("Cadangan funding minimum", account.funding_reserve_rate, f"≥ pembayaran satu event rate saat ini {minimum_funding:.8g}; horizon dikonfirmasi pengguna", account.funding_reserve_rate >= minimum_funding)

    confidence_score, confidence_breakdown = compute_confidence(
        b, e, direction, bool(touched), bool(protected), bool(confirmed), bool(vwap_ok), bool(spread_ok)
    )
    if getattr(account, "confidence_override", None) is not None:
        confidence_score = int(account.confidence_override)
    sl_pct = distance / entry if entry > 0 else 0.0
    atr_pct = atr / entry if entry > 0 else 0.0
    calibrated = bool(getattr(account, "confidence_calibrated", False))
    min_conf_threshold = int(prof["entry_confidence_threshold"])
    max_prof_lev = int(prof["max_profile_leverage"]) if not calibrated else None
    selected_lev, lev_caps = select_dynamic_leverage(
        confidence_score,
        atr_pct,
        sl_pct,
        account.leverage,
        calibrated=calibrated,
        min_confidence_threshold=min_conf_threshold,
        max_profile_leverage=max_prof_lev,
    )
    effective_leverage = int(selected_lev)
    r.metrics["confidence_score"] = confidence_score
    r.metrics["confidence_breakdown"] = confidence_breakdown
    r.metrics["entry_confidence_threshold"] = min_conf_threshold
    r.metrics["max_allowed_leverage"] = lev_caps["max_allowed_leverage"]
    r.metrics["selected_leverage"] = effective_leverage
    r.check(
        "Confidence & leverage dinamis",
        f"Confidence {confidence_score}/100 (ambang {prof['name']} ≥{min_conf_threshold}); leverage efektif {effective_leverage}x (batas maks {lev_caps['max_allowed_leverage']}x; pilihan operator {account.leverage}x)",
        f"Confidence ≥{min_conf_threshold}; leverage efektif ≤ batas confidence, volatilitas, likuidasi, kalibrasi, dan operator",
        bool(
            confidence_score >= min_conf_threshold
            and lev_caps.get("allowed", False)
            and 1 <= effective_leverage <= lev_caps["max_allowed_leverage"]
            and effective_leverage <= account.leverage
        ),
    )
    if confidence_score < min_conf_threshold or effective_leverage < 1 or any(x["Status"] == "GAGAL" for x in r.checks):
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res

    # BUG-48: Lower PAPER risk per trade for more active profiles (e.g. BALANCED <= 0.35%, ACTIVE <= 0.25%)
    effective_risk_pct = min(float(account.risk_pct), float(prof["paper_risk_pct_cap"])) if (paper and is_relaxed_or_balanced) else float(account.risk_pct)
    risk_budget = account.equity * effective_risk_pct / 100
    remaining = account.equity * .01 - account.total_existing_risk
    if snap.symbol != "BTCUSDT":
        remaining = min(remaining, account.equity * .005 - account.correlated_alt_risk)
    effective_budget = max(0, min(risk_budget, remaining))
    step = common_step(spec.step, spec.market_step)
    qty = quantize(max(0, min(effective_budget / (distance + csl), account.equity * .2 * effective_leverage / entry,
                             account.free_margin / (entry / effective_leverage + csl), spec.max_qty, spec.market_max_qty)), step)

    distance, tp1, tp2, tp3, q1, q2, qr = compute_tp_targets(entry, sl, direction, tick, qty, step)

    actual_risk = qty * (distance + csl)
    r.check("Anggaran risiko tersedia", f"B={risk_budget:.4f}; tersedia={effective_budget:.4f}; risiko={actual_risk:.4f}", "Per trade ≤0,5%; total ≤1%; grup altcoin searah ≤0,5%", effective_budget > 0 and actual_risk <= risk_budget + 1e-8)
    minqty = max(spec.min_qty, spec.market_min_qty)
    valid_parts = all(q >= minqty and q * min(entry, sl, tp1, tp2) >= spec.min_notional for q in (q1, q2, qr))
    r.check("Lot dan TP parsial", f"Q={qty:.8g}; TP1={q1:.8g}; TP2={q2:.8g}; runner={qr:.8g}", "Step valid; setiap bagian memenuhi minQty dan minNotional (konservatif)", valid_parts)
    margin = qty * entry / effective_leverage
    r.check("Margin dan biaya", f"Margin {margin:.4f}; cadangan {qty * csl:.4f}", "Margin ≤20% ekuitas; margin + biaya ≤margin bebas", margin <= account.equity * .2 + 1e-8 and margin + qty * csl <= account.free_margin + 1e-8 and qty > 0)
    price_ok = all(spec.min_price <= p <= spec.max_price and p > 0 for p in (trigger, entry, sl, tp1, tp2))
    if not spot:
        price_ok = price_ok and all(snap.mark * spec.multiplier_down <= p <= snap.mark * spec.multiplier_up for p in (entry, tp1, tp2))
    r.check("Filter harga bursa", f"E={entry:.8g}; TP1={tp1:.8g}; TP2={tp2:.8g}", "PRICE_FILTER spot untuk simulasi" if spot else "PRICE_FILTER dan PERCENT_PRICE", price_ok)
    tp_cost = entry * fee + (tp1 + slip) * fee + slip + funding
    rr = (abs(tp1 - entry) - tp_cost) / (distance + csl)
    r.check("RR bersih TP1", f"{rr:.3f}", "≥1,5 jika seluruh posisi keluar TP1 (metrik pembanding)", rr >= 1.5)
    try:
        if not all(finite(float(p)) and finite(float(q)) and float(p) > 0 and float(q) >= 0
                   for p, q in snap.asks + snap.bids):
            raise ValueError("Non-finite order book")
        entry_depth = sum(float(q) for p, q in (snap.asks if sign == 1 else snap.bids)
                          if abs(float(p) - (snap.ask if sign == 1 else snap.bid)) <= .10 * atr)
        exit_depth = sum(float(q) for p, q in (snap.bids if sign == 1 else snap.asks)
                         if abs(float(p) - (snap.bid if sign == 1 else snap.ask)) <= slip)
        depth_ok = all(finite(x) and x >= qty > 0 for x in (entry_depth, exit_depth))
    except (ValueError, TypeError):
        entry_depth = exit_depth = 0
        depth_ok = False
    r.check("Likuiditas snapshot", f"Entry depth {entry_depth:.8g}; exit depth {exit_depth:.8g}; Q {qty:.8g}", "Kedalaman mencukupi dalam toleransi; bukan jaminan fill", depth_ok, not bool(snap.bids and snap.asks))

    expires = compute_order_expiry_ms(signal.close_time, exec_tf, snap.now_ms, snap.mode, news_ok=news_ok, news_clear_until_ms=account.news_clear_until_ms)

    estimated_margin_loss_pct = (actual_risk / margin * 100.0) if margin > 0 else 0.0
    estimated_fees_usdt = qty * csl
    calibration_status = "CALIBRATED" if calibrated else f"UNCALIBRATED (MAX {lev_caps['calibration_cap']}x)"
    plan = dict(symbol=snap.symbol, mode=snap.mode, direction=direction, signal_close_ms=int(signal.close_time),
                entry_trigger=trigger, entry=entry, entry_type="STOP-LIMIT; trigger Last", sl=sl,
                sl_type="STOP-MARKET; trigger Mark; reduce-only/close-position sesuai bursa", atr=atr, distance=distance,
                sl_distance_pct=sl_pct * 100.0,
                tp1=tp1, tp2=tp2, tp3=tp3, qty=qty, tp1_qty=q1, tp2_qty=q2, runner_qty=qr,
                leverage=effective_leverage, max_allowed_leverage=lev_caps["max_allowed_leverage"],
                confidence_score=confidence_score, confidence_calibration_status=calibration_status,
                confidence_breakdown=confidence_breakdown, leverage_caps=lev_caps,
                strategy_profile=prof["name"], effective_risk_pct=effective_risk_pct,
                m5_confirmations_passed=confluence["core_passed"],
                margin=margin, estimated_margin_loss_pct=estimated_margin_loss_pct,
                estimated_fees_usdt=estimated_fees_usdt,
                notional=qty * entry, equity=account.equity,
                risk_budget=risk_budget, risk=actual_risk, fee=fee, cost_sl_per_unit=csl,
                funding_per_unit=funding, slippage_per_unit=slip, net_rr_tp1=rr, expires_ms=expires,
                quantity_step=step, tick=tick, execution_tf=exec_tf, obstacle=obstacle,
                min_qty=minqty, min_notional=spec.min_notional,
                liquidation=None, liquidation_buffer=None,
                liquidation_safety_check="SIMULASI PAPER (SL < Jarak Likuidasi Teoritis)" if paper else "PENDING_VERIFICATION",
                relaxed_mode=is_relaxed_or_balanced,
                cancellation=["Harga mencapai SL sebelum fill", "Bias atau filter biaya/likuiditas berubah",
                              "Order kedaluwarsa atau memasuki jendela berita", "Jangan ganti stop-limit yang tidak fill menjadi market"],
                exit_rules="TP1 50% (2R); TP2 25% (3R); TP3 25% Runner (Target Eksekusi Otomatis 4R + Trailing Stop 2 ATR setelah TP2); BE biaya setelah TP1; time-exit 12 candle tanpa +1R",
                **val_meta)
    plan["instrument_kind"] = snap.instrument_kind
    if spot:
        plan["sl_type"] = "STOP simulasi; trigger Last spot (Harga Spot ≠ Mark Price Futures)"
        plan["source"] = snap.source
    plan["id"] = fingerprint(plan)
    if paper:
        plan["scope"] = "PAPER"
    if any(x["Status"] == "GAGAL" for x in r.checks):
        res = r.finish()
        res.meta["rejection_category"] = classify_result_rejection_category(res)
        return res
    r.candidate = plan
    liq_ok = (finite(account.liquidation_price) and account.liquidation_price > 0 and
              account.liquidation_plan_id == plan["id"] and bool(account.liquidation_source.strip()) and
              account.liquidation_checked_ms is not None and
              0 <= snap.now_ms - account.liquidation_checked_ms <= 300000)
    if paper:
        r.checks.append({"Kriteria": "Likuidasi sesuai rencana", "Nilai aktual": "Tidak dimodelkan; harga likuidasi tidak dikarang",
                         "Syarat": "PAPER saja; untuk live tetap wajib verifikasi bursa", "Status": "SIMULASI"})
    else:
        r.check("Likuidasi sesuai rencana", account.liquidation_price if liq_ok else "Belum diverifikasi untuk ID rencana ini", "Hasil bursa/kalkulator; mode/risk tier/Q/leverage tepat; ≤5 menit", liq_ok, not liq_ok)
    if liq_ok and not paper:
        buffer = sign * (sl - account.liquidation_price)
        minimum = max(atr, deviation + slip)
        r.check("Buffer likuidasi", f"{buffer:.8g}", f"SL sebelum likuidasi; buffer ≥{minimum:.8g}", buffer >= minimum)
        liq_safe = buffer >= minimum
        plan.update(liquidation=account.liquidation_price, liquidation_buffer=buffer,
                    liquidation_safety_check="PASSED" if liq_safe else "FAILED")
    r.check("Masa berlaku", expires, "Waktu sekarang < kedaluwarsa", snap.now_ms < expires, snap.now_ms >= expires)
    res = r.finish()
    res.meta["rejection_category"] = classify_result_rejection_category(res)
    return res

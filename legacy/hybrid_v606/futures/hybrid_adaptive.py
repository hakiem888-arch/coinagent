"""CoinAgentX — Hybrid Adaptive Strategy Specification v1.0 (`Strategy F`).

Implements Deliverables 1–6 of `CoinAgentX_Hybrid_Adaptive_Spec_v1.0.md`:
- Deliverable 1: M15 Regime Detector (`detect_m15_regime`) with strict precedence:
  `HIGH_VOLATILITY` -> `BREAKOUT_UP/DOWN` -> `STRONG_TREND_UP/DOWN` -> `TREND_UP/DOWN` -> `RANGE` -> `TRANSITION`.
- Deliverable 2: Deterministic Strategy Router (`route_strategy_by_regime`) & persistent breakout/sweep
  arming state tracker (`ArmingStateStore`), with `CONFLICT_WAIT` protection.
- Deliverable 3: Isolated Strategy Evaluators (`evaluate_strategy_a` .. `evaluate_strategy_e`) for:
  - Strategy `A`: Trend Pullback (Section 5)
  - Strategy `B`: Breakout Retest (Section 6)
  - Strategy `C`: Momentum Scalping (Section 7)
  - Strategy `D`: Mean Reversion (Section 8)
  - Strategy `E`: Liquidity Sweep Reversal (Section 9)
- Deliverable 4: 100-Point Setup Quality Score (`compute_setup_quality_score`, Section 10) and Stage
  Classifier (`EARLY_WATCH` 55–64, `SETUP_FORMING` 65–77, `ENTRY_CANDIDATE` / `PAPER_ENTRY` >= 78).
- Deliverable 5: Mandatory Hard Gates (`evaluate_hybrid_hard_gates`, Section 11), Simulated `STOP-LIMIT`
  Entry Order with `MISSED_NO_CHASE` & 2-M5-bar expiry (`build_hybrid_entry_order`, Section 12), and
  Exit / Partial TP (`40% +1R / 35% +2R / 25% +3R` + lot fallback) / Net BE / `+1.5R` Trailing (`1.25 ATR`)
  / `6`-bar & `12`-bar Time Stops / `CONSERVATIVE_INTRABAR_APPROXIMATION` (`Section 13`).
- Deliverable 6: Groq Advisor-Only mode (`evaluate_hybrid_ai_advisor`, Section 14), Stage-Aware Telegram
  Templates & Deduplication (`Section 15`), and 50/30/10/5 Rotating Pipeline & 17-Metric Funnel (`Section 16`).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import numpy as np
import pandas as pd

from .indicators import confirmed_pivots, confirmed_swing_pivots, indicators
from .models import INTERVAL_MS, Result, TIMEFRAMES
from .version import APP_VERSION

REGIME_HIGH_VOLATILITY = "HIGH_VOLATILITY"
REGIME_BREAKOUT_UP = "BREAKOUT_UP"
REGIME_BREAKOUT_DOWN = "BREAKOUT_DOWN"
REGIME_STRONG_TREND_UP = "STRONG_TREND_UP"
REGIME_STRONG_TREND_DOWN = "STRONG_TREND_DOWN"
REGIME_TREND_UP = "TREND_UP"
REGIME_TREND_DOWN = "TREND_DOWN"
REGIME_RANGE = "RANGE"
REGIME_TRANSITION = "TRANSITION"

ALL_REGIMES = (
    REGIME_HIGH_VOLATILITY,
    REGIME_BREAKOUT_UP,
    REGIME_BREAKOUT_DOWN,
    REGIME_STRONG_TREND_UP,
    REGIME_STRONG_TREND_DOWN,
    REGIME_TREND_UP,
    REGIME_TREND_DOWN,
    REGIME_RANGE,
    REGIME_TRANSITION,
)

STRATEGY_A = "A"  # Trend Pullback
STRATEGY_B = "B"  # Breakout Retest
STRATEGY_C = "C"  # Momentum Scalping
STRATEGY_D = "D"  # Mean Reversion
STRATEGY_E = "E"  # Liquidity Sweep Reversal
STRATEGY_F = "F"  # Hybrid Adaptive Router

STRATEGY_NAMES = {
    STRATEGY_A: "Trend Pullback",
    STRATEGY_B: "Breakout Retest",
    STRATEGY_C: "Momentum Scalping",
    STRATEGY_D: "Mean Reversion",
    STRATEGY_E: "Liquidity Sweep Reversal",
    STRATEGY_F: "Hybrid Adaptive",
}

DEFAULT_MIN_CANDLES_M15 = 250
DEFAULT_MIN_CANDLES_M5 = 250
M5_INTERVAL_MS = INTERVAL_MS["5m"]
M15_INTERVAL_MS = INTERVAL_MS["15m"]
COOLDOWN_CONSECUTIVE_LOSSES_MS = 3_600_000  # 60 minutes after 3 consecutive closed losses


def finite(value) -> bool:
    return isinstance(value, (float, int, np.number)) and not isinstance(value, bool) and math.isfinite(float(value))


def quantize(value, step, up=False) -> float:
    val_d, step_d = Decimal(str(value)), Decimal(str(step))
    if step_d <= 0:
        raise ValueError("Step harus positif")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return float((val_d / step_d).to_integral_value(rounding=rounding) * step_d)


def common_step(a, b) -> float:
    da, db = Decimal(str(a)), Decimal(str(b))
    scale = 10 ** max(0, -da.as_tuple().exponent, -db.as_tuple().exponent)
    return math.lcm(int(da * scale), int(db * scale)) / scale


def break_even_price(entry, direction, fee, funding_per_unit, slippage_per_unit, entry_fee_per_unit=None) -> float:
    """Fee-adjusted net break-even price covering entry fee, exit fee, funding reserve, and exit slippage."""
    entry_fee = entry * fee if entry_fee_per_unit is None else entry_fee_per_unit
    if direction in ("LONG", "BUY"):
        return (entry + entry_fee + funding_per_unit) / (1.0 - fee) + slippage_per_unit
    return (entry - entry_fee - funding_per_unit) / (1.0 + fee) - slippage_per_unit


# ============================================================================
# SECTION 2: INSTRUMENT & CLOSED-CANDLE HISTORY VALIDATION
# ============================================================================

def validate_hybrid_instrument(snap, spec=None, allow_spot_watchlist_only=False) -> dict:
    """Section 2.1 & 11.1: Active USDT-M linear perpetual contract check.
    `SPOT_REFERENCE` is NEVER eligible to create a Futures PAPER entry.
    """
    kind = getattr(snap, "instrument_kind", "USD_M_PERPETUAL")
    if kind == "SPOT_REFERENCE":
        return {
            "ok": bool(allow_spot_watchlist_only),
            "eligible_for_futures_paper": False,
            "instrument_kind": kind,
            "status": "SPOT_REFERENCE_WATCHLIST_ONLY" if allow_spot_watchlist_only else "SPOT_REFERENCE_BLOCKED_FOR_FUTURES_PAPER",
            "label": "SPOT_REFERENCE (NOT FUTURES PERPETUAL)",
            "reason": "SPOT_REFERENCE tidak pernah diizinkan membuat entri PAPER Futures Perpetual.",
        }
    contract = spec or getattr(snap, "contract", None)
    if contract is None:
        return {
            "ok": False,
            "eligible_for_futures_paper": False,
            "instrument_kind": kind,
            "status": "MISSING_CONTRACT_SPEC",
            "label": str(kind),
            "reason": "Spesifikasi kontrak tidak tersedia.",
        }
    specs_ok = all(
        finite(getattr(contract, attr, None)) and float(getattr(contract, attr)) > 0
        for attr in ("tick", "step", "min_qty", "max_qty", "min_notional", "market_step", "market_min_qty", "market_max_qty")
    )
    linear_ok = (
        getattr(contract, "quote_asset", "") == "USDT"
        and getattr(contract, "contract_type", "") == "PERPETUAL"
        and getattr(contract, "status", "") == "TRADING"
    )
    ok = bool(specs_ok and linear_ok)
    return {
        "ok": ok,
        "eligible_for_futures_paper": ok,
        "instrument_kind": kind,
        "status": "OK" if ok else "INVALID_PERPETUAL_CONTRACT",
        "label": f"{kind} (USDT-M LINEAR PERPETUAL)",
        "reason": "" if ok else "Kontrak bukan USDT-M Linear Perpetual berstatus TRADING atau spesifikasi lot/tick tidak lengkap.",
    }


def filter_closed_candles(frame: pd.DataFrame, interval: str, now_ms: int) -> pd.DataFrame:
    """Section 2.2: Ensure only strictly closed candles according to exchange server time are included."""
    if frame is None or frame.empty or "close_time" not in frame.columns:
        return pd.DataFrame() if frame is None else frame.iloc[0:0].copy()
    closed = frame[frame["close_time"] < int(now_ms)].copy()
    return closed.reset_index(drop=True)


def validate_hybrid_candle_history(
    frame: pd.DataFrame,
    interval: str,
    now_ms: int,
    min_candles: int = DEFAULT_MIN_CANDLES_M15,
) -> dict:
    """Section 2.2 & 2.3: Require `>= min_candles` (default 250) closed, contiguous, finite candles
    whose latest candle is closed for the current interval according to exchange server time.
    """
    required_cols = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
    if frame is None or not set(required_cols).issubset(frame.columns):
        return {"ok": False, "code": "MISSING_COLUMNS", "reason": "Kolom OHLCV tidak lengkap"}
    if interval not in INTERVAL_MS:
        return {"ok": False, "code": "INVALID_INTERVAL", "reason": f"Interval {interval} tidak didukung"}

    step = INTERVAL_MS[interval]
    if len(frame) > 0 and int(frame["close_time"].iloc[-1]) >= int(now_ms):
        return {
            "ok": False,
            "code": "CANDLE_NOT_CLOSED",
            "reason": "Candle terakhir masih berjalan (belum ditutup menurut waktu server bursa)",
        }

    if len(frame) < int(min_candles):
        return {
            "ok": False,
            "code": "HISTORY_INSUFFICIENT",
            "reason": f"HISTORY_INSUFFICIENT: Hanya {len(frame)} candle tertutup; minimum {int(min_candles)}",
        }

    arr = frame[required_cols].to_numpy(dtype=float)
    if not np.isfinite(arr).all():
        return {"ok": False, "code": "NON_FINITE_OHLCV", "reason": "OHLCV memuat nilai NaN atau infinite"}

    diffs = frame["open_time"].diff().iloc[1:]
    if not (diffs == step).all():
        return {"ok": False, "code": "CANDLE_GAP_OR_DUPLICATE", "reason": "Candle duplikat, tidak berurutan, atau memiliki celah"}

    if not ((frame["open_time"] % step == 0) & (frame["close_time"] == frame["open_time"] + step - 1)).all():
        return {"ok": False, "code": "INVALID_CANDLE_BOUNDARY", "reason": "Batas waktu open_time/close_time candle tidak valid"}

    expected_last_close = (int(now_ms) // step) * step - 1
    if int(frame["close_time"].iloc[-1]) != expected_last_close:
        return {
            "ok": False,
            "code": "STALE_OR_UNCLOSED_LAST_CANDLE",
            "reason": "Candle terakhir selesai belum tersedia atau masih berjalan",
        }

    valid_ohlc = (
        (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
        & (frame["low"] > 0)
        & (frame["volume"] >= 0)
    )
    if not bool(valid_ohlc.all()):
        return {"ok": False, "code": "INVALID_OHLC_RELATIONS", "reason": "Relasi high/low/open/close/volume tidak valid"}

    return {"ok": True, "code": "OK", "reason": "", "candles": len(frame), "last_close_ms": int(frame["close_time"].iloc[-1])}


# ============================================================================
# SECTION 3: M15 REGIME DETECTOR (DELIVERABLE 1)
# ============================================================================

def detect_m15_regime(m15_frame: pd.DataFrame) -> dict:
    """Section 3: Classify M15 closed-candle frame into exactly one regime following strict precedence:
    1. `HIGH_VOLATILITY`
    2. `BREAKOUT_UP` / `BREAKOUT_DOWN`
    3. `STRONG_TREND_UP` / `STRONG_TREND_DOWN`
    4. `TREND_UP` / `TREND_DOWN`
    5. `RANGE`
    6. `TRANSITION`
    """
    d = m15_frame if ("ema50_slope_norm" in m15_frame.columns and "donchian20_high" in m15_frame.columns) else indicators(m15_frame)
    if len(d) < 30:
        return {
            "ok": False,
            "regime": REGIME_TRANSITION,
            "allow_new_entries": False,
            "reason": "HISTORY_INSUFFICIENT",
        }

    last = d.iloc[-1]
    prev = d.iloc[-2]
    prev2 = d.iloc[-3]

    req_fields = [
        last.close,
        last.open,
        last.high,
        last.low,
        last.tr,
        last.atr,
        prev.atr,
        last.adx,
        prev.adx,
        prev2.adx,
        last.plus_di,
        last.minus_di,
        last.ema20,
        last.ema50,
        last.ema200,
        last.ema50_slope_norm,
        last.rvol20,
        last.donchian20_high,
        last.donchian20_low,
        last.bb_width,
    ]
    if not all(finite(x) for x in req_fields) or float(last.atr) <= 0 or float(prev.atr) <= 0:
        return {
            "ok": False,
            "regime": REGIME_TRANSITION,
            "allow_new_entries": False,
            "reason": "NON_FINITE_M15_INDICATORS",
        }

    close = float(last.close)
    high = float(last.high)
    low = float(last.low)
    tr_last = float(last.tr)
    atr_now = float(last.atr)
    atr_prev = float(prev.atr)
    adx_now = float(last.adx)
    adx_prev1 = float(prev.adx)
    adx_prev2 = float(prev2.adx)
    plus_di = float(last.plus_di)
    minus_di = float(last.minus_di)
    di_spread = plus_di - minus_di
    ema20 = float(last.ema20)
    ema50 = float(last.ema50)
    ema200 = float(last.ema200)
    slope_norm = float(last.ema50_slope_norm)
    rvol20 = float(last.rvol20)
    donch_hi = float(last.donchian20_high)
    donch_lo = float(last.donchian20_low)
    bb_width_now = float(last.bb_width)

    atr_hist_series = d["atr"].iloc[max(0, len(d) - 101) : -1].dropna()
    if len(atr_hist_series) >= 20:
        atr_arr = atr_hist_series.to_numpy(dtype=float)
        atr_p95 = float(np.percentile(atr_arr, 95))
        atr_med = float(np.median(atr_arr))
        effective_p95 = max(atr_p95, atr_med * 1.25)
    else:
        atr_p95 = float("inf")
        effective_p95 = float("inf")

    tr_spike = tr_last > 2.5 * atr_prev
    atr_extreme = len(atr_hist_series) >= 20 and atr_now > effective_p95
    is_high_vol = bool(tr_spike or atr_extreme)

    bar_range = max(1e-12, high - low)
    close_pos_in_range = (close - low) / bar_range if high > low else 0.5

    breakout_up = (
        not is_high_vol
        and close >= donch_hi + 0.05 * atr_now
        and rvol20 >= 1.30
        and high > low
        and close_pos_in_range >= 0.75
    )
    breakout_down = (
        not is_high_vol
        and close <= donch_lo - 0.05 * atr_now
        and rvol20 >= 1.30
        and high > low
        and close_pos_in_range <= 0.25
    )

    adx_rising_2bars = adx_now > adx_prev1 > adx_prev2
    strong_trend_up = (
        not is_high_vol
        and adx_now >= 25.0
        and adx_rising_2bars
        and di_spread >= 5.0
        and close > ema50
        and slope_norm >= 0.10
    )
    strong_trend_down = (
        not is_high_vol
        and adx_now >= 25.0
        and adx_rising_2bars
        and (-di_spread) >= 5.0
        and close < ema50
        and slope_norm <= -0.10
    )

    trend_up = (
        not is_high_vol
        and adx_now >= 18.0
        and close > ema50
        and slope_norm >= 0.10
        and plus_di > minus_di
    )
    trend_down = (
        not is_high_vol
        and adx_now >= 18.0
        and close < ema50
        and slope_norm <= -0.10
        and minus_di > plus_di
    )

    bb_hist = d["bb_width"].iloc[max(0, len(d) - 21) : -1].dropna()
    bb_median_20 = float(np.median(bb_hist.to_numpy(dtype=float))) if len(bb_hist) >= 5 else bb_width_now
    is_range = (
        not is_high_vol
        and not breakout_up
        and not breakout_down
        and adx_now < 18.0
        and abs(slope_norm) < 0.15
        and bb_width_now <= 1.15 * bb_median_20 + 1e-12
    )

    if is_high_vol:
        regime = REGIME_HIGH_VOLATILITY
    elif breakout_up:
        regime = REGIME_BREAKOUT_UP
    elif breakout_down:
        regime = REGIME_BREAKOUT_DOWN
    elif strong_trend_up:
        regime = REGIME_STRONG_TREND_UP
    elif strong_trend_down:
        regime = REGIME_STRONG_TREND_DOWN
    elif trend_up:
        regime = REGIME_TREND_UP
    elif trend_down:
        regime = REGIME_TREND_DOWN
    elif is_range:
        regime = REGIME_RANGE
    else:
        regime = REGIME_TRANSITION

    breakout_level = donch_hi if regime == REGIME_BREAKOUT_UP else (donch_lo if regime == REGIME_BREAKOUT_DOWN else None)
    return {
        "ok": True,
        "regime": regime,
        "allow_new_entries": regime != REGIME_HIGH_VOLATILITY,
        "candle_close_ms": int(last.close_time),
        "close": close,
        "atr14": atr_now,
        "atr14_prev": atr_prev,
        "atr_p95": atr_p95,
        "tr_last": tr_last,
        "tr_ratio": tr_last / atr_prev if atr_prev > 0 else 0.0,
        "adx14": adx_now,
        "adx_rising_2bars": bool(adx_rising_2bars),
        "plus_di": plus_di,
        "minus_di": minus_di,
        "di_spread": abs(di_spread),
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "ema50_slope_norm": slope_norm,
        "rvol20": rvol20,
        "donchian20_high": donch_hi,
        "donchian20_low": donch_lo,
        "breakout_level": breakout_level,
        "bb_width": bb_width_now,
        "bb_width_median20": bb_median_20,
        "close_pos_in_range": close_pos_in_range,
    }


# ============================================================================
# SECTION 4: PERSISTENT BREAKOUT & SWEEP ARMING STATE + ROUTER (DELIVERABLE 2)
# ============================================================================

class ArmingStateStore:
    """Section 4 & 6 & 9: Tracks armed M15 breakout states (valid for 6 completed M5 candles = 30m)
    and armed M5 liquidity sweep states (valid for 5 completed M5 candles).
    """

    def __init__(self, initial_state: dict | None = None):
        self._state: dict[str, dict] = copy.deepcopy(initial_state) if isinstance(initial_state, dict) else {}

    def to_dict(self) -> dict:
        return copy.deepcopy(self._state)

    def arm_breakout(self, symbol: str, direction: str, level: float, m15_close_ms: int, m15_atr: float):
        sym = str(symbol).upper()
        entry = self._state.setdefault(sym, {})
        entry["breakout"] = {
            "direction": str(direction).upper(),
            "level": float(level),
            "armed_at_ms": int(m15_close_ms),
            "expires_at_ms": int(m15_close_ms) + 6 * M5_INTERVAL_MS,
            "m15_atr": float(m15_atr),
        }

    def get_active_breakout(self, symbol: str, current_m5_close_ms: int) -> dict | None:
        sym = str(symbol).upper()
        bo = (self._state.get(sym) or {}).get("breakout")
        if not bo:
            return None
        if int(current_m5_close_ms) > int(bo["expires_at_ms"]) or int(current_m5_close_ms) < int(bo["armed_at_ms"]):
            return None
        return dict(bo)

    def clear_breakout(self, symbol: str):
        sym = str(symbol).upper()
        if sym in self._state and "breakout" in self._state[sym]:
            self._state[sym].pop("breakout", None)

    def arm_sweep(self, symbol: str, direction: str, swept_level: float, sweep_extreme: float, sweep_close_ms: int, m5_atr: float):
        sym = str(symbol).upper()
        entry = self._state.setdefault(sym, {})
        entry["sweep"] = {
            "direction": str(direction).upper(),
            "swept_level": float(swept_level),
            "sweep_extreme": float(sweep_extreme),
            "armed_at_ms": int(sweep_close_ms),
            "expires_at_ms": int(sweep_close_ms) + 5 * M5_INTERVAL_MS,
            "m5_atr": float(m5_atr),
        }

    def get_active_sweep(self, symbol: str, current_m5_close_ms: int) -> dict | None:
        sym = str(symbol).upper()
        sw = (self._state.get(sym) or {}).get("sweep")
        if not sw:
            return None
        if int(current_m5_close_ms) > int(sw["expires_at_ms"]) or int(current_m5_close_ms) < int(sw["armed_at_ms"]):
            return None
        return dict(sw)


GLOBAL_ARMING_STORE = ArmingStateStore()


# ============================================================================
# SECTIONS 5–9: ISOLATED STRATEGY MODULES A–E (DELIVERABLE 3)
# ============================================================================

def _check_strategy_indicator_finiteness(m5_d: pd.DataFrame, required_cols: tuple[str, ...]) -> tuple[bool, list[str]]:
    """Section 2.4 & Acceptance Test 14: Verify required M5 indicator values are finite on the latest bars."""
    if m5_d is None or len(m5_d) < 6:
        return False, ["M5_HISTORY_TOO_SHORT"]
    last = m5_d.iloc[-1]
    prev = m5_d.iloc[-2]
    missing = []
    for col in required_cols:
        if col not in m5_d.columns or not finite(last[col]) or not finite(prev[col]):
            missing.append(col)
    if not finite(last.get("atr", np.nan)) or float(last["atr"]) <= 0:
        missing.append("atr")
    return len(missing) == 0, missing


def evaluate_strategy_a(m15_regime: dict, m5_d: pd.DataFrame) -> dict:
    """Section 5: Strategy A — Trend Pullback (Primary Workhorse)."""
    regime = m15_regime.get("regime")
    if regime in (REGIME_TREND_UP, REGIME_STRONG_TREND_UP):
        direction = "LONG"
        side = "BUY"
        sign = 1
    elif regime in (REGIME_TREND_DOWN, REGIME_STRONG_TREND_DOWN):
        direction = "SHORT"
        side = "SELL"
        sign = -1
    else:
        return {
            "strategy": STRATEGY_A,
            "strategy_name": STRATEGY_NAMES[STRATEGY_A],
            "eligible_regime": False,
            "pullback_zone_active": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": f"Regime {regime} tidak mengizinkan Strategy A (Trend Pullback).",
        }

    ok_fin, missing = _check_strategy_indicator_finiteness(
        m5_d, ("ema20", "ema50", "rsi", "atr", "vwap", "rvol20")
    )
    if not ok_fin:
        return {
            "strategy": STRATEGY_A,
            "strategy_name": STRATEGY_NAMES[STRATEGY_A],
            "eligible_regime": True,
            "pullback_zone_active": False,
            "triggered": False,
            "direction": direction,
            "side": side,
            "non_finite_block": True,
            "missing_indicators": missing,
            "reason": f"Indikator M5 non-finite: {', '.join(missing)}",
        }

    sig = m5_d.iloc[-1]
    prev = m5_d.iloc[-2]
    window6 = m5_d.iloc[-6:]
    atr = float(sig.atr)

    ema_aligned = float(sig.ema20) > float(sig.ema50) if sign == 1 else float(sig.ema20) < float(sig.ema50)
    touched_mask = (window6["low"] <= window6["ema20"] + 0.30 * window6["atr"]) & (
        window6["high"] >= window6["ema20"] - 0.30 * window6["atr"]
    )
    pullback_touched = bool(touched_mask.any())

    if sign == 1:
        ema50_held = bool((window6["close"] >= window6["ema50"] - 0.15 * window6["atr"]).all())
    else:
        ema50_held = bool((window6["close"] <= window6["ema50"] + 0.15 * window6["atr"]).all())

    pullback_zone_active = bool(ema_aligned and pullback_touched and ema50_held)

    if sign == 1:
        candle_dir_ok = float(sig.close) > float(sig.open)
        breaks_prev_hl = float(sig.close) > float(prev.high)
        above_ema20 = float(sig.close) > float(sig.ema20)
    else:
        candle_dir_ok = float(sig.close) < float(sig.open)
        breaks_prev_hl = float(sig.close) < float(prev.low)
        above_ema20 = float(sig.close) < float(sig.ema20)

    body = abs(float(sig.close) - float(sig.open))
    bar_range = max(1e-12, float(sig.high) - float(sig.low))
    body_ok = body >= 0.25 * atr
    if float(sig.high) > float(sig.low):
        close_pos = (float(sig.close) - float(sig.low)) / bar_range
        close_pos_ok = (close_pos >= 0.70) if sign == 1 else (close_pos <= 0.30)
    else:
        close_pos = 0.5
        close_pos_ok = False

    trigger_candle_ok = bool(candle_dir_ok and breaks_prev_hl and above_ema20 and body_ok and close_pos_ok)

    if sign == 1:
        rsi_support = float(sig.rsi) >= 50.0 and float(sig.rsi) > float(prev.rsi)
        vwap_support = float(sig.close) > float(sig.vwap)
    else:
        rsi_support = float(sig.rsi) <= 50.0 and float(sig.rsi) < float(prev.rsi)
        vwap_support = float(sig.close) < float(sig.vwap)
    rvol_support = float(sig.rvol20) >= 0.90
    support_count = int(rsi_support) + int(vwap_support) + int(rvol_support)
    support_ok = support_count >= 2

    invalidation_level = float(window6["low"].min()) if sign == 1 else float(window6["high"].max())

    triggered = bool(pullback_zone_active and trigger_candle_ok and support_ok)
    conditions = {
        "ema20_ema50_aligned": bool(ema_aligned),
        "pullback_touched_ema20_030atr": bool(pullback_touched),
        "ema50_structure_held_015atr": bool(ema50_held),
        "confirmation_break_prev_hl_and_ema20": bool(candle_dir_ok and breaks_prev_hl and above_ema20),
        "confirmation_body_025atr_and_top30pct": bool(body_ok and close_pos_ok),
        "supporting_2of3": bool(support_ok),
        "rsi_support": bool(rsi_support),
        "vwap_support": bool(vwap_support),
        "rvol_support": bool(rvol_support),
    }
    passed_count = sum(
        1
        for k in (
            "ema20_ema50_aligned",
            "pullback_touched_ema20_030atr",
            "ema50_structure_held_015atr",
            "confirmation_break_prev_hl_and_ema20",
            "confirmation_body_025atr_and_top30pct",
            "supporting_2of3",
        )
        if conditions[k]
    )
    return {
        "strategy": STRATEGY_A,
        "strategy_name": STRATEGY_NAMES[STRATEGY_A],
        "eligible_regime": True,
        "pullback_zone_active": pullback_zone_active,
        "triggered": triggered,
        "direction": direction,
        "side": side,
        "invalidation_level": invalidation_level,
        "support_count": support_count,
        "passed_conditions": passed_count,
        "total_conditions": 6,
        "conditions": conditions,
        "body_atr": body / atr if atr > 0 else 0.0,
        "close_pos_in_range": close_pos,
        "reason": "" if triggered else "Menunggu konfirmasi pullback EMA20 M5 & candle konfirmasi Strategy A.",
    }


def evaluate_strategy_b(
    m15_regime: dict,
    m5_d: pd.DataFrame,
    armed_breakout: dict | None = None,
    current_price: float | None = None,
) -> dict:
    """Section 6: Strategy B — Breakout Retest."""
    regime = m15_regime.get("regime")
    if regime == REGIME_HIGH_VOLATILITY:
        return {
            "strategy": STRATEGY_B,
            "strategy_name": STRATEGY_NAMES[STRATEGY_B],
            "eligible_regime": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": "HIGH_VOLATILITY memblokir Strategy B.",
        }

    bo_info = None
    if regime in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
        bo_dir = "LONG" if regime == REGIME_BREAKOUT_UP else "SHORT"
        bo_level = m15_regime.get("breakout_level")
        bo_armed_ms = int(m15_regime.get("candle_close_ms") or 0)
        bo_info = {
            "direction": bo_dir,
            "level": float(bo_level),
            "armed_at_ms": bo_armed_ms,
            "expires_at_ms": bo_armed_ms + 6 * M5_INTERVAL_MS,
        }
    if armed_breakout is not None:
        bo_info = dict(armed_breakout)

    if not bo_info or not finite(bo_info.get("level")):
        return {
            "strategy": STRATEGY_B,
            "strategy_name": STRATEGY_NAMES[STRATEGY_B],
            "eligible_regime": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": "Tidak ada M15 breakout aktif atau armed dalam 6 candle M5 terakhir.",
        }

    direction = "LONG" if bo_info["direction"] in ("LONG", "BUY") else "SHORT"
    side = "BUY" if direction == "LONG" else "SELL"
    sign = 1 if direction == "LONG" else -1
    broken_level = float(bo_info["level"])
    armed_at_ms = int(bo_info["armed_at_ms"])
    expires_at_ms = int(bo_info["expires_at_ms"])

    ok_fin, missing = _check_strategy_indicator_finiteness(m5_d, ("atr", "rsi", "rvol20", "vwap"))
    if not ok_fin:
        return {
            "strategy": STRATEGY_B,
            "strategy_name": STRATEGY_NAMES[STRATEGY_B],
            "eligible_regime": True,
            "triggered": False,
            "direction": direction,
            "side": side,
            "non_finite_block": True,
            "missing_indicators": missing,
            "reason": f"Indikator M5 non-finite: {', '.join(missing)}",
        }

    sig = m5_d.iloc[-1]
    sig_close_ms = int(sig.close_time)
    atr = float(sig.atr)

    after_bo = m5_d[(m5_d["close_time"] > armed_at_ms) & (m5_d["close_time"] <= expires_at_ms)]
    within_6_m5_bars = 1 <= len(after_bo) <= 6 and sig_close_ms <= expires_at_ms and sig_close_ms > armed_at_ms

    if not within_6_m5_bars:
        return {
            "strategy": STRATEGY_B,
            "strategy_name": STRATEGY_NAMES[STRATEGY_B],
            "eligible_regime": True,
            "triggered": False,
            "direction": direction,
            "side": side,
            "broken_level": broken_level,
            "within_6_m5_bars": False,
            "reason": "Belum ada candle M5 retest setelah M15 breakout close (atau jendela 6 candle M5 telah kedaluwarsa).",
        }

    retest_zone_mask = (after_bo["low"] <= broken_level + 0.20 * after_bo["atr"]) & (
        after_bo["high"] >= broken_level - 0.20 * after_bo["atr"]
    )
    retest_touched = bool(retest_zone_mask.any())

    if sign == 1:
        reclaim_close_ok = float(sig.close) > broken_level and float(sig.close) > float(sig.open)
        retest_held_ok = bool((after_bo["close"] >= broken_level - 0.15 * after_bo["atr"]).all())
    else:
        reclaim_close_ok = float(sig.close) < broken_level and float(sig.close) < float(sig.open)
        retest_held_ok = bool((after_bo["close"] <= broken_level + 0.15 * after_bo["atr"]).all())

    ref_px = float(current_price) if finite(current_price) else float(sig.close)
    chase_dist = sign * (ref_px - broken_level)
    no_chase_ok = chase_dist <= 0.50 * atr

    if sign == 1:
        invalidation_level = min(float(after_bo["low"].min()), broken_level)
    else:
        invalidation_level = max(float(after_bo["high"].max()), broken_level)

    triggered = bool(within_6_m5_bars and retest_touched and reclaim_close_ok and retest_held_ok and no_chase_ok)
    conditions = {
        "within_6_m5_bars": bool(within_6_m5_bars),
        "retest_within_020atr": bool(retest_touched),
        "reclaim_close_in_direction": bool(reclaim_close_ok),
        "retest_held_015atr": bool(retest_held_ok),
        "no_chase_050atr": bool(no_chase_ok),
    }
    return {
        "strategy": STRATEGY_B,
        "strategy_name": STRATEGY_NAMES[STRATEGY_B],
        "eligible_regime": True,
        "triggered": triggered,
        "direction": direction,
        "side": side,
        "broken_level": broken_level,
        "invalidation_level": invalidation_level,
        "bars_since_breakout": len(after_bo),
        "passed_conditions": sum(1 for v in conditions.values() if v),
        "total_conditions": 5,
        "conditions": conditions,
        "reason": "" if triggered else "Menunggu retest level breakout M15 dalam ±0,20 ATR & close konfirmasi M5.",
    }


def evaluate_strategy_c(m15_regime: dict, m5_d: pd.DataFrame) -> dict:
    """Section 7: Strategy C — Momentum Scalping."""
    regime = m15_regime.get("regime")
    if regime == REGIME_STRONG_TREND_UP:
        direction = "LONG"
        side = "BUY"
        sign = 1
    elif regime == REGIME_STRONG_TREND_DOWN:
        direction = "SHORT"
        side = "SELL"
        sign = -1
    else:
        return {
            "strategy": STRATEGY_C,
            "strategy_name": STRATEGY_NAMES[STRATEGY_C],
            "eligible_regime": False,
            "momentum_forming": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": f"Regime {regime} tidak mengizinkan Strategy C (hanya STRONG_TREND_UP/DOWN).",
        }

    ok_fin, missing = _check_strategy_indicator_finiteness(
        m5_d, ("ema9", "ema21", "macd_hist", "rsi", "rvol20", "atr", "vwap")
    )
    if not ok_fin or len(m5_d) < 7 or not finite(m5_d["macd_hist"].iloc[-3]):
        return {
            "strategy": STRATEGY_C,
            "strategy_name": STRATEGY_NAMES[STRATEGY_C],
            "eligible_regime": True,
            "momentum_forming": False,
            "triggered": False,
            "direction": direction,
            "side": side,
            "non_finite_block": True,
            "missing_indicators": missing or ["macd_hist[-3]"],
            "reason": "Indikator M5 untuk Strategy C belum lengkap / non-finite.",
        }

    m15_adx_ok = bool(
        float(m15_regime.get("adx14", 0.0)) >= 22.0
        and m15_regime.get("adx_rising_2bars")
        and float(m15_regime.get("di_spread", 0.0)) >= 5.0
    )

    sig = m5_d.iloc[-1]
    prev = m5_d.iloc[-2]
    prev2 = m5_d.iloc[-3]
    prev5 = m5_d.iloc[-6:-1]
    atr = float(sig.atr)

    if sign == 1:
        ema_fast_ok = (
            float(sig.ema9) > float(sig.ema21)
            and float(sig.ema9) > float(prev.ema9)
            and float(sig.ema21) > float(prev.ema21)
        )
        macd_ok = float(sig.macd_hist) > 0 and float(sig.macd_hist) > float(prev.macd_hist) > float(prev2.macd_hist)
        rsi_ok = 52.0 <= float(sig.rsi) <= 70.0
        breakout_5bar_ok = float(sig.close) > float(prev5["high"].max()) and float(sig.close) > float(sig.open)
        anti_exhaustion_ok = (float(sig.close) - float(sig.ema21)) <= 1.0 * atr
        invalidation_level = float(m5_d["low"].iloc[-5:].min())
    else:
        ema_fast_ok = (
            float(sig.ema9) < float(sig.ema21)
            and float(sig.ema9) < float(prev.ema9)
            and float(sig.ema21) < float(prev.ema21)
        )
        macd_ok = float(sig.macd_hist) < 0 and float(sig.macd_hist) < float(prev.macd_hist) < float(prev2.macd_hist)
        rsi_ok = 30.0 <= float(sig.rsi) <= 48.0
        breakout_5bar_ok = float(sig.close) < float(prev5["low"].min()) and float(sig.close) < float(sig.open)
        anti_exhaustion_ok = (float(sig.ema21) - float(sig.close)) <= 1.0 * atr
        invalidation_level = float(m5_d["high"].iloc[-5:].max())

    rvol_ok = float(sig.rvol20) >= 1.10
    momentum_forming = bool(m15_adx_ok and ema_fast_ok and (macd_ok or rsi_ok))
    triggered = bool(m15_adx_ok and ema_fast_ok and macd_ok and rsi_ok and rvol_ok and breakout_5bar_ok and anti_exhaustion_ok)

    conditions = {
        "m15_adx22_rising_di5": bool(m15_adx_ok),
        "ema9_ema21_aligned_and_sloping": bool(ema_fast_ok),
        "macd_hist_2bar_acceleration": bool(macd_ok),
        "rsi_momentum_band": bool(rsi_ok),
        "rvol_ge_110": bool(rvol_ok),
        "close_breaks_5bar_hl": bool(breakout_5bar_ok),
        "anti_exhaustion_1atr_ema21": bool(anti_exhaustion_ok),
    }
    return {
        "strategy": STRATEGY_C,
        "strategy_name": STRATEGY_NAMES[STRATEGY_C],
        "eligible_regime": True,
        "momentum_forming": momentum_forming,
        "triggered": triggered,
        "direction": direction,
        "side": side,
        "invalidation_level": invalidation_level,
        "passed_conditions": sum(1 for v in conditions.values() if v),
        "total_conditions": 7,
        "conditions": conditions,
        "reason": "" if triggered else "Menunggu akselerasi MACD 2 bar, breakout 5-bar M5, & filter anti-exhaustion Strategy C.",
    }


def evaluate_strategy_d(
    m15_regime: dict,
    m5_d: pd.DataFrame,
    cost_rate_estimate: float = 0.0014,
) -> dict:
    """Section 8: Strategy D — Mean Reversion (Range Only)."""
    regime = m15_regime.get("regime")
    if regime != REGIME_RANGE:
        return {
            "strategy": STRATEGY_D,
            "strategy_name": STRATEGY_NAMES[STRATEGY_D],
            "eligible_regime": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": f"Strategy D (Mean Reversion) diblokir pada regime {regime} (hanya diizinkan pada RANGE).",
        }

    ok_fin, missing = _check_strategy_indicator_finiteness(
        m5_d, ("bb_upper", "bb_lower", "bb_mid", "rsi", "stoch_rsi_k", "stoch_rsi_d", "atr", "vwap", "rvol20")
    )
    if not ok_fin or len(m5_d) < 6:
        return {
            "strategy": STRATEGY_D,
            "strategy_name": STRATEGY_NAMES[STRATEGY_D],
            "eligible_regime": True,
            "triggered": False,
            "direction": None,
            "side": None,
            "non_finite_block": True,
            "missing_indicators": missing,
            "reason": f"Indikator M5 non-finite untuk Strategy D: {', '.join(missing)}",
        }

    sig = m5_d.iloc[-1]
    prev = m5_d.iloc[-2]
    prev3 = m5_d.iloc[-4:-1]
    atr = float(sig.atr)

    def _eval_side(direction: str) -> dict:
        sign = 1 if direction == "LONG" else -1
        side = "BUY" if sign == 1 else "SELL"
        if sign == 1:
            excursion_mask = (prev3["close"] < prev3["bb_lower"]) | (
                (prev3["bb_lower"] - prev3["low"]) >= 0.15 * prev3["atr"]
            )
            reclaim_inside = (
                float(sig.close) > float(sig.bb_lower)
                and float(sig.close) < float(sig.bb_upper)
                and float(sig.close) > float(sig.open)
            )
            rsi_turn = bool((prev3["rsi"] <= 35.0).any() and float(sig.rsi) > float(prev.rsi))
            stoch_cross = (
                float(prev.stoch_rsi_k) <= float(prev.stoch_rsi_d)
                and float(sig.stoch_rsi_k) > float(sig.stoch_rsi_d)
                and min(float(prev.stoch_rsi_k), float(sig.stoch_rsi_k)) <= 20.0
            )
            vwap_or_vol = bool(float(sig.rvol20) >= 1.00 or float(sig.close) >= float(sig.vwap) or float(sig.close) >= float(prev.high))
            invalidation_level = float(m5_d["low"].iloc[-4:].min())
            sl_est = invalidation_level - 0.15 * atr
            risk_dist = max(1e-8, float(sig.close) - sl_est)
            opposing_candidates = [
                x
                for x in (
                    float(sig.bb_mid),
                    float(sig.vwap),
                    float(sig.bb_upper),
                    float(m15_regime.get("donchian20_high") or sig.bb_upper),
                )
                if x > float(sig.close)
            ]
            target_level = max(opposing_candidates) if opposing_candidates else float(sig.bb_upper)
            gross_room = max(0.0, target_level - float(sig.close))
        else:
            excursion_mask = (prev3["close"] > prev3["bb_upper"]) | (
                (prev3["high"] - prev3["bb_upper"]) >= 0.15 * prev3["atr"]
            )
            reclaim_inside = (
                float(sig.close) < float(sig.bb_upper)
                and float(sig.close) > float(sig.bb_lower)
                and float(sig.close) < float(sig.open)
            )
            rsi_turn = bool((prev3["rsi"] >= 65.0).any() and float(sig.rsi) < float(prev.rsi))
            stoch_cross = (
                float(prev.stoch_rsi_k) >= float(prev.stoch_rsi_d)
                and float(sig.stoch_rsi_k) < float(sig.stoch_rsi_d)
                and max(float(prev.stoch_rsi_k), float(sig.stoch_rsi_k)) >= 80.0
            )
            vwap_or_vol = bool(float(sig.rvol20) >= 1.00 or float(sig.close) <= float(sig.vwap) or float(sig.close) <= float(prev.low))
            invalidation_level = float(m5_d["high"].iloc[-4:].max())
            sl_est = invalidation_level + 0.15 * atr
            risk_dist = max(1e-8, sl_est - float(sig.close))
            opposing_candidates = [
                x
                for x in (
                    float(sig.bb_mid),
                    float(sig.vwap),
                    float(sig.bb_lower),
                    float(m15_regime.get("donchian20_low") or sig.bb_lower),
                )
                if x < float(sig.close)
            ]
            target_level = min(opposing_candidates) if opposing_candidates else float(sig.bb_lower)
            gross_room = max(0.0, float(sig.close) - target_level)

        excursion_ok = bool(excursion_mask.any())
        support_count = int(rsi_turn) + int(stoch_cross) + int(vwap_or_vol)
        support_ok = support_count >= 2
        est_cost = float(sig.close) * float(cost_rate_estimate)
        net_rr_room = (gross_room - est_cost) / (risk_dist + est_cost) if (risk_dist + est_cost) > 0 else 0.0
        room_ok = net_rr_room >= 1.5

        conditions = {
            "bb_excursion_prev3": excursion_ok,
            "reclaim_close_inside_bb": bool(reclaim_inside),
            "supporting_2of3": bool(support_ok),
            "rsi_extreme_turning": bool(rsi_turn),
            "stoch_rsi_extreme_cross": bool(stoch_cross),
            "vwap_or_volume_support": bool(vwap_or_vol),
            "target_room_ge_15r": bool(room_ok),
        }
        passed = sum(
            1
            for k in ("bb_excursion_prev3", "reclaim_close_inside_bb", "supporting_2of3", "target_room_ge_15r")
            if conditions[k]
        )
        return {
            "direction": direction,
            "side": side,
            "triggered": bool(excursion_ok and reclaim_inside and support_ok and room_ok),
            "invalidation_level": invalidation_level,
            "target_level": target_level,
            "net_rr_room": round(net_rr_room, 3),
            "support_count": support_count,
            "passed_conditions": passed,
            "total_conditions": 4,
            "conditions": conditions,
        }

    buy_eval = _eval_side("LONG")
    sell_eval = _eval_side("SHORT")

    if buy_eval["triggered"] and sell_eval["triggered"]:
        return {
            "strategy": STRATEGY_D,
            "strategy_name": STRATEGY_NAMES[STRATEGY_D],
            "eligible_regime": True,
            "triggered": False,
            "conflict": True,
            "status": "CONFLICT_WAIT",
            "direction": None,
            "side": None,
            "reason": "CONFLICT_WAIT: Sinyal BUY dan SELL Strategy D aktif bersamaan.",
        }

    chosen = buy_eval if (buy_eval["triggered"] or buy_eval["passed_conditions"] >= sell_eval["passed_conditions"]) else sell_eval
    return {
        "strategy": STRATEGY_D,
        "strategy_name": STRATEGY_NAMES[STRATEGY_D],
        "eligible_regime": True,
        "triggered": chosen["triggered"],
        "direction": chosen["direction"],
        "side": chosen["side"],
        "invalidation_level": chosen["invalidation_level"],
        "target_level": chosen["target_level"],
        "net_rr_room": chosen["net_rr_room"],
        "support_count": chosen["support_count"],
        "passed_conditions": chosen["passed_conditions"],
        "total_conditions": chosen["total_conditions"],
        "conditions": chosen["conditions"],
        "reason": "" if chosen["triggered"] else "Menunggu reclaim Bollinger Band, 2/3 konfirmasi osilator, & ruang target ≥1,5R.",
    }


def evaluate_strategy_e(m15_regime: dict, m5_d: pd.DataFrame) -> dict:
    """Section 9: Strategy E — Liquidity Sweep Reversal."""
    regime = m15_regime.get("regime")
    if regime not in (REGIME_RANGE, REGIME_TRANSITION):
        return {
            "strategy": STRATEGY_E,
            "strategy_name": STRATEGY_NAMES[STRATEGY_E],
            "eligible_regime": False,
            "sweep_detected": False,
            "choch_confirmed": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "reason": f"Regime {regime} tidak mengizinkan Strategy E (hanya RANGE atau TRANSITION).",
        }

    ok_fin, missing = _check_strategy_indicator_finiteness(m5_d, ("atr", "rsi", "rvol20", "vwap"))
    if not ok_fin or len(m5_d) < 15:
        return {
            "strategy": STRATEGY_E,
            "strategy_name": STRATEGY_NAMES[STRATEGY_E],
            "eligible_regime": True,
            "sweep_detected": False,
            "choch_confirmed": False,
            "triggered": False,
            "direction": None,
            "side": None,
            "non_finite_block": True,
            "missing_indicators": missing,
            "reason": f"Indikator M5 non-finite untuk Strategy E: {', '.join(missing)}",
        }

    pivots = confirmed_swing_pivots(m5_d, lookback=80, left=2, right=2)
    low_pivots = [p for p in pivots if p["kind"] == "LOW"]
    high_pivots = [p for p in pivots if p["kind"] == "HIGH"]

    cur_idx = len(m5_d) - 1
    sig = m5_d.iloc[-1]

    def _eval_sweep_side(direction: str) -> dict:
        is_long = direction == "LONG"
        side = "BUY" if is_long else "SELL"
        target_pivots = low_pivots if is_long else high_pivots
        opp_pivots = high_pivots if is_long else low_pivots

        best_match = None
        for sweep_idx in range(max(4, cur_idx - 5), cur_idx + 1):
            s_bar = m5_d.iloc[sweep_idx]
            s_atr = float(s_bar.atr) if finite(s_bar.atr) and float(s_bar.atr) > 0 else float(sig.atr)
            confirmed_before_sweep = [p for p in target_pivots if p["confirmed_at_index"] < sweep_idx]
            if not confirmed_before_sweep:
                continue
            for piv in reversed(confirmed_before_sweep[-4:]):
                lvl = float(piv["price"])
                if is_long:
                    exc = lvl - float(s_bar.low)
                    closed_inside = float(s_bar.close) > lvl
                    extreme_px = float(s_bar.low)
                else:
                    exc = float(s_bar.high) - lvl
                    closed_inside = float(s_bar.close) < lvl
                    extreme_px = float(s_bar.high)
                depth_ok = (0.05 * s_atr - 1e-9) <= exc <= (0.35 * s_atr + 1e-9)
                if depth_ok and closed_inside:
                    best_match = {
                        "sweep_idx": sweep_idx,
                        "pivot": piv,
                        "swept_level": lvl,
                        "sweep_extreme": extreme_px,
                        "excursion_atr": exc / s_atr if s_atr > 0 else 0.0,
                        "sweep_rvol": float(s_bar.rvol20) if finite(s_bar.rvol20) else 0.0,
                    }
                    break
            if best_match is not None:
                break

        if best_match is None:
            return {
                "direction": direction,
                "side": side,
                "sweep_detected": False,
                "choch_confirmed": False,
                "triggered": False,
                "rsi_divergence": False,
                "invalidation_level": None,
                "swept_level": None,
                "passed_conditions": 0,
                "total_conditions": 3,
                "conditions": {
                    "sweep_005_035atr_and_close_inside": False,
                    "rvol_ge_100": False,
                    "choch_mss_within_5bars": False,
                },
            }

        sweep_idx = best_match["sweep_idx"]
        rvol_ok = bool(best_match["sweep_rvol"] >= 1.00 or float(sig.rvol20) >= 1.00)

        internal_pivots = [p for p in opp_pivots if p["confirmed_at_index"] <= cur_idx and p["index"] >= best_match["pivot"]["index"] - 4]
        if not internal_pivots:
            internal_pivots = [p for p in opp_pivots if p["confirmed_at_index"] <= cur_idx]

        choch_ok = False
        choch_level = None
        if internal_pivots:
            latest_internal = internal_pivots[-1]
            choch_level = float(latest_internal["price"])
            if is_long:
                choch_ok = float(sig.close) > choch_level and float(sig.close) > float(sig.open)
            else:
                choch_ok = float(sig.close) < choch_level and float(sig.close) < float(sig.open)

        rsi_div = False
        if len(target_pivots) >= 2:
            p1, p2 = target_pivots[-2], target_pivots[-1]
            if finite(p1["rsi"]) and finite(p2["rsi"]):
                if is_long:
                    rsi_div = bool(p2["price"] < p1["price"] and p2["rsi"] > p1["rsi"])
                else:
                    rsi_div = bool(p2["price"] > p1["price"] and p2["rsi"] < p1["rsi"])
        if not rsi_div and finite(best_match["pivot"]["rsi"]):
            s_rsi = float(m5_d.iloc[sweep_idx].rsi)
            if finite(s_rsi) and sweep_idx <= cur_idx - 2:
                if is_long:
                    rsi_div = bool(best_match["sweep_extreme"] < best_match["swept_level"] and s_rsi > float(best_match["pivot"]["rsi"]))
                else:
                    rsi_div = bool(best_match["sweep_extreme"] > best_match["swept_level"] and s_rsi < float(best_match["pivot"]["rsi"]))

        invalidation_level = (
            float(m5_d["low"].iloc[sweep_idx : cur_idx + 1].min())
            if is_long
            else float(m5_d["high"].iloc[sweep_idx : cur_idx + 1].max())
        )
        conditions = {
            "sweep_005_035atr_and_close_inside": True,
            "rvol_ge_100": rvol_ok,
            "choch_mss_within_5bars": bool(choch_ok),
        }
        return {
            "direction": direction,
            "side": side,
            "sweep_detected": True,
            "choch_confirmed": bool(choch_ok),
            "triggered": bool(rvol_ok and choch_ok),
            "rsi_divergence": bool(rsi_div),
            "invalidation_level": invalidation_level,
            "swept_level": best_match["swept_level"],
            "choch_level": choch_level,
            "excursion_atr": round(best_match["excursion_atr"], 3),
            "bars_since_sweep": cur_idx - sweep_idx,
            "passed_conditions": sum(1 for v in conditions.values() if v),
            "total_conditions": 3,
            "conditions": conditions,
        }

    buy_e = _eval_sweep_side("LONG")
    sell_e = _eval_sweep_side("SHORT")

    if buy_e["triggered"] and sell_e["triggered"]:
        return {
            "strategy": STRATEGY_E,
            "strategy_name": STRATEGY_NAMES[STRATEGY_E],
            "eligible_regime": True,
            "sweep_detected": True,
            "choch_confirmed": True,
            "triggered": False,
            "conflict": True,
            "status": "CONFLICT_WAIT",
            "direction": None,
            "side": None,
            "reason": "CONFLICT_WAIT: Sinyal BUY dan SELL Strategy E aktif bersamaan.",
        }

    chosen = buy_e if (buy_e["triggered"] or buy_e["passed_conditions"] >= sell_e["passed_conditions"]) else sell_e
    return {
        "strategy": STRATEGY_E,
        "strategy_name": STRATEGY_NAMES[STRATEGY_E],
        "eligible_regime": True,
        "sweep_detected": chosen["sweep_detected"],
        "choch_confirmed": chosen["choch_confirmed"],
        "triggered": chosen["triggered"],
        "rsi_divergence": chosen["rsi_divergence"],
        "direction": chosen["direction"] if chosen["sweep_detected"] else None,
        "side": chosen["side"] if chosen["sweep_detected"] else None,
        "invalidation_level": chosen["invalidation_level"],
        "swept_level": chosen["swept_level"],
        "choch_level": chosen.get("choch_level"),
        "excursion_atr": chosen.get("excursion_atr", 0.0),
        "passed_conditions": chosen["passed_conditions"],
        "total_conditions": chosen["total_conditions"],
        "conditions": chosen["conditions"],
        "reason": "" if chosen["triggered"] else "Menunggu liquidity sweep 0,05–0,35 ATR pada swing pivot terkonfirmasi + CHoCH M5.",
    }


# ============================================================================
# SECTION 4: STRATEGY ROUTER (DELIVERABLE 2)
# ============================================================================

def route_strategy_by_regime(
    symbol: str,
    m15_regime: dict,
    m5_d: pd.DataFrame,
    arming_store: ArmingStateStore | None = None,
    current_price: float | None = None,
    cost_rate_estimate: float = 0.0014,
) -> dict:
    """Section 4: Deterministic M15 regime -> Strategy A–E router (`Strategy F`)."""
    store = arming_store or GLOBAL_ARMING_STORE
    regime = m15_regime.get("regime", REGIME_TRANSITION)
    m5_close_ms = int(m5_d["close_time"].iloc[-1]) if (m5_d is not None and len(m5_d) > 0) else 0

    if not m15_regime.get("ok") or regime == REGIME_HIGH_VOLATILITY:
        return {
            "routed_strategy": None,
            "routed_strategy_name": "NONE",
            "regime": regime,
            "status": "HIGH_VOLATILITY_BLOCK" if regime == REGIME_HIGH_VOLATILITY else "REGIME_DATA_BLOCK",
            "evaluation": None,
            "reason": "HIGH_VOLATILITY: Semua entri baru diblokir." if regime == REGIME_HIGH_VOLATILITY else m15_regime.get("reason", "Data M15 belum valid"),
        }

    if regime in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
        bo_dir = "LONG" if regime == REGIME_BREAKOUT_UP else "SHORT"
        store.arm_breakout(
            symbol,
            bo_dir,
            float(m15_regime["breakout_level"]),
            int(m15_regime["candle_close_ms"]),
            float(m15_regime["atr14"]),
        )
        armed_bo = store.get_active_breakout(symbol, m5_close_ms)
        eval_b = evaluate_strategy_b(m15_regime, m5_d, armed_breakout=armed_bo, current_price=current_price)
        return {
            "routed_strategy": STRATEGY_B,
            "routed_strategy_name": STRATEGY_NAMES[STRATEGY_B],
            "regime": regime,
            "status": "TRIGGERED" if eval_b["triggered"] else "ARMED_WAITING_RETEST",
            "evaluation": eval_b,
            "armed_breakout": armed_bo,
            "reason": eval_b["reason"],
        }

    armed_bo = store.get_active_breakout(symbol, m5_close_ms)
    if armed_bo is not None:
        eval_b = evaluate_strategy_b(m15_regime, m5_d, armed_breakout=armed_bo, current_price=current_price)
        if eval_b.get("triggered"):
            return {
                "routed_strategy": STRATEGY_B,
                "routed_strategy_name": STRATEGY_NAMES[STRATEGY_B],
                "regime": regime,
                "status": "TRIGGERED",
                "evaluation": eval_b,
                "armed_breakout": armed_bo,
                "reason": "",
            }

    if regime in (REGIME_STRONG_TREND_UP, REGIME_STRONG_TREND_DOWN):
        eval_a = evaluate_strategy_a(m15_regime, m5_d)
        if eval_a.get("pullback_zone_active"):
            return {
                "routed_strategy": STRATEGY_A,
                "routed_strategy_name": STRATEGY_NAMES[STRATEGY_A],
                "regime": regime,
                "status": "TRIGGERED" if eval_a["triggered"] else "PULLBACK_ZONE_FORMING",
                "evaluation": eval_a,
                "reason": eval_a["reason"],
            }
        eval_c = evaluate_strategy_c(m15_regime, m5_d)
        return {
            "routed_strategy": STRATEGY_C,
            "routed_strategy_name": STRATEGY_NAMES[STRATEGY_C],
            "regime": regime,
            "status": "TRIGGERED" if eval_c["triggered"] else ("MOMENTUM_FORMING" if eval_c.get("momentum_forming") else "WATCH_WAIT"),
            "evaluation": eval_c,
            "reason": eval_c["reason"],
        }

    if regime in (REGIME_TREND_UP, REGIME_TREND_DOWN):
        eval_a = evaluate_strategy_a(m15_regime, m5_d)
        return {
            "routed_strategy": STRATEGY_A,
            "routed_strategy_name": STRATEGY_NAMES[STRATEGY_A],
            "regime": regime,
            "status": "TRIGGERED" if eval_a["triggered"] else "WATCH_WAIT",
            "evaluation": eval_a,
            "reason": eval_a["reason"],
        }

    if regime == REGIME_RANGE:
        eval_e = evaluate_strategy_e(m15_regime, m5_d)
        if eval_e.get("conflict"):
            return {
                "routed_strategy": None,
                "routed_strategy_name": "CONFLICT_WAIT",
                "regime": regime,
                "status": "CONFLICT_WAIT",
                "evaluation": eval_e,
                "reason": eval_e["reason"],
            }
        if eval_e.get("triggered"):
            return {
                "routed_strategy": STRATEGY_E,
                "routed_strategy_name": STRATEGY_NAMES[STRATEGY_E],
                "regime": regime,
                "status": "TRIGGERED",
                "evaluation": eval_e,
                "reason": "",
            }
        eval_d = evaluate_strategy_d(m15_regime, m5_d, cost_rate_estimate=cost_rate_estimate)
        if eval_d.get("conflict"):
            return {
                "routed_strategy": None,
                "routed_strategy_name": "CONFLICT_WAIT",
                "regime": regime,
                "status": "CONFLICT_WAIT",
                "evaluation": eval_d,
                "reason": eval_d["reason"],
            }
        if eval_e.get("sweep_detected") and eval_d.get("triggered") and eval_e.get("direction") != eval_d.get("direction"):
            return {
                "routed_strategy": None,
                "routed_strategy_name": "CONFLICT_WAIT",
                "regime": regime,
                "status": "CONFLICT_WAIT",
                "evaluation": eval_d,
                "reason": "CONFLICT_WAIT: Arah Strategy E dan Strategy D bertentangan pada candle yang sama.",
            }
        return {
            "routed_strategy": STRATEGY_D,
            "routed_strategy_name": STRATEGY_NAMES[STRATEGY_D],
            "regime": regime,
            "status": "TRIGGERED" if eval_d["triggered"] else "WATCH_WAIT",
            "evaluation": eval_d,
            "reason": eval_d["reason"],
        }

    eval_e = evaluate_strategy_e(m15_regime, m5_d)
    if eval_e.get("conflict"):
        return {
            "routed_strategy": None,
            "routed_strategy_name": "CONFLICT_WAIT",
            "regime": regime,
            "status": "CONFLICT_WAIT",
            "evaluation": eval_e,
            "reason": eval_e["reason"],
        }
    if eval_e.get("triggered"):
        return {
            "routed_strategy": STRATEGY_E,
            "routed_strategy_name": STRATEGY_NAMES[STRATEGY_E],
            "regime": regime,
            "status": "TRIGGERED",
            "evaluation": eval_e,
            "reason": "",
        }
    return {
        "routed_strategy": STRATEGY_E if eval_e.get("sweep_detected") else None,
        "routed_strategy_name": STRATEGY_NAMES[STRATEGY_E] if eval_e.get("sweep_detected") else "WATCH_WAIT",
        "regime": regime,
        "status": "WATCH_WAIT",
        "evaluation": eval_e,
        "reason": "TRANSITION: Menunggu liquidity sweep terkonfirmasi + CHoCH M5 (Strategy E) atau kejelasan regime.",
    }


# ============================================================================
# SECTION 10: SETUP QUALITY SCORE (0–100) & STAGE CLASSIFICATION (DELIVERABLE 4)
# ============================================================================

def classify_setup_stage(score: int | float, triggered: bool = False, hard_gates_passed: bool = False) -> str:
    """Section 10.2: Map Setup Quality Score (0–100) and trigger/gate status to stage."""
    if not finite(score) or float(score) < 55:
        return "REJECT"
    sc = int(score)
    if sc <= 64:
        return "EARLY_WATCH"
    if sc <= 77:
        return "SETUP_FORMING"
    if triggered and hard_gates_passed:
        return "PAPER_ENTRY"
    return "ENTRY_CANDIDATE"


def compute_setup_quality_score(
    m15_regime: dict,
    route_info: dict,
    m5_d: pd.DataFrame,
    spread_ok: bool = True,
    depth_ok: bool = True,
    trigger_distance_ok: bool = True,
) -> tuple[int, dict]:
    """Section 10.1: Compute the 8-component Setup Quality Score (0–100 points)."""
    zero_breakdown = {
        "Regime fit": 0,
        "M15 context": 0,
        "Strategy pattern": 0,
        "M5 trigger": 0,
        "Momentum": 0,
        "Relative volume": 0,
        "VWAP / structural location": 0,
        "Execution quality": 0,
    }
    if not m15_regime.get("ok") or m15_regime.get("regime") == REGIME_HIGH_VOLATILITY:
        return 0, zero_breakdown
    if route_info.get("status") == "CONFLICT_WAIT":
        return 0, zero_breakdown

    ev = route_info.get("evaluation") or {}
    if ev.get("non_finite_block"):
        return 0, zero_breakdown

    strat = route_info.get("routed_strategy")
    regime = m15_regime.get("regime")
    direction = ev.get("direction")
    sign = 1 if direction == "LONG" else (-1 if direction == "SHORT" else 0)
    sig = m5_d.iloc[-1] if (m5_d is not None and len(m5_d) > 0) else None
    if sig is None or sign == 0:
        return 0, zero_breakdown

    conds = ev.get("conditions") or {}

    if strat and ev.get("eligible_regime"):
        if regime in (REGIME_STRONG_TREND_UP, REGIME_STRONG_TREND_DOWN, REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN, REGIME_TREND_UP, REGIME_TREND_DOWN, REGIME_RANGE):
            regime_fit = 15
        elif regime == REGIME_TRANSITION and strat == STRATEGY_E and ev.get("triggered"):
            regime_fit = 15
        elif regime == REGIME_TRANSITION and strat == STRATEGY_E and ev.get("sweep_detected"):
            regime_fit = 10
        else:
            regime_fit = 0
    else:
        regime_fit = 0

    adx14 = float(m15_regime.get("adx14", 0.0))
    slope_norm = abs(float(m15_regime.get("ema50_slope_norm", 0.0)))
    di_spread = float(m15_regime.get("di_spread", 0.0))
    m15_rvol = float(m15_regime.get("rvol20", 0.0))
    if regime in (REGIME_STRONG_TREND_UP, REGIME_STRONG_TREND_DOWN):
        m15_context = 15 if (adx14 >= 28.0 and slope_norm >= 0.15 and di_spread >= 8.0) else 13
    elif regime in (REGIME_TREND_UP, REGIME_TREND_DOWN):
        m15_context = 15 if (adx14 >= 22.0 and slope_norm >= 0.15) else 12
    elif regime in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
        m15_context = 15 if m15_rvol >= 1.50 else 13
    elif regime == REGIME_RANGE:
        m15_context = 15 if adx14 < 15.0 else 12
    elif regime == REGIME_TRANSITION and strat == STRATEGY_E and ev.get("sweep_detected"):
        m15_context = 11
    else:
        m15_context = 0

    strategy_pattern = 0
    m5_trigger = 0
    momentum_pts = 0

    if strat == STRATEGY_A:
        if conds.get("ema20_ema50_aligned") and conds.get("pullback_touched_ema20_030atr") and conds.get("ema50_structure_held_015atr"):
            strategy_pattern = 25
        elif conds.get("ema20_ema50_aligned") and conds.get("pullback_touched_ema20_030atr"):
            strategy_pattern = 15
        if conds.get("confirmation_break_prev_hl_and_ema20") and conds.get("confirmation_body_025atr_and_top30pct"):
            m5_trigger = 20
        elif conds.get("confirmation_break_prev_hl_and_ema20"):
            m5_trigger = 12
        if conds.get("rsi_support") and ev.get("support_count", 0) >= 2:
            momentum_pts = 10
        elif conds.get("rsi_support") or ev.get("support_count", 0) >= 2:
            momentum_pts = 7

    elif strat == STRATEGY_B:
        if conds.get("within_6_m5_bars") and conds.get("retest_within_020atr") and conds.get("retest_held_015atr"):
            strategy_pattern = 25
        elif conds.get("within_6_m5_bars") and conds.get("retest_within_020atr"):
            strategy_pattern = 15
        if conds.get("reclaim_close_in_direction") and conds.get("no_chase_050atr"):
            m5_trigger = 20
        elif conds.get("reclaim_close_in_direction"):
            m5_trigger = 10
        if finite(sig.rsi) and ((sign == 1 and float(sig.rsi) >= 50.0) or (sign == -1 and float(sig.rsi) <= 50.0)):
            momentum_pts = 10

    elif strat == STRATEGY_C:
        if conds.get("ema9_ema21_aligned_and_sloping") and conds.get("anti_exhaustion_1atr_ema21"):
            strategy_pattern = 25
        elif conds.get("ema9_ema21_aligned_and_sloping"):
            strategy_pattern = 14
        if conds.get("close_breaks_5bar_hl"):
            m5_trigger = 20
        if conds.get("macd_hist_2bar_acceleration") and conds.get("rsi_momentum_band"):
            momentum_pts = 10
        elif conds.get("macd_hist_2bar_acceleration") or conds.get("rsi_momentum_band"):
            momentum_pts = 6

    elif strat == STRATEGY_D:
        if conds.get("bb_excursion_prev3") and conds.get("target_room_ge_15r"):
            strategy_pattern = 25
        elif conds.get("bb_excursion_prev3"):
            strategy_pattern = 14
        if conds.get("reclaim_close_inside_bb"):
            m5_trigger = 20
        if conds.get("rsi_extreme_turning") and conds.get("stoch_rsi_extreme_cross"):
            momentum_pts = 10
        elif conds.get("supporting_2of3"):
            momentum_pts = 8

    elif strat == STRATEGY_E:
        if conds.get("sweep_005_035atr_and_close_inside"):
            strategy_pattern = 25 if ev.get("rsi_divergence") else 22
        if conds.get("choch_mss_within_5bars"):
            m5_trigger = 20
        if ev.get("rsi_divergence"):
            momentum_pts = 10
        elif finite(sig.rsi) and ((sign == 1 and float(sig.rsi) > float(m5_d.iloc[-2].rsi)) or (sign == -1 and float(sig.rsi) < float(m5_d.iloc[-2].rsi))):
            momentum_pts = 8

    rvol = float(sig.rvol20) if finite(sig.rvol20) else 0.0
    if rvol >= 1.30:
        rvol_pts = 5
    elif rvol >= 1.00:
        rvol_pts = 4
    elif rvol >= 0.80:
        rvol_pts = 2
    else:
        rvol_pts = 0

    vwap_pts = 0
    if finite(sig.vwap) and finite(sig.close):
        if strat in (STRATEGY_A, STRATEGY_B, STRATEGY_C):
            if sign * (float(sig.close) - float(sig.vwap)) > 0:
                vwap_pts = 5
            elif sign * (float(sig.close) - float(sig.vwap)) >= -0.15 * float(sig.atr):
                vwap_pts = 3
        elif strat in (STRATEGY_D, STRATEGY_E):
            vwap_pts = 5 if ev.get("triggered") else 3

    if spread_ok and depth_ok and trigger_distance_ok:
        exec_pts = 5
    elif spread_ok and trigger_distance_ok:
        exec_pts = 3
    else:
        exec_pts = 0

    breakdown = {
        "Regime fit": int(regime_fit),
        "M15 context": int(m15_context),
        "Strategy pattern": int(strategy_pattern),
        "M5 trigger": int(m5_trigger),
        "Momentum": int(momentum_pts),
        "Relative volume": int(rvol_pts),
        "VWAP / structural location": int(vwap_pts),
        "Execution quality": int(exec_pts),
    }
    total = int(min(100, max(0, sum(breakdown.values()))))
    return total, breakdown


# ============================================================================
# SECTIONS 11–13: HARD GATES, SIZING, ENTRY ORDERS & LIFECYCLE (DELIVERABLE 5)
# ============================================================================

def compute_hybrid_tp_targets(
    entry: float,
    sl: float,
    direction: str,
    tick: float,
    qty: float,
    step: float,
    min_qty: float = 0.0,
    min_notional: float = 0.0,
) -> dict:
    """Section 13.1: Partial Take-Profit Targets with automatic lot-size fallback."""
    sign = 1 if direction in ("LONG", "BUY") else -1
    dist_dec = abs(Decimal(str(entry)) - Decimal(str(sl)))
    distance = float(dist_dec)
    ent_dec = Decimal(str(entry))

    def _px(r_mult: float) -> float:
        return quantize(ent_dec + sign * Decimal(str(r_mult)) * dist_dec, tick, up=(sign == 1))

    def _leg_valid(q_val: float, px_val: float) -> bool:
        if q_val <= 0:
            return False
        if min_qty > 0 and q_val + 1e-12 < min_qty:
            return False
        if min_notional > 0 and q_val * min(entry, sl, px_val) + 1e-9 < min_notional:
            return False
        return True

    tp1_3 = _px(1.0)
    tp2_3 = _px(2.0)
    tp3_3 = _px(3.0)
    q1_3 = quantize(qty * 0.40, step)
    q2_3 = quantize(qty * 0.35, step)
    qr_3 = round(float(Decimal(str(qty)) - Decimal(str(q1_3)) - Decimal(str(q2_3))), 12)
    if _leg_valid(q1_3, tp1_3) and _leg_valid(q2_3, tp2_3) and _leg_valid(qr_3, tp3_3):
        return {
            "split_mode": "3_WAY_40_35_25",
            "distance": distance,
            "tp1": tp1_3,
            "tp2": tp2_3,
            "tp3": tp3_3,
            "tp1_r": 1.0,
            "tp2_r": 2.0,
            "tp3_r": 3.0,
            "tp1_qty": q1_3,
            "tp2_qty": q2_3,
            "runner_qty": qr_3,
            "partial_tp_ready": True,
        }

    tp1_2 = _px(1.5)
    tp2_2 = _px(2.5)
    q1_2 = quantize(qty * 0.60, step)
    qr_2 = round(float(Decimal(str(qty)) - Decimal(str(q1_2))), 12)
    if _leg_valid(q1_2, tp1_2) and _leg_valid(qr_2, tp2_2):
        return {
            "split_mode": "2_WAY_60_40_FALLBACK",
            "distance": distance,
            "tp1": tp1_2,
            "tp2": tp2_2,
            "tp3": tp2_2,
            "tp1_r": 1.5,
            "tp2_r": 2.5,
            "tp3_r": 2.5,
            "tp1_qty": q1_2,
            "tp2_qty": 0.0,
            "runner_qty": qr_2,
            "partial_tp_ready": True,
        }

    tp1_1 = _px(1.8)
    q_single = quantize(qty, step)
    single_ok = _leg_valid(q_single, tp1_1)
    return {
        "split_mode": "1_WAY_100_FALLBACK",
        "distance": distance,
        "tp1": tp1_1,
        "tp2": tp1_1,
        "tp3": tp1_1,
        "tp1_r": 1.8,
        "tp2_r": 1.8,
        "tp3_r": 1.8,
        "tp1_qty": q_single,
        "tp2_qty": 0.0,
        "runner_qty": 0.0,
        "partial_tp_ready": bool(single_ok),
    }


def build_hybrid_entry_order(
    signal_bar,
    direction: str,
    m5_atr: float,
    invalidation_level: float,
    tick: float,
    now_ms: int,
    last_price: float,
    open_bar_high: float | None = None,
    open_bar_low: float | None = None,
    news_clear_until_ms: int | None = None,
) -> dict:
    """Section 11.3 & Section 12: Compute structural SL (`structure ± 0.15 * M5_ATR14`),
    entry STOP-LIMIT trigger (`signal high/low ± 1 tick`), entry limit (`trigger ± 0.10 * M5_ATR14`),
    `MISSED_NO_CHASE` check, and `2` completed M5 candle expiry (`signal_close_ms + 1 + 2 * 300_000`).
    """
    sign = 1 if direction in ("LONG", "BUY") else -1
    sig_extreme = float(signal_bar.high if sign == 1 else signal_bar.low)
    trigger_base = quantize(sig_extreme, tick, up=(sign == 1))
    trigger = float(Decimal(str(trigger_base)) + sign * Decimal(str(tick)))
    entry_limit = quantize(trigger + sign * 0.10 * m5_atr, tick, up=(sign == -1))

    raw_sl = float(invalidation_level) - sign * 0.15 * m5_atr
    sl = quantize(raw_sl, tick, up=(sign == -1))
    distance = float(sign * (Decimal(str(entry_limit)) - Decimal(str(sl))))
    sl_atr_ratio = distance / m5_atr if m5_atr > 0 else 0.0
    sl_distance_valid = (0.75 * m5_atr - 1e-9) <= distance <= (2.50 * m5_atr + 1e-9) and distance > 0

    live_past_limit = sign * (float(last_price) - entry_limit) > 0
    intrabar_ext = open_bar_high if sign == 1 else open_bar_low
    intrabar_past_limit = finite(intrabar_ext) and sign * (float(intrabar_ext) - entry_limit) > 0
    missed_no_chase = bool(live_past_limit or intrabar_past_limit)

    sig_close_ms = int(signal_bar.close_time)
    expires_ms = sig_close_ms + 1 + 2 * M5_INTERVAL_MS
    if news_clear_until_ms is not None and finite(news_clear_until_ms):
        expires_ms = min(expires_ms, int(news_clear_until_ms) - 900_000)

    return {
        "entry_type": "STOP-LIMIT; trigger Last",
        "entry_trigger": trigger,
        "entry": entry_limit,
        "sl": sl,
        "invalidation_level": float(invalidation_level),
        "sl_buffer_atr": 0.15,
        "distance": distance,
        "sl_atr_ratio": sl_atr_ratio,
        "sl_distance_valid": bool(sl_distance_valid),
        "missed_no_chase": missed_no_chase,
        "order_status": "MISSED_NO_CHASE" if missed_no_chase else "READY",
        "signal_close_ms": sig_close_ms,
        "expires_ms": int(expires_ms),
        "expiry_m5_bars": 2,
    }


def verify_liquidation_safety(
    entry: float,
    sl: float,
    direction: str,
    leverage: int,
    mmr: float = 0.005,
    tick: float = 0.01,
) -> dict:
    """Section 6 (Risk Engine v1.1): Verify SL sits safely inside Isolated Margin liquidation price."""
    lev = max(1, int(leverage or 1))
    sign = 1 if direction in ("LONG", "BUY") else -1
    if sign == 1:
        liq_raw = float(entry) * (1.0 - (1.0 / lev) + float(mmr))
        safe_margin = float(sl) - liq_raw
        ok = float(sl) > liq_raw + max(float(tick), 0.002 * float(entry))
    else:
        liq_raw = float(entry) * (1.0 + (1.0 / lev) - float(mmr))
        safe_margin = liq_raw - float(sl)
        ok = float(sl) < liq_raw - max(float(tick), 0.002 * float(entry))
    return {
        "ok": bool(ok),
        "liquidation_price": quantize(liq_raw, tick, up=(sign == 1)),
        "safety_margin_price": round(safe_margin, 8),
        "leverage": lev,
        "mmr": float(mmr),
    }


def evaluate_hybrid_adaptive(
    snap,
    account,
    *,
    paper: bool = True,
    arming_store: ArmingStateStore | None = None,
    min_m15_candles: int = DEFAULT_MIN_CANDLES_M15,
    min_m5_candles: int = DEFAULT_MIN_CANDLES_M5,
    min_24h_volume_usd: float = 5_000_000.0,
    min_24h_trades: int = 500,
    forced_strategy: str | None = None,
    allow_spot_paper_simulation: bool = False,
    futures_unavailable: bool = False,
) -> Result:
    """Master Hybrid Adaptive Engine (`Strategy F` or isolated `A`..`E`) implementing Sections 1–13."""
    store = arming_store or GLOBAL_ARMING_STORE
    is_spot_sim = bool(allow_spot_paper_simulation and paper and getattr(snap, "instrument_kind", "") == "SPOT_REFERENCE")
    inst_check = validate_hybrid_instrument(snap, getattr(snap, "contract", None), allow_spot_watchlist_only=is_spot_sim)
    r = Result(
        meta={
            "symbol": snap.symbol,
            "mode": snap.mode,
            "source": snap.source,
            "market_provider": getattr(snap, "market_provider", None) or snap.source,
            "instrument_kind": snap.instrument_kind,
            "data_timestamp": int(getattr(snap, "data_timestamp", None) or snap.now_ms),
            "received_timestamp": int(getattr(snap, "received_timestamp", None) or snap.now_ms),
            "data_status": "FUTURES_DATA_UNAVAILABLE" if futures_unavailable else "OK",
            "failover_reason": str(getattr(snap, "failover_reason", "") or ""),
            "quote_time_basis": snap.quote_time_basis,
            "time_ms": snap.now_ms,
            "timezone": "Asia/Jakarta",
            "scope": "PAPER" if paper else "LIVE_DISABLED",
            "strategy_engine": "HYBRID_ADAPTIVE_V1",
            "strategy_profile": "BALANCED",
            "live_orders_disabled": True,
            "confidence_disclaimer": "Setup Quality Score (0–100) adalah kualitas konfluensi teknikal, BUKAN win probability.",
            "limitations": list(getattr(snap, "warnings", [])),
        }
    )

    if futures_unavailable:
        r.check(
            "Ketersediaan provider Futures",
            "FUTURES_DATA_UNAVAILABLE",
            "Minimal satu provider Futures Perpetual (Binance USDⓈ-M / Bybit Linear) aktif",
            False,
            True,
        )
        r.meta["data_status"] = "FUTURES_DATA_UNAVAILABLE"
        r.meta["hold_status"] = "HOLD"
        r.meta["rejection_category"] = "rejected_by_instrument"
        res = r.finish()
        res.status = "HOLD"
        return res

    if is_spot_sim:
        r.checks.append({
            "Kriteria": "Kontrak linear USDT perpetual",
            "Nilai aktual": inst_check["label"],
            "Syarat": "SPOT_REFERENCE SIMULASI PAPER (Jurnal Terpisah)",
            "Status": "SIMULASI",
        })
    else:
        r.check(
            "Kontrak linear USDT perpetual",
            inst_check["label"],
            "USDT-M PERPETUAL TRADING (SPOT_REFERENCE diblokir untuk PAPER entry Futures)",
            inst_check["eligible_for_futures_paper"],
            not inst_check["eligible_for_futures_paper"],
        )
        if not inst_check["eligible_for_futures_paper"]:
            r.meta["hold_status"] = "HOLD"
            r.meta["rejection_category"] = "rejected_by_instrument"
            return r.finish()

    reference = snap.reference_price
    quotes_ok = (
        all(finite(x) and float(x) > 0 for x in (snap.last, reference, snap.bid, snap.ask))
        and float(snap.ask) >= float(snap.bid)
    )
    r.check("Harga pasar", "Tersedia" if quotes_ok else "Tidak valid", "Last, Mark, Bid, Ask > 0 & Ask ≥ Bid", quotes_ok, not quotes_ok)

    ages = [(snap.now_ms - snap.quote_times.get(k, 0)) / 1000.0 for k in snap.quote_keys]
    fresh = (
        bool(ages)
        and len(ages) == len(snap.quote_keys)
        and all(k in snap.quote_times for k in snap.quote_keys)
        and all(0.0 <= age <= 30.0 for age in ages)
    )
    if not fresh:
        r.meta["stale_data"] = True
        r.meta["data_status"] = "STALE_DATA"
    r.check("Usia snapshot", f"{max(ages):.1f} detik" if ages else "Tidak tersedia", "0–30 detik (timestamp bursa)", fresh, not fresh)

    bias_tf, exec_tf = TIMEFRAMES.get(snap.mode, ("15m", "5m"))
    r.meta.update(bias_tf=bias_tf, execution_tf=exec_tf)
    m15_hist = validate_hybrid_candle_history(snap.bias, bias_tf, snap.now_ms, min_candles=min_m15_candles)
    m5_hist = validate_hybrid_candle_history(snap.execution, exec_tf, snap.now_ms, min_candles=min_m5_candles)
    if not m15_hist["ok"] or not m5_hist["ok"]:
        if "HISTORY_INSUFFICIENT" in (m15_hist.get("code", ""), m5_hist.get("code", "")):
            r.meta["history_status"] = "HISTORY_INSUFFICIENT"
            r.meta["data_status"] = "HISTORY_INSUFFICIENT"
    r.check(
        "Candle bias M15",
        f"{len(snap.bias)} tertutup" if m15_hist["ok"] else m15_hist["reason"],
        f"≥{min_m15_candles} candle tertutup, kontinu, terbaru selesai",
        m15_hist["ok"],
        not m15_hist["ok"],
    )
    r.check(
        "Candle eksekusi M5",
        f"{len(snap.execution)} tertutup" if m5_hist["ok"] else m5_hist["reason"],
        f"≥{min_m5_candles} candle tertutup, kontinu, terbaru selesai",
        m5_hist["ok"],
        not m5_hist["ok"],
    )
    if any(x["Status"] not in ("LULUS", "SIMULASI") for x in r.checks):
        r.meta["rejection_category"] = "rejected_by_candle_history" if not (m15_hist["ok"] and m5_hist["ok"]) else "rejected_by_data"
        return r.finish()

    m15_d = indicators(snap.bias)
    m5_d = indicators(snap.execution)
    m15_regime = detect_m15_regime(m15_d)
    r.meta["m15_regime"] = m15_regime.get("regime")
    r.meta["m15_regime_info"] = m15_regime

    r.check(
        "Regime M15",
        f"{m15_regime.get('regime')} (ADX {m15_regime.get('adx14', 0):.1f}; Slope {m15_regime.get('ema50_slope_norm', 0):.2f})",
        "Bukan HIGH_VOLATILITY & indikator M15 finite",
        bool(m15_regime.get("ok") and m15_regime.get("allow_new_entries")),
        not bool(m15_regime.get("ok")),
    )
    if not m15_regime.get("ok") or not m15_regime.get("allow_new_entries"):
        r.meta["rejection_category"] = "rejected_by_volatility_regime"
        return r.finish()

    cost_est = 2.0 * float(getattr(account, "taker_fee", 0.0005)) + float(getattr(account, "exit_slippage_bps", 2.0)) / 10000.0 + float(getattr(account, "funding_reserve_rate", 0.0003))
    if forced_strategy in (STRATEGY_A, STRATEGY_B, STRATEGY_C, STRATEGY_D, STRATEGY_E):
        m5_close_ms = int(m5_d["close_time"].iloc[-1])
        if forced_strategy == STRATEGY_A:
            ev = evaluate_strategy_a(m15_regime, m5_d)
        elif forced_strategy == STRATEGY_B:
            if m15_regime["regime"] in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
                store.arm_breakout(snap.symbol, "LONG" if m15_regime["regime"] == REGIME_BREAKOUT_UP else "SHORT", float(m15_regime["breakout_level"]), int(m15_regime["candle_close_ms"]), float(m15_regime["atr14"]))
            ev = evaluate_strategy_b(m15_regime, m5_d, armed_breakout=store.get_active_breakout(snap.symbol, m5_close_ms), current_price=snap.last)
        elif forced_strategy == STRATEGY_C:
            ev = evaluate_strategy_c(m15_regime, m5_d)
        elif forced_strategy == STRATEGY_D:
            ev = evaluate_strategy_d(m15_regime, m5_d, cost_rate_estimate=cost_est)
        else:
            ev = evaluate_strategy_e(m15_regime, m5_d)
        route_info = {
            "routed_strategy": forced_strategy if ev.get("eligible_regime") else None,
            "routed_strategy_name": STRATEGY_NAMES[forced_strategy],
            "regime": m15_regime["regime"],
            "status": "CONFLICT_WAIT" if ev.get("conflict") else ("TRIGGERED" if ev.get("triggered") else "WATCH_WAIT"),
            "evaluation": ev,
            "reason": ev.get("reason", ""),
        }
    else:
        route_info = route_strategy_by_regime(
            snap.symbol,
            m15_regime,
            m5_d,
            arming_store=store,
            current_price=snap.last,
            cost_rate_estimate=cost_est,
        )

    ev = route_info.get("evaluation") or {}
    routed_strat = route_info.get("routed_strategy")
    direction = ev.get("direction")
    r.direction = direction if direction in ("LONG", "SHORT") else None
    r.meta["routed_strategy"] = routed_strat
    r.meta["routed_strategy_name"] = route_info.get("routed_strategy_name")
    r.meta["route_status"] = route_info.get("status")

    sig = m5_d.iloc[-1]
    m5_atr = float(sig.atr) if finite(sig.atr) and float(sig.atr) > 0 else 1e-8
    spread_price = float(snap.ask) - float(snap.bid)
    mid_price = (float(snap.ask) + float(snap.bid)) / 2.0
    spread_bps = (spread_price / mid_price * 10000.0) if mid_price > 0 else 999.0
    spread_ok = spread_bps <= 10.0 and spread_price <= 0.08 * m5_atr

    score, breakdown = compute_setup_quality_score(
        m15_regime,
        route_info,
        m5_d,
        spread_ok=spread_ok,
        depth_ok=True,
        trigger_distance_ok=True,
    )
    if getattr(account, "confidence_override", None) is not None:
        score = int(account.confidence_override)

    r.metrics = {
        "m15_regime": m15_regime["regime"],
        "routed_strategy": routed_strat,
        "routed_strategy_name": route_info.get("routed_strategy_name"),
        "setup_quality_score": score,
        "confidence_score": score,
        "score_breakdown": breakdown,
        "confidence_breakdown": breakdown,
        "adx": float(m15_regime["adx14"]),
        "plus_di": float(m15_regime["plus_di"]),
        "minus_di": float(m15_regime["minus_di"]),
        "ema50_slope_norm": float(m15_regime["ema50_slope_norm"]),
        "rsi": float(sig.rsi) if finite(sig.rsi) else None,
        "atr": m5_atr,
        "relative_volume": float(sig.rvol20) if finite(sig.rvol20) else 0.0,
        "vwap": float(sig.vwap) if finite(sig.vwap) else None,
        "spread_bps": round(spread_bps, 3),
        "last": float(snap.last),
        "mark": float(reference) if snap.mark is not None else None,
        "reference_price": float(reference),
        "index_price": getattr(snap, "index_price", None),
        "funding_rate": getattr(snap, "funding_rate", None),
        "open_interest": getattr(snap, "open_interest", None),
        "bid": float(snap.bid),
        "ask": float(snap.ask),
        "market_provider": r.meta["market_provider"],
        "instrument_kind": snap.instrument_kind,
        "data_timestamp": r.meta["data_timestamp"],
        "received_timestamp": r.meta["received_timestamp"],
    }

    r.check(
        "Routing strategi & pemicu M5",
        f"Regime {m15_regime['regime']} -> Strategi {routed_strat or 'NONE'} ({route_info.get('status')})",
        "Strategi sesuai regime & kondisi trigger M5 terpenuhi tanpa konflik",
        bool(routed_strat and ev.get("triggered") and not ev.get("conflict") and direction in ("LONG", "SHORT")),
    )
    if not (routed_strat and ev.get("triggered") and direction in ("LONG", "SHORT")):
        r.meta["setup_stage"] = classify_setup_stage(score, triggered=False, hard_gates_passed=False)
        r.meta["rejection_category"] = "rejected_by_confidence"
        return r.finish()

    if is_spot_sim:
        r.checks.append({
            "Kriteria": "Deviasi Mark–Last",
            "Nilai aktual": "Spot memakai Last",
            "Syarat": "SIMULASI (Spot tidak memiliki Mark Price Futures)",
            "Status": "SIMULASI",
        })
    else:
        deviation = abs(float(reference) - float(snap.last))
        r.check("Deviasi Mark–Last", f"{deviation:.8g}", f"≤{0.25 * m5_atr:.8g} (0,25 ATR M5)", deviation <= 0.25 * m5_atr)
    r.check(
        "Spread pasar",
        f"{spread_bps:.2f} bps ({spread_price:.8g})",
        f"≤10 bps DAN ≤{0.08 * m5_atr:.8g} (0,08 ATR M5)",
        spread_ok,
    )

    vol_24h = float(getattr(snap, "quote_volume_24h", 0.0) or 0.0)
    trades_24h = int(getattr(snap, "trade_count_24h", 0) or 0)
    if vol_24h > 0 or trades_24h > 0:
        liq_24h_ok = vol_24h >= min_24h_volume_usd and trades_24h >= min_24h_trades
        r.check(
            "Volume & jumlah transaksi 24j",
            f"Vol ${vol_24h:,.0f}; Trades {trades_24h}",
            f"Volume ≥${min_24h_volume_usd:,.0f} & Trades ≥{min_24h_trades}",
            liq_24h_ok,
        )

    spec = snap.contract
    order_info = build_hybrid_entry_order(
        sig,
        direction,
        m5_atr,
        float(ev["invalidation_level"]),
        spec.tick,
        snap.now_ms,
        snap.last,
        open_bar_high=getattr(snap, "open_bar_high", None),
        open_bar_low=getattr(snap, "open_bar_low", None),
        news_clear_until_ms=getattr(account, "news_clear_until_ms", None),
    )
    trigger = order_info["entry_trigger"]
    entry = order_info["entry"]
    sl = order_info["sl"]
    distance = order_info["distance"]
    sign = 1 if direction == "LONG" else -1

    r.check(
        "Jarak SL struktural",
        f"{order_info['sl_atr_ratio']:.3f} ATR (SL {sl:.8g}; buffer 0,15 ATR)",
        "0,75–2,50 ATR M5 di luar struktur invalidasi",
        order_info["sl_distance_valid"],
    )
    r.check(
        "Disiplin No-Chase (STOP-LIMIT)",
        f"Last {snap.last:.8g}; Limit {entry:.8g} ({order_info['order_status']})",
        "Harga belum melewati batas limit entry sebelum order dibuat",
        not order_info["missed_no_chase"],
    )
    if order_info["missed_no_chase"]:
        r.meta["order_status"] = "MISSED_NO_CHASE"

    raw_adv_last = getattr(snap, "open_bar_low", None) if sign == 1 else getattr(snap, "open_bar_high", None)
    adverse_last = raw_adv_last if raw_adv_last is not None else snap.last
    raw_adv_mark = getattr(snap, "open_mark_low", None) if sign == 1 else getattr(snap, "open_mark_high", None)
    adverse_mark = raw_adv_mark if raw_adv_mark is not None else reference
    sl_untouched = (
        finite(adverse_last)
        and finite(adverse_mark)
        and sign * (float(adverse_last) - sl) > 0
        and sign * (float(adverse_mark) - sl) > 0
        and sign * (float(snap.last) - sl) > 0
        and sign * (float(reference) - sl) > 0
    )
    r.check("SL belum tersentuh sejak sinyal", f"Last {adverse_last}; Mark {adverse_mark}", f"Belum menyentuh SL {sl:.8g}", sl_untouched)

    now_day = datetime.fromtimestamp(snap.now_ms / 1000.0, timezone.utc).date().isoformat()
    valid_account = (
        bool(getattr(account, "account_verified", False))
        and finite(account.equity)
        and account.equity > 0
        and finite(account.day_start_equity)
        and account.day_start_equity > 0
        and finite(account.free_margin)
        and account.free_margin >= 0
    )
    r.check("Data akun PAPER", "Terverifikasi" if valid_account else "Tidak valid", "Ekuitas & margin bebas valid", valid_account, not valid_account)
    r.check("Hari akun UTC", account.account_day_utc, now_day, account.account_day_utc == now_day, account.account_day_utc != now_day)

    drawdown = (account.day_start_equity + getattr(account, "net_cash_flow", 0.0) - account.equity) / account.day_start_equity if valid_account else 1.0
    last_loss_ms = getattr(account, "last_loss_closed_ms", None)
    in_loss_cooldown = False
    if int(getattr(account, "consecutive_losses", 0)) >= 3:
        if last_loss_ms is None or (int(snap.now_ms) - int(last_loss_ms)) < COOLDOWN_CONSECUTIVE_LOSSES_MS:
            in_loss_cooldown = True
    r.check(
        "Batas rugi harian & cooldown",
        f"DD {drawdown * 100:.2f}%; rugi beruntun {account.consecutive_losses}",
        "Rugi harian <2,0% & tidak dalam cooldown 60m setelah 3 rugi beruntun",
        drawdown < 0.02 and not in_loss_cooldown,
    )
    r.check("Batas posisi & pending", account.active_pending_count, "<2 posisi/pending aktif", int(account.active_pending_count) < 2)
    same_dir_alts = int(getattr(account, "same_direction_alt_count", 0) or 0)
    if snap.symbol != "BTCUSDT":
        r.check("Korelasi altcoin searah", same_dir_alts, "<2 posisi altcoin searah", same_dir_alts < 2)

    news_ok = (
        account.news_verified_at_ms is not None
        and 0 <= snap.now_ms - account.news_verified_at_ms <= 3_600_000
        and bool(str(account.news_source or "").strip())
        and account.news_clear_from_ms is not None
        and account.news_clear_until_ms is not None
        and account.news_clear_from_ms <= snap.now_ms - 900_000
        and account.news_clear_until_ms >= snap.now_ms + 900_000
    )
    r.check("Filter berita berdampak tinggi", account.news_source if news_ok else "Blackout / belum terverifikasi", "Tidak dalam ±15 menit event berdampak tinggi", news_ok, not news_ok)

    req_lev = int(getattr(account, "leverage", 2) or 2)
    effective_leverage = max(1, min(3, req_lev))
    r.check("Leverage PAPER", f"{effective_leverage}x (diminta {req_lev}x)", "1x–3x (skor tidak pernah menaikkan leverage)", 1 <= req_lev <= 3)

    fee = float(account.taker_fee)
    slip = entry * float(account.exit_slippage_bps) / 10000.0
    funding = entry * float(account.funding_reserve_rate)
    csl = entry * fee + (sl + slip) * fee + slip + funding
    cost_ok = distance > 0 and csl <= 0.20 * distance
    r.check("Biaya round-trip terhadap 1R", f"{csl:.8g}", f"≤{0.20 * max(0.0, distance):.8g} (20% dari 1R)", cost_ok)

    levels = list(confirmed_pivots(m15_d))
    if routed_strat in (STRATEGY_D, STRATEGY_E):
        levels += confirmed_pivots(m5_d)
    for d_hl in (getattr(snap, "previous_day_high", None), getattr(snap, "previous_day_low", None)):
        if finite(d_hl) and float(d_hl) > 0:
            levels.append(float(d_hl))
    obstacles = [x for x in levels if sign * (x - entry) > 0]
    nearest_obstacle = min(obstacles, key=lambda x: abs(x - entry)) if obstacles else None
    primary_target_px = quantize(Decimal(str(entry)) + sign * Decimal("2.0") * Decimal(str(max(1e-8, distance))), spec.tick, up=(sign == 1))
    effective_target_px = nearest_obstacle if (nearest_obstacle is not None and abs(nearest_obstacle - entry) < abs(primary_target_px - entry)) else primary_target_px
    tp_cost = entry * fee + (effective_target_px + slip) * fee + slip + funding
    net_rr = (abs(effective_target_px - entry) - tp_cost) / (distance + csl) if (distance + csl) > 0 else 0.0
    r.check("Ruang target bersih (Net R:R)", f"{net_rr:.2f}R", "≥1,50R setelah biaya, slippage, dan funding", net_rr >= 1.50)

    eff_risk_pct = min(float(getattr(account, "risk_pct", 0.25) or 0.25), 0.25)
    per_pos_budget = account.equity * (eff_risk_pct / 100.0)
    rem_portfolio_budget = max(0.0, account.equity * 0.005 - float(getattr(account, "total_existing_risk", 0.0)))
    effective_budget = max(0.0, min(per_pos_budget, rem_portfolio_budget))

    c_mult = float(getattr(spec, "contract_multiplier", 1.0) or 1.0)
    if not finite(c_mult) or c_mult <= 0:
        c_mult = 1.0
    unit_risk = (distance + csl) * c_mult
    unit_notional = entry * c_mult
    step = common_step(spec.step, spec.market_step)
    raw_qty = (
        min(
            effective_budget / unit_risk,
            account.equity * 0.20 * effective_leverage / unit_notional,
            account.free_margin / (unit_notional / effective_leverage + csl * c_mult),
            spec.max_qty,
            spec.market_max_qty,
        )
        if unit_risk > 0 and unit_notional > 0
        else 0.0
    )
    qty = quantize(max(0.0, raw_qty), step)
    actual_risk = qty * unit_risk
    notional = qty * unit_notional
    margin = notional / effective_leverage

    r.check(
        "Anggaran risiko (≤0,25% posisi / ≤0,50% total)",
        f"Risiko {actual_risk:.4f} USDT ({actual_risk / account.equity * 100:.3f}% ekuitas)",
        f"≤{per_pos_budget:.4f} USDT (0,25%) & sisa portofolio {rem_portfolio_budget:.4f} USDT",
        qty > 0 and effective_budget > 0 and actual_risk <= per_pos_budget + 1e-8 and actual_risk <= rem_portfolio_budget + 1e-8,
    )

    liq_info = verify_liquidation_safety(entry, sl, direction, effective_leverage, tick=spec.tick)
    r.meta["liquidation_safety"] = liq_info
    if is_spot_sim:
        r.checks.append({
            "Kriteria": "Keamanan likuidasi (Isolated Margin)",
            "Nilai aktual": f"Spot tanpa likuidasi kontrak (Est. {liq_info['liquidation_price']:.8g})",
            "Syarat": "SIMULASI (Spot tidak memiliki likuidasi Futures)",
            "Status": "SIMULASI",
        })
    else:
        r.check(
            "Keamanan likuidasi (Isolated Margin)",
            f"SL {sl:.8g} vs Est. Liq {liq_info['liquidation_price']:.8g} (margin {liq_info['safety_margin_price']:.8g})",
            "SL berada aman sebelum harga likuidasi Isolated Margin",
            liq_info["ok"],
        )

    minqty = max(spec.min_qty, spec.market_min_qty)
    tp_bundle = compute_hybrid_tp_targets(
        entry,
        sl,
        direction,
        spec.tick,
        qty,
        step,
        min_qty=minqty,
        min_notional=spec.min_notional,
    )
    r.check(
        "Pembagian lot & TP parsial",
        f"Mode {tp_bundle['split_mode']}: Q={qty:.8g} (TP1={tp_bundle['tp1_qty']:.8g}, TP2={tp_bundle['tp2_qty']:.8g}, Runner={tp_bundle['runner_qty']:.8g})",
        "Memenuhi step, minQty, dan minNotional bursa",
        tp_bundle["partial_tp_ready"] and qty >= minqty and notional >= spec.min_notional,
    )

    bid_px, ask_px = float(snap.bid), float(snap.ask)
    bid_depth_usdt = sum(
        float(p) * float(q)
        for p, q in (snap.bids or [])
        if finite(float(p)) and finite(float(q)) and abs(float(p) - bid_px) / bid_px <= 0.0010 + 1e-12
    )
    ask_depth_usdt = sum(
        float(p) * float(q)
        for p, q in (snap.asks or [])
        if finite(float(p)) and finite(float(q)) and abs(float(p) - ask_px) / ask_px <= 0.0010 + 1e-12
    )
    depth_5x_ok = notional > 0 and bid_depth_usdt >= 5.0 * notional and ask_depth_usdt >= 5.0 * notional
    r.check(
        "Kedalaman order book (±10 bps ≥ 5x notional)",
        f"Bid ${bid_depth_usdt:,.1f}; Ask ${ask_depth_usdt:,.1f}; 5x Notional ${5.0 * notional:,.1f}",
        "Bid & Ask dalam 10 bps ≥ 5× nilai nosional posisi",
        depth_5x_ok,
        not bool(snap.bids and snap.asks),
    )

    score, breakdown = compute_setup_quality_score(
        m15_regime,
        route_info,
        m5_d,
        spread_ok=spread_ok,
        depth_ok=depth_5x_ok,
        trigger_distance_ok=not order_info["missed_no_chase"],
    )
    if getattr(account, "confidence_override", None) is not None:
        score = int(account.confidence_override)
    r.metrics["setup_quality_score"] = score
    r.metrics["confidence_score"] = score
    r.metrics["score_breakdown"] = breakdown
    r.metrics["confidence_breakdown"] = breakdown

    r.check(
        "Setup Quality Score (≥78 untuk PAPER_ENTRY)",
        f"{score}/100 ({classify_setup_stage(score, triggered=True, hard_gates_passed=True)})",
        "≥78 poin pada Setup Quality Score (0–100)",
        score >= 78,
    )

    all_passed = all(c["Status"] in ("LULUS", "SIMULASI") for c in r.checks)
    stage = classify_setup_stage(score, triggered=True, hard_gates_passed=all_passed)
    r.meta["setup_stage"] = stage

    if not all_passed:
        if int(account.active_pending_count) >= 2 or same_dir_alts >= 2:
            r.meta["rejection_category"] = "rejected_by_position_limit"
        elif not news_ok:
            r.meta["rejection_category"] = "rejected_by_news"
        elif in_loss_cooldown or drawdown >= 0.02:
            r.meta["rejection_category"] = "rejected_by_cooldown_or_daily_loss"
        elif not spread_ok or not depth_5x_ok:
            r.meta["rejection_category"] = "rejected_by_liquidity_or_spread"
        elif not order_info["sl_distance_valid"] or net_rr < 1.50 or not cost_ok:
            r.meta["rejection_category"] = "rejected_by_sl_or_rr"
        elif score < 78:
            r.meta["rejection_category"] = "rejected_by_confidence"
        else:
            r.meta["rejection_category"] = "rejected_by_risk"
        return r.finish()

    plan = {
        "symbol": snap.symbol,
        "mode": snap.mode,
        "direction": direction,
        "strategy": routed_strat,
        "strategy_name": route_info.get("routed_strategy_name"),
        "m15_regime": m15_regime["regime"],
        "signal_close_ms": order_info["signal_close_ms"],
        "entry_trigger": trigger,
        "entry": entry,
        "entry_type": order_info["entry_type"],
        "sl": sl,
        "sl_type": "STOP-MARKET; trigger Mark; reduce-only",
        "invalidation_level": order_info["invalidation_level"],
        "distance": distance,
        "sl_distance_pct": round(distance / entry * 100.0, 4),
        "sl_atr_ratio": round(order_info["sl_atr_ratio"], 4),
        "tp1": tp_bundle["tp1"],
        "tp2": tp_bundle["tp2"],
        "tp3": tp_bundle["tp3"],
        "tp1_qty": tp_bundle["tp1_qty"],
        "tp2_qty": tp_bundle["tp2_qty"],
        "runner_qty": tp_bundle["runner_qty"],
        "tp_split_mode": tp_bundle["split_mode"],
        "qty": qty,
        "notional": notional,
        "margin": margin,
        "leverage": effective_leverage,
        "equity": float(account.equity),
        "risk_budget": effective_budget,
        "risk": actual_risk,
        "fee": fee,
        "funding_per_unit": funding,
        "slippage_per_unit": slip,
        "cost_sl_per_unit": csl,
        "net_rr_tp1": round(net_rr, 4),
        "tick": spec.tick,
        "quantity_step": step,
        "min_qty": minqty,
        "min_notional": spec.min_notional,
        "expires_ms": order_info["expires_ms"],
        "expiry_m5_bars": 2,
        "trailing_activation_r": 1.5,
        "trailing_atr_mult": 1.25,
        "time_exit_half_r_bars": 6,
        "time_exit_one_r_bars": 12,
        "setup_quality_score": score,
        "confidence_score": score,
        "confidence_breakdown": breakdown,
        "strategy_profile": "BALANCED",
        "scope": "PAPER",
        "instrument_kind": snap.instrument_kind,
        "market_provider": r.meta["market_provider"],
        "data_timestamp": r.meta["data_timestamp"],
        "received_timestamp": r.meta["received_timestamp"],
        "contract_multiplier": c_mult,
        "liquidation_price": liq_info["liquidation_price"],
        "liquidation_safety_ok": liq_info["ok"],
        "hybrid_adaptive_v1": True,
    }
    id_fields = {
        k: plan[k]
        for k in (
            "symbol",
            "mode",
            "direction",
            "strategy",
            "signal_close_ms",
            "entry",
            "sl",
            "qty",
            "leverage",
        )
    }
    plan["id"] = hashlib.sha256(json.dumps(id_fields, sort_keys=True).encode()).hexdigest()[:20]
    r.candidate = plan
    r.plan = plan
    return r.finish()


def evaluate_hybrid_position_lifecycle(
    plan: dict,
    m5_frame: pd.DataFrame,
    now_ms: int,
    mark_price: float,
    current_stop: float | None = None,
    tp1_filled: bool = False,
    tp2_filled: bool = False,
    reached_half_r: bool = False,
    reached_one_r: bool = False,
    reached_15_r: bool = False,
    intrabar_high: float | None = None,
    intrabar_low: float | None = None,
    m1_sequence_available: bool = False,
) -> dict:
    """Section 13: Manage open Hybrid Adaptive PAPER position."""
    sign = 1 if plan["direction"] in ("LONG", "BUY") else -1
    tighten = max if sign == 1 else min
    stop = float(plan["sl"] if current_stop is None else current_stop)
    entry = float(plan["entry"])
    dist = float(plan["distance"])
    tick = float(plan.get("tick", 0.01))

    bar_hi = float(intrabar_high) if finite(intrabar_high) else float(mark_price)
    bar_lo = float(intrabar_low) if finite(intrabar_low) else float(mark_price)
    adverse = bar_lo if sign == 1 else bar_hi
    favorable = bar_hi if sign == 1 else bar_lo
    sl_touched = sign * (adverse - stop) <= 0
    tp_touched = sign * (favorable - float(plan["tp1"])) >= 0
    if sl_touched and tp_touched and not m1_sequence_available:
        return {
            "action": "EXIT",
            "exit_reason": "STOP_LOSS",
            "flag": "CONSERVATIVE_INTRABAR_APPROXIMATION",
            "stop": stop,
            "sl_touched": True,
            "tp_touched": True,
        }
    if sl_touched or sign * (float(mark_price) - stop) <= 0:
        return {
            "action": "EXIT",
            "exit_reason": "STOP_LOSS",
            "flag": "NONE",
            "stop": stop,
            "sl_touched": True,
            "tp_touched": False,
        }

    closed = m5_frame[m5_frame["close_time"] < int(now_ms)].reset_index(drop=True)
    d = indicators(closed) if "atr" not in closed.columns else closed
    fill_ms = int(plan.get("fill_ms") or plan.get("signal_close_ms") or 0)
    after = d[d["open_time"] >= fill_ms]
    bars_open = len(after)
    m5_atr = float(d["atr"].iloc[-1]) if (len(d) > 0 and finite(d["atr"].iloc[-1])) else dist

    if tp1_filled:
        be_raw = break_even_price(
            entry,
            plan["direction"],
            float(plan["fee"]),
            float(plan["funding_per_unit"]),
            float(plan["slippage_per_unit"]),
        )
        be_px = quantize(be_raw, tick, up=(sign == 1))
        stop = tighten(stop, be_px)

    hit_half_r = bool(reached_half_r)
    hit_one_r = bool(reached_one_r or tp1_filled)
    hit_15_r = bool(reached_15_r or tp2_filled)
    if bars_open > 0:
        max_fav_extreme = float(after["high"].max()) if sign == 1 else float(after["low"].min())
        max_fav_close = float(after["close"].max()) if sign == 1 else float(after["close"].min())
        if sign * (max_fav_extreme - entry) >= 0.5 * dist - 1e-9:
            hit_half_r = True
        if sign * (max_fav_extreme - entry) >= 1.0 * dist - 1e-9:
            hit_one_r = True
        if sign * (max_fav_close - entry) >= 1.5 * dist - 1e-9 or sign * (max_fav_extreme - entry) >= 1.5 * dist - 1e-9:
            hit_15_r = True

    if hit_15_r and bars_open > 0:
        if sign == 1:
            raw_trail = float(after["high"].max()) - 1.25 * m5_atr
        else:
            raw_trail = float(after["low"].min()) + 1.25 * m5_atr
        trail_px = quantize(raw_trail, tick, up=(sign == 1))
        stop = tighten(stop, trail_px)

    if bars_open >= 6 and not hit_half_r and len(d) >= 2:
        last_bar = d.iloc[-1]
        prev_bar = d.iloc[-2]
        if sign == 1:
            mom_reversed = (
                float(last_bar.close) < float(last_bar.ema20)
                and float(last_bar.rsi) < 50.0
                and float(last_bar.close) < float(prev_bar.close)
            )
        else:
            mom_reversed = (
                float(last_bar.close) > float(last_bar.ema20)
                and float(last_bar.rsi) > 50.0
                and float(last_bar.close) > float(prev_bar.close)
            )
        if mom_reversed:
            return {
                "action": "EXIT",
                "exit_reason": "TIME_EXIT_6_BARS",
                "flag": "NONE",
                "stop": stop,
                "bars_open": bars_open,
                "reached_half_r": hit_half_r,
                "reached_one_r": hit_one_r,
                "reached_15_r": hit_15_r,
            }

    if bars_open >= 12 and not hit_one_r:
        return {
            "action": "EXIT",
            "exit_reason": "TIME_EXIT_12_BARS",
            "flag": "NONE",
            "stop": stop,
            "bars_open": bars_open,
            "reached_half_r": hit_half_r,
            "reached_one_r": hit_one_r,
            "reached_15_r": hit_15_r,
        }

    return {
        "action": "HOLD",
        "exit_reason": "NONE",
        "flag": "NONE",
        "stop": stop,
        "bars_open": bars_open,
        "reached_half_r": hit_half_r,
        "reached_one_r": hit_one_r,
        "reached_15_r": hit_15_r,
    }


# ============================================================================
# SECTION 14: GROQ AI ROLE — ADVISOR-ONLY MODE (DELIVERABLE 6)
# ============================================================================

def evaluate_hybrid_ai_advisor(result: Result, ai_response: dict | None = None) -> dict:
    """Section 14: Groq is a contextual advisor, NEVER a hard dependency or price calculator."""
    rule_valid = result.status == "VALID"
    ai = dict(ai_response or {})
    if not ai or not ai.get("ok"):
        ai_status = "AI_UNAVAILABLE"
        notes = ai.get("reasons") or ["Groq AI sedang tidak tersedia / rate-limited; dieksekusi berdasarkan aturan kuantitatif."]
    elif ai.get("decision") == result.direction:
        ai_status = "AI_ADVISORY_OK"
        notes = ai.get("reasons") or ["Setup selaras dengan tinjauan kontekstual AI."]
    else:
        ai_status = "AI_CAUTION"
        notes = (ai.get("cautions") or []) + (ai.get("reasons") or ["AI memberikan catatan kehati-hatian pada struktur pasar."])

    return {
        "rule_status": "RULE_BASED_VALID" if rule_valid else result.status,
        "ai_status": ai_status,
        "final_status": "VALID" if rule_valid else result.status,
        "paper_entry_allowed": bool(rule_valid),
        "advisory_notes": [str(x)[:200] for x in notes[:3]],
    }


# ============================================================================
# SECTION 15: TELEGRAM ALERT TEMPLATES & DEDUPLICATION (DELIVERABLE 6)
# ============================================================================

def hybrid_alert_dedup_key(
    symbol: str,
    timeframe: str,
    candle_close_ms: int,
    strategy: str,
    direction: str,
    stage: str,
) -> str:
    """Section 15.2: Deduplicate alerts by `symbol + timeframe + candle_close_ms + strategy + direction + stage`."""
    return ":".join(
        [
            "HYBRID_ALERT",
            str(symbol or "").strip().upper(),
            str(timeframe or "5m").strip().lower(),
            str(int(candle_close_ms or 0)),
            str(strategy or "F").strip().upper(),
            str(direction or "NONE").strip().upper(),
            str(stage or "EARLY_WATCH").strip().upper(),
        ]
    )


class HybridAlertDeduplicator:
    """Tracks sent alert keys and stage progression so identical stage messages are never repeated."""

    STAGE_RANK = {
        "EARLY_WATCH": 1,
        "SETUP_FORMING": 2,
        "PAPER_ENTRY": 3,
        "TRADE_UPDATE": 4,
    }

    def __init__(self):
        self._sent_keys: set[str] = set()
        self._highest_stage: dict[tuple, int] = {}

    def should_send(
        self,
        symbol: str,
        timeframe: str,
        candle_close_ms: int,
        strategy: str,
        direction: str,
        stage: str,
        event_id: str = "",
    ) -> bool:
        stage_u = str(stage or "EARLY_WATCH").strip().upper()
        key = hybrid_alert_dedup_key(symbol, timeframe, candle_close_ms, strategy, direction, stage_u)
        if stage_u == "TRADE_UPDATE" and event_id:
            key = f"{key}:{event_id}"
        if key in self._sent_keys:
            return False
        base_tuple = (
            str(symbol).strip().upper(),
            str(timeframe).strip().lower(),
            int(candle_close_ms or 0),
            str(strategy or "F").strip().upper(),
            str(direction or "NONE").strip().upper(),
        )
        rank = self.STAGE_RANK.get(stage_u, 0)
        prev_rank = self._highest_stage.get(base_tuple, 0)
        if stage_u != "TRADE_UPDATE" and rank <= prev_rank:
            return False
        self._sent_keys.add(key)
        if stage_u != "TRADE_UPDATE":
            self._highest_stage[base_tuple] = max(prev_rank, rank)
        return True


def format_hybrid_alert(stage: str, payload: dict) -> str:
    """Section 15.1: Format the 4 distinct Telegram alert templates."""
    st_u = str(stage or "EARLY_WATCH").strip().upper()
    symbol = payload.get("symbol", "BTCUSDT")
    direction = payload.get("direction", "LONG")
    side_emoji = "🟢" if direction in ("LONG", "BUY") else "🔴"
    strat = payload.get("strategy", "A")
    strat_name = STRATEGY_NAMES.get(strat, payload.get("strategy_name", strat))
    regime = payload.get("m15_regime", "TREND_UP")
    score = int(payload.get("setup_quality_score", payload.get("score", 60)))

    if st_u == "EARLY_WATCH":
        return "\n".join(
            [
                f"👀 [EARLY_WATCH] {side_emoji} {symbol} ({direction})",
                f"• Regime M15: {regime} · Strategi: {strat} ({strat_name})",
                f"• Setup Quality Score: {score}/100 (55–64 · Pantauan Awal — BUKAN instruksi entry)",
            ]
        )
    if st_u == "SETUP_FORMING":
        return "\n".join(
            [
                f"⚡ [SETUP_FORMING] {side_emoji} {symbol} ({direction})",
                f"• Regime M15: {regime} · Strategi: {strat} ({strat_name})",
                f"• Setup Quality Score: {score}/100 (65–77 · Menunggu konfirmasi trigger M5)",
            ]
        )
    if st_u == "PAPER_ENTRY":
        p = payload.get("plan") or payload
        ai_status = payload.get("ai_status", "AI_ADVISORY_OK")
        return "\n".join(
            [
                f"🚀 [PAPER_ENTRY] {side_emoji} {symbol} ({direction}) · Strategi {strat} ({strat_name})",
                f"• Regime M15: {regime} · Score: {score}/100 · Lev: {p.get('leverage', 2)}x",
                f"• Trigger: {p.get('entry_trigger')} · Limit: {p.get('entry')} · SL: {p.get('sl')}",
                f"• TP1 (+1R 40%): {p.get('tp1')} · TP2 (+2R 35%): {p.get('tp2')} · TP3 (+3R 25%): {p.get('tp3')}",
                f"• Risiko: {p.get('risk', 0.0):.2f} USDT (≤0,25% ekuitas) · AI: {ai_status}",
            ]
        )
    ev_type = payload.get("event", "TP1")
    note = payload.get("note", "")
    return "\n".join(
        [
            f"🔔 [TRADE_UPDATE · {ev_type}] {side_emoji} {symbol} ({direction})",
            f"• Harga: {payload.get('price', 'N/A')} · PnL: {payload.get('pnl', 0.0):+.2f} USDT",
            f"• Catatan: {note}",
        ]
    )


# ============================================================================
# SECTION 16: SCANNER PIPELINE (50/30/10/5) & 17-METRIC FUNNEL (DELIVERABLE 6)
# ============================================================================

def empty_hybrid_funnel() -> dict:
    """Section 16.2: Mandatory 17-metric funnel report for every scan cycle."""
    return {
        "universe_total": 0,
        "passed_stage1": 0,
        "passed_m15_history": 0,
        "regime_counts": {reg: 0 for reg in ALL_REGIMES},
        "strategy_candidate_counts": {k: 0 for k in (STRATEGY_A, STRATEGY_B, STRATEGY_C, STRATEGY_D, STRATEGY_E)},
        "early_watch_count": 0,
        "setup_forming_count": 0,
        "entry_candidate_count": 0,
        "paper_entries_created": 0,
        "blocked_by_liquidity_or_spread": 0,
        "blocked_by_sl_or_rr": 0,
        "blocked_by_risk_or_position_limit": 0,
        "blocked_by_news": 0,
        "blocked_by_cooldown_or_daily_loss": 0,
        "ai_advisory_ok": 0,
        "ai_caution": 0,
        "ai_unavailable": 0,
    }


class HybridRotatingQueue:
    """Section 16.1: Rotating queue ensuring Stage 1 (top 50), Stage 2 (30 shortlist balanced LONG/SHORT),
    Stage 3 (10 M5 candidates per cycle; priority >= 70 immediately, 55–69 rotated across cycles so all 30
    are covered within <= 3 cycles), and Stage 4 (5 deep order-book & AI advisory candidates).
    """

    def __init__(self, stage1_cap: int = 50, stage2_cap: int = 30, stage3_cap: int = 10, stage4_cap: int = 5):
        self.stage1_cap = int(stage1_cap)
        self.stage2_cap = int(stage2_cap)
        self.stage3_cap = int(stage3_cap)
        self.stage4_cap = int(stage4_cap)
        self.rotation_cursor = 0
        self.m15_cache: dict[tuple, dict] = {}
        self.m5_cache: dict[tuple, dict] = {}
        self.ai_cache: dict[tuple, dict] = {}

    def select_stage2_balanced(self, candidates: list[dict]) -> list[dict]:
        """Select up to `stage2_cap` (30) candidates with balanced bullish and bearish setups."""
        longs = sorted([c for c in candidates if c.get("direction") in ("LONG", "BUY")], key=lambda x: x.get("m15_score", 0), reverse=True)
        shorts = sorted([c for c in candidates if c.get("direction") in ("SHORT", "SELL")], key=lambda x: x.get("m15_score", 0), reverse=True)
        neutral = sorted([c for c in candidates if c.get("direction") not in ("LONG", "BUY", "SHORT", "SELL")], key=lambda x: x.get("m15_score", 0), reverse=True)
        out = []
        half = self.stage2_cap // 2
        out.extend(longs[:half])
        out.extend(shorts[:half])
        used = {x["symbol"] for x in out}
        rem = [c for c in (longs[half:] + shorts[half:] + neutral) if c["symbol"] not in used]
        rem.sort(key=lambda x: x.get("m15_score", 0), reverse=True)
        out.extend(rem[: max(0, self.stage2_cap - len(out))])
        return out[: self.stage2_cap]

    def select_stage3_batch(self, stage2_shortlist: list[dict]) -> list[dict]:
        """Select up to `stage3_cap` (10) candidates for M5 evaluation."""
        priority = [c for c in stage2_shortlist if float(c.get("m15_score", 0)) >= 70]
        rotatable = [c for c in stage2_shortlist if 55 <= float(c.get("m15_score", 0)) < 70]
        if not rotatable:
            rotatable = [c for c in stage2_shortlist if c not in priority]

        chosen = list(priority[: self.stage3_cap])
        slots_left = max(0, self.stage3_cap - len(chosen))
        if slots_left > 0 and rotatable:
            n = len(rotatable)
            start = self.rotation_cursor % n
            rotated = [rotatable[(start + i) % n] for i in range(min(slots_left, n))]
            self.rotation_cursor = (start + len(rotated)) % n
            chosen.extend(rotated)
        return chosen[: self.stage3_cap]

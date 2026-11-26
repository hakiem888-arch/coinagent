"""CoinAgentX v6.0.5 — Predictive Watchlist v1.0 Early Signal Engine & Lifecycle State Machine.

Path A (Predictive Detection):
  Public Futures Market Data -> M1/M5/M15 Intrabar Monitor -> Early Signal Engine
  -> PRE_WATCH (50-54) -> EARLY_WATCH (55-64) -> SETUP_FORMING (65-77) -> Watchlist & Telegram Alert
  (NEVER directly creates PAPER_ENTRY).

Path B (Confirmed Trading Setup):
  M15 Closed Regime -> Strategy Router A-E -> M5 Closed/Valid Trigger -> Setup Quality Score (>=78)
  -> All Hard Gates -> Groq Advisor Only -> CONFIRMED -> PAPER_ENTRY.

Scores represent technical setup quality (0-100), NEVER win probability or profit percentage.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import numpy as np
import pandas as pd

from .hybrid_adaptive import (
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
    detect_m15_regime,
)
from .indicators import confirmed_swing_pivots, indicators
from .models import MonitorSnapshot

# ---------------------------------------------------------------------------
# Lifecycle Stages & Event Types (Section G.1)
# ---------------------------------------------------------------------------
STAGE_PRE_WATCH = "PRE_WATCH"
STAGE_EARLY_WATCH = "EARLY_WATCH"
STAGE_SETUP_FORMING = "SETUP_FORMING"
STAGE_CONFIRMED = "CONFIRMED"
STAGE_PAPER_ENTRY = "PAPER_ENTRY"
STAGE_SIGNAL_STRENGTHENING = "SIGNAL_STRENGTHENING"
STAGE_SIGNAL_WEAKENING = "SIGNAL_WEAKENING"
STAGE_DIRECTION_CHANGED = "DIRECTION_CHANGED"
STAGE_SIGNAL_INVALIDATED = "SIGNAL_INVALIDATED"
STAGE_MISSED_ENTRY = "MISSED_ENTRY"
STAGE_EXPIRED = "EXPIRED"
STAGE_CONFLICT_WAIT = "CONFLICT_WAIT"
STAGE_NO_ENTRY_HIGH_VOLATILITY = "NO_ENTRY_HIGH_VOLATILITY"

ALL_PREDICTIVE_STAGES = (
    STAGE_PRE_WATCH,
    STAGE_EARLY_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_CONFIRMED,
    STAGE_PAPER_ENTRY,
    STAGE_SIGNAL_STRENGTHENING,
    STAGE_SIGNAL_WEAKENING,
    STAGE_DIRECTION_CHANGED,
    STAGE_SIGNAL_INVALIDATED,
    STAGE_MISSED_ENTRY,
    STAGE_EXPIRED,
    STAGE_CONFLICT_WAIT,
    STAGE_NO_ENTRY_HIGH_VOLATILITY,
)

ACTIVE_STAGES = frozenset({
    STAGE_PRE_WATCH,
    STAGE_EARLY_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_CONFIRMED,
})

TERMINAL_STAGES = frozenset({
    STAGE_PAPER_ENTRY,
    STAGE_DIRECTION_CHANGED,
    STAGE_SIGNAL_INVALIDATED,
    STAGE_MISSED_ENTRY,
    STAGE_EXPIRED,
    STAGE_NO_ENTRY_HIGH_VOLATILITY,
})

# Explicit state machine adjacency list — arbitrary illegal transitions are rejected!
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STAGE_PRE_WATCH: frozenset({
        STAGE_PRE_WATCH,
        STAGE_EARLY_WATCH,
        STAGE_SETUP_FORMING,
        STAGE_DIRECTION_CHANGED,
        STAGE_SIGNAL_INVALIDATED,
        STAGE_MISSED_ENTRY,
        STAGE_EXPIRED,
        STAGE_CONFLICT_WAIT,
        STAGE_NO_ENTRY_HIGH_VOLATILITY,
    }),
    STAGE_EARLY_WATCH: frozenset({
        STAGE_EARLY_WATCH,
        STAGE_SETUP_FORMING,
        STAGE_CONFIRMED,
        STAGE_DIRECTION_CHANGED,
        STAGE_SIGNAL_INVALIDATED,
        STAGE_MISSED_ENTRY,
        STAGE_EXPIRED,
        STAGE_CONFLICT_WAIT,
        STAGE_NO_ENTRY_HIGH_VOLATILITY,
    }),
    STAGE_SETUP_FORMING: frozenset({
        STAGE_SETUP_FORMING,
        STAGE_EARLY_WATCH,
        STAGE_CONFIRMED,
        STAGE_DIRECTION_CHANGED,
        STAGE_SIGNAL_INVALIDATED,
        STAGE_MISSED_ENTRY,
        STAGE_EXPIRED,
        STAGE_CONFLICT_WAIT,
        STAGE_NO_ENTRY_HIGH_VOLATILITY,
    }),
    STAGE_CONFIRMED: frozenset({
        STAGE_CONFIRMED,
        STAGE_PAPER_ENTRY,
        STAGE_DIRECTION_CHANGED,
        STAGE_SIGNAL_INVALIDATED,
        STAGE_MISSED_ENTRY,
        STAGE_EXPIRED,
        STAGE_NO_ENTRY_HIGH_VOLATILITY,
    }),
    STAGE_CONFLICT_WAIT: frozenset({
        STAGE_PRE_WATCH,
        STAGE_EARLY_WATCH,
        STAGE_SETUP_FORMING,
        STAGE_EXPIRED,
        STAGE_SIGNAL_INVALIDATED,
        STAGE_NO_ENTRY_HIGH_VOLATILITY,
    }),
    STAGE_PAPER_ENTRY: frozenset(),
    STAGE_DIRECTION_CHANGED: frozenset(),
    STAGE_SIGNAL_INVALIDATED: frozenset(),
    STAGE_MISSED_ENTRY: frozenset(),
    STAGE_EXPIRED: frozenset(),
    STAGE_NO_ENTRY_HIGH_VOLATILITY: frozenset(),
}


def is_valid_transition(from_stage: Optional[str], to_stage: str) -> bool:
    if to_stage not in ALL_PREDICTIVE_STAGES:
        return False
    if not from_stage:
        return to_stage in (
            STAGE_PRE_WATCH,
            STAGE_EARLY_WATCH,
            STAGE_SETUP_FORMING,
            STAGE_CONFIRMED,
            STAGE_CONFLICT_WAIT,
            STAGE_NO_ENTRY_HIGH_VOLATILITY,
        )
    allowed = VALID_TRANSITIONS.get(from_stage, frozenset())
    return to_stage in allowed


# ---------------------------------------------------------------------------
# Auditable Configuration (Section L)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PredictiveConfig:
    predictive_watchlist_enabled: bool = True
    early_monitor_max_candidates: int = 50
    monitor_interval_seconds: int = 10
    websocket_enabled: bool = True
    rest_fallback_enabled: bool = True
    pre_watch_threshold: float = 50.0
    early_watch_threshold: float = 55.0
    setup_forming_threshold: float = 65.0
    confirmed_threshold: float = 78.0
    pre_watch_ttl_seconds: int = 600
    early_watch_ttl_seconds: int = 900
    setup_forming_ttl_seconds: int = 900
    confirmed_ttl_seconds: int = 600
    max_chase_atr: float = 0.35
    max_chase_pct: float = 0.45
    stale_market_data_seconds: int = 30
    signal_dedup_window_seconds: int = 300
    max_early_alerts_per_cycle: int = 10
    max_groq_candidates: int = 5
    strengthening_delta: float = 4.0
    weakening_delta: float = -4.0

    def ttl_ms_for_stage(self, stage: str) -> int:
        st = str(stage or "").upper()
        if st == STAGE_PRE_WATCH:
            return int(self.pre_watch_ttl_seconds) * 1000
        if st == STAGE_EARLY_WATCH:
            return int(self.early_watch_ttl_seconds) * 1000
        if st == STAGE_SETUP_FORMING:
            return int(self.setup_forming_ttl_seconds) * 1000
        if st == STAGE_CONFIRMED:
            return int(self.confirmed_ttl_seconds) * 1000
        return int(self.early_watch_ttl_seconds) * 1000

    @classmethod
    def from_scanner_json(cls, path: Optional[Path | str] = None) -> "PredictiveConfig":
        cfg_path = Path(path) if path else Path(__file__).resolve().parent.parent / "config" / "scanner.json"
        if not cfg_path.exists():
            return cls()
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        pw = raw.get("predictive_watchlist", raw) if isinstance(raw, dict) else {}
        if not isinstance(pw, dict):
            return cls()
        kwargs = {}
        for f in cls.__dataclass_fields__:
            if f in pw:
                kwargs[f] = pw[f]
            elif f in raw:
                kwargs[f] = raw[f]
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Predictive Signal Data Model (Section F.4)
# ---------------------------------------------------------------------------
@dataclass
class PredictiveSignal:
    signal_id: str
    symbol: str
    direction: str                     # "LONG" or "SHORT"
    stage: str                         # PRE_WATCH / EARLY_WATCH / SETUP_FORMING / CONFIRMED / ...
    early_score: float                 # 0-100 provisional intrabar setup quality (NEVER win probability)
    setup_quality_score: Optional[float] = None  # Official closed-candle Hybrid Adaptive v1.1 score (0-100)
    confirmation_score: Optional[float] = None
    m15_regime: str = "UNKNOWN"        # Official closed-candle M15 regime ONLY
    provisional_context: str = ""      # Separated intrabar M15/M5/M1 context
    strategy_id: str = ""              # A-E when determinable
    source_timeframes: tuple[str, ...] = ("1m", "5m", "15m")
    m1_status: str = "NEUTRAL"
    m5_status: str = "NEUTRAL"
    detected_at_ms: int = 0
    updated_at_ms: int = 0
    detected_price: float = 0.0
    current_price: float = 0.0
    trigger_price: float = 0.0
    invalidation_price: float = 0.0
    distance_to_trigger_pct: float = 0.0
    distance_to_invalidation_pct: float = 0.0
    price_change_since_detected_pct: float = 0.0
    atr_value: float = 0.0
    atr_distance_to_trigger: float = 0.0
    max_permitted_entry_distance_pct: float = 0.45
    signal_age_seconds: float = 0.0
    provider: str = "Binance Futures"
    instrument_kind: str = "USD_M_PERPETUAL"
    confidence_label: str = "PROVISIONAL_EARLY_SETUP"
    reason_codes: list[str] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)
    data_freshness_ms: int = 0
    is_stale: bool = False
    expires_at_ms: int = 0
    signal_state: str = "ACTIVE"       # "ACTIVE" or "TERMINAL"
    entry_validity: str = "WATCH_ONLY" # WATCH_ONLY / VALID / CHASE_MISSED / INVALIDATED / EXPIRED / STALE / BLOCKED
    trend_state: str = "STABLE"        # STRENGTHENING / WEAKENING / STABLE
    alert_state: str = "PENDING"
    confirmed_plan: Optional[dict] = None
    hard_gates_passed: bool = False
    dedup_key: str = ""

    def __post_init__(self):
        if not self.dedup_key:
            self.dedup_key = f"{self.symbol}:{self.direction}:{self.signal_id}"
        self.signal_state = "ACTIVE" if self.stage in ACTIVE_STAGES else "TERMINAL"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_timeframes"] = list(self.source_timeframes)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PredictiveSignal":
        d = dict(data)
        if "source_timeframes" in d and isinstance(d["source_timeframes"], list):
            d["source_timeframes"] = tuple(d["source_timeframes"])
        valid_fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Feature Extraction Helpers (Zero Future Leakage)
# ---------------------------------------------------------------------------
def _filter_no_future(df: Optional[pd.DataFrame], now_ms: int, closed_only: bool = False) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if "open_time" not in df.columns or "close_time" not in df.columns:
        return df.copy()
    if closed_only:
        out = df[df["close_time"] < int(now_ms)].copy()
    else:
        out = df[df["open_time"] <= int(now_ms)].copy()
    return out.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)


def _safe_float(val, default=None):
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _detect_rsi_divergence(df: pd.DataFrame, direction: str, lookback: int = 24) -> bool:
    """Detect simple conservative bullish/bearish RSI divergence on confirmed pivots (zero lookahead)."""
    if df is None or len(df) < 20 or "rsi" not in df.columns:
        return False
    pivs = confirmed_swing_pivots(df, lookback=lookback, left=2, right=2)
    if direction == "LONG":
        lows = [p for p in pivs if p["kind"] == "LOW" and math.isfinite(p["rsi"])]
        if len(lows) >= 2:
            p1, p2 = lows[-2], lows[-1]
            return p2["price"] < p1["price"] and p2["rsi"] > p1["rsi"] + 2.0
    else:
        highs = [p for p in pivs if p["kind"] == "HIGH" and math.isfinite(p["rsi"])]
        if len(highs) >= 2:
            p1, p2 = highs[-2], highs[-1]
            return p2["price"] > p1["price"] and p2["rsi"] < p1["rsi"] - 2.0
    return False


def _detect_liquidity_sweep(df: pd.DataFrame, direction: str, lookback: int = 16) -> bool:
    """Detect intrabar/recent sweep of prior swing low (bullish) or swing high (bearish) with reclaim."""
    if df is None or len(df) < lookback + 2:
        return False
    recent = df.iloc[-1]
    prior = df.iloc[-(lookback + 1):-1]
    if direction == "LONG":
        prior_low = float(prior["low"].min())
        return float(recent["low"]) < prior_low and float(recent["close"]) > prior_low
    else:
        prior_high = float(prior["high"].max())
        return float(recent["high"]) > prior_high and float(recent["close"]) < prior_high


def _detect_early_choch(df: pd.DataFrame, direction: str, lookback: int = 12) -> bool:
    """Detect early Change of Character (micro break of prior local swing)."""
    if df is None or len(df) < lookback + 2:
        return False
    recent = df.iloc[-1]
    prior = df.iloc[-(lookback + 1):-1]
    if direction == "LONG":
        local_high = float(prior["high"].iloc[-6:].max())
        return float(recent["close"]) > local_high
    else:
        local_low = float(prior["low"].iloc[-6:].min())
        return float(recent["close"]) < local_low


def _infer_strategy_from_regime(m15_regime: str, has_sweep: bool, has_pullback: bool) -> str:
    """Mirror Hybrid Adaptive v1.1 regime routing without modifying official regime rules."""
    if m15_regime in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
        return STRATEGY_B
    if m15_regime in (REGIME_TREND_UP, REGIME_TREND_DOWN):
        return STRATEGY_A
    if m15_regime in (REGIME_STRONG_TREND_UP, REGIME_STRONG_TREND_DOWN):
        return STRATEGY_A if has_pullback else STRATEGY_C
    if m15_regime == REGIME_RANGE:
        return STRATEGY_E if has_sweep else STRATEGY_D
    if m15_regime == REGIME_TRANSITION:
        return STRATEGY_E if has_sweep else ""
    return ""


def _confidence_label_for_stage(stage: str) -> str:
    """Setup quality label — explicitly NEVER a profit probability."""
    mapping = {
        STAGE_PRE_WATCH: "PROVISIONAL_EARLY_SETUP_NOT_ENTRY",
        STAGE_EARLY_WATCH: "EARLY_STRUCTURE_WATCH_NOT_ENTRY",
        STAGE_SETUP_FORMING: "M5_FORMING_WAIT_CONFIRMATION",
        STAGE_CONFIRMED: "CONFIRMED_TECHNICAL_SETUP_PAPER_ONLY",
        STAGE_PAPER_ENTRY: "PAPER_ENTRY_EXECUTED",
        STAGE_CONFLICT_WAIT: "REGIME_OR_TF_CONFLICT_WAIT",
        STAGE_NO_ENTRY_HIGH_VOLATILITY: "BLOCKED_HIGH_VOLATILITY",
    }
    return mapping.get(stage, "PROVISIONAL_SETUP_QUALITY")


def _score_direction(
    direction: str,
    m1_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    m15_closed: pd.DataFrame,
    m15_live: pd.DataFrame,
    m15_regime: str,
    spread_pct: Optional[float],
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    """Compute symmetric provisional early score (0-100) for LONG or SHORT.

    Returns (score, reason_codes, missing_features, details).
    Single-indicator protection: requires >=2 aligned factors; missing features are
    marked transparently and never auto-passed.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    score = 0.0
    reasons: list[str] = []
    missing: list[str] = []
    factors_hit = 0
    m1_hits = 0
    m5_hits = 0
    details: dict[str, Any] = {
        "m1_status": "NEUTRAL",
        "m5_status": "NEUTRAL",
        "has_sweep": False,
        "has_pullback": False,
        "has_choch": False,
        "m5_atr": 0.0,
    }

    # --- 1. M1 Micro-Structure & Momentum (max 32 pts) ---
    if m1_df is not None and len(m1_df) >= 22:
        d1 = indicators(m1_df)
        last1 = d1.iloc[-1]
        prev1 = d1.iloc[-2]
        prev3 = d1.iloc[-4] if len(d1) >= 4 else prev1
        c_now = _safe_float(last1["close"])
        c_prev3 = _safe_float(prev3["close"])
        ema20 = _safe_float(last1.get("ema20"))
        ema50 = _safe_float(last1.get("ema50"))
        rsi1 = _safe_float(last1.get("rsi"))
        macd_h1 = _safe_float(last1.get("macd_hist"))
        macd_h0 = _safe_float(prev1.get("macd_hist"))
        rvol1 = _safe_float(last1.get("rvol20"))

        # Micro momentum & velocity acceleration (max 9 pts)
        if c_now and c_prev3 and c_prev3 > 0:
            roc3 = (c_now - c_prev3) / c_prev3 * 100.0 * sign
            roc1 = (c_now - float(prev1["close"])) / float(prev1["close"]) * 100.0 * sign if float(prev1["close"]) > 0 else 0.0
            if roc3 >= 0.08 and roc1 > 0:
                pts = 9.0 if roc3 >= 0.18 else 6.0
                score += pts
                factors_hit += 1
                m1_hits += 1
                reasons.append(f"M1_MICRO_MOMENTUM_{direction}")
        else:
            missing.append("m1_micro_momentum")

        # EMA20/EMA50 alignment (max 7 pts)
        if ema20 is not None and c_now is not None:
            if ema50 is not None:
                aligned = (c_now > ema20 > ema50) if direction == "LONG" else (c_now < ema20 < ema50)
                partial = (c_now > ema20) if direction == "LONG" else (c_now < ema20)
                if aligned:
                    score += 7.0
                    factors_hit += 1
                    m1_hits += 1
                    reasons.append(f"M1_EMA20_50_ALIGNED_{direction}")
                elif partial:
                    score += 4.0
                    m1_hits += 1
            else:
                missing.append("m1_ema50")
                if (c_now > ema20) if direction == "LONG" else (c_now < ema20):
                    score += 3.5
                    m1_hits += 1
        else:
            missing.append("m1_ema20")

        # M1 RSI & MACD slope (max 9 pts)
        if rsi1 is not None:
            rsi_ok = (52.0 <= rsi1 <= 74.0) if direction == "LONG" else (26.0 <= rsi1 <= 48.0)
            if rsi_ok:
                score += 5.0
                factors_hit += 1
                m1_hits += 1
                reasons.append(f"M1_RSI_SUPPORT_{direction}")
        else:
            missing.append("m1_rsi")

        if macd_h1 is not None and macd_h0 is not None:
            if (macd_h1 > macd_h0 and macd_h1 > -0.0001) if direction == "LONG" else (macd_h1 < macd_h0 and macd_h1 < 0.0001):
                score += 4.0
                m1_hits += 1
                reasons.append(f"M1_MACD_ACCEL_{direction}")
        else:
            missing.append("m1_macd")

        # M1 Relative Volume & Micro Swing (max 7 pts)
        if rvol1 is not None:
            if rvol1 >= 1.25:
                score += 4.5
                factors_hit += 1
                m1_hits += 1
                reasons.append("M1_RVOL_EXPANSION")
        else:
            missing.append("m1_rvol")

        if spread_pct is not None and spread_pct >= 0:
            if spread_pct <= 0.08:
                score += 2.5
            elif spread_pct > 0.25:
                score = max(0.0, score - 5.0)
                reasons.append("WIDE_SPREAD_PENALTY")
        else:
            missing.append("bid_ask_spread")

        details["m1_status"] = (
            f"BULL_IMPULSE ({m1_hits}/5)" if (direction == "LONG" and m1_hits >= 3)
            else f"BEAR_IMPULSE ({m1_hits}/5)" if (direction == "SHORT" and m1_hits >= 3)
            else f"EARLY_{direction} ({m1_hits}/5)" if m1_hits >= 1
            else "NEUTRAL"
        )
    else:
        missing.append("m1_candles_insufficient")

    # --- 2. M5 Momentum, Structure, CHoCH, Sweep, Divergence, VWAP, Volume (max 40 pts) ---
    if m5_df is not None and len(m5_df) >= 25:
        d5 = indicators(m5_df)
        last5 = d5.iloc[-1]
        prev5 = d5.iloc[-2]
        c5 = _safe_float(last5["close"])
        ema20_5 = _safe_float(last5.get("ema20"))
        ema50_5 = _safe_float(last5.get("ema50"))
        vwap5 = _safe_float(last5.get("vwap"))
        atr5 = _safe_float(last5.get("atr"), 0.0) or 0.0
        rsi5 = _safe_float(last5.get("rsi"))
        rvol5 = _safe_float(last5.get("rvol20"))
        don_hi = _safe_float(last5.get("donchian20_high"))
        don_lo = _safe_float(last5.get("donchian20_low"))
        details["m5_atr"] = atr5

        # M5 Structure / VWAP / EMA (max 10 pts)
        if c5 is not None and vwap5 is not None:
            vwap_ok = (c5 >= vwap5) if direction == "LONG" else (c5 <= vwap5)
            if vwap_ok:
                score += 5.0
                factors_hit += 1
                m5_hits += 1
                reasons.append(f"M5_VWAP_ALIGNED_{direction}")
        else:
            missing.append("m5_vwap")

        if c5 is not None and ema20_5 is not None:
            ema_ok = (c5 > ema20_5) if direction == "LONG" else (c5 < ema20_5)
            if ema_ok:
                score += 5.0
                m5_hits += 1
                reasons.append(f"M5_EMA20_ALIGNED_{direction}")
            # Pullback proximity to EMA20/EMA50
            if atr5 > 0 and abs(c5 - ema20_5) <= 0.8 * atr5:
                details["has_pullback"] = True
        else:
            missing.append("m5_ema20")

        # Early CHoCH / Breakout proximity / Retest (max 12 pts)
        has_choch = _detect_early_choch(d5, direction)
        details["has_choch"] = has_choch
        if has_choch:
            score += 8.0
            factors_hit += 1
            m5_hits += 1
            reasons.append(f"M5_EARLY_CHOCH_{direction}")

        if c5 is not None and atr5 > 0:
            if direction == "LONG" and don_hi is not None:
                dist = (don_hi - c5) / atr5
                if -0.25 <= dist <= 0.75:
                    score += 6.0
                    factors_hit += 1
                    m5_hits += 1
                    reasons.append("M5_BREAKOUT_PROXIMITY_LONG")
            elif direction == "SHORT" and don_lo is not None:
                dist = (c5 - don_lo) / atr5
                if -0.25 <= dist <= 0.75:
                    score += 6.0
                    factors_hit += 1
                    m5_hits += 1
                    reasons.append("M5_BREAKDOWN_PROXIMITY_SHORT")

        # Sweep & Divergence (max 10 pts)
        has_sweep = _detect_liquidity_sweep(d5, direction)
        details["has_sweep"] = has_sweep
        if has_sweep:
            score += 6.0
            factors_hit += 1
            m5_hits += 1
            reasons.append(f"M5_LIQUIDITY_SWEEP_{direction}")

        if _detect_rsi_divergence(d5, direction):
            score += 5.0
            factors_hit += 1
            m5_hits += 1
            reasons.append(f"M5_RSI_DIVERGENCE_{direction}")

        # Volume confirmation & M5 momentum (max 8 pts)
        if rvol5 is not None:
            if rvol5 >= 1.15:
                score += 5.0
                factors_hit += 1
                m5_hits += 1
                reasons.append("M5_VOLUME_CONFIRM")
        else:
            missing.append("m5_rvol")

        if rsi5 is not None:
            if (50.0 <= rsi5 <= 72.0) if direction == "LONG" else (28.0 <= rsi5 <= 50.0):
                score += 3.0
                m5_hits += 1

        details["m5_status"] = (
            f"FORMING_{direction} ({m5_hits}/6)" if m5_hits >= 3
            else f"BUILDING_{direction} ({m5_hits}/6)" if m5_hits >= 1
            else "NEUTRAL"
        )
    else:
        missing.append("m5_candles_insufficient")

    # --- 3. M15 Official Closed Regime & Higher-TF Context (max 28 pts) ---
    bull_regimes = (REGIME_TREND_UP, REGIME_STRONG_TREND_UP, REGIME_BREAKOUT_UP)
    bear_regimes = (REGIME_TREND_DOWN, REGIME_STRONG_TREND_DOWN, REGIME_BREAKOUT_DOWN)
    if direction == "LONG":
        if m15_regime in bull_regimes:
            score += 20.0
            factors_hit += 1
            reasons.append(f"M15_REGIME_ALIGNED_{m15_regime}")
        elif m15_regime == REGIME_RANGE and details["has_sweep"]:
            score += 14.0
            factors_hit += 1
            reasons.append("M15_RANGE_SWEEP_LONG")
        elif m15_regime == REGIME_TRANSITION and details["has_sweep"]:
            score += 12.0
            factors_hit += 1
            reasons.append("M15_TRANSITION_SWEEP_LONG")
        elif m15_regime in bear_regimes:
            score = max(0.0, score - 12.0)
            reasons.append("M15_COUNTER_TREND_PENALTY")
    else:
        if m15_regime in bear_regimes:
            score += 20.0
            factors_hit += 1
            reasons.append(f"M15_REGIME_ALIGNED_{m15_regime}")
        elif m15_regime == REGIME_RANGE and details["has_sweep"]:
            score += 14.0
            factors_hit += 1
            reasons.append("M15_RANGE_SWEEP_SHORT")
        elif m15_regime == REGIME_TRANSITION and details["has_sweep"]:
            score += 12.0
            factors_hit += 1
            reasons.append("M15_TRANSITION_SWEEP_SHORT")
        elif m15_regime in bull_regimes:
            score = max(0.0, score - 12.0)
            reasons.append("M15_COUNTER_TREND_PENALTY")

    # M15 S/R & live provisional context (max 8 pts, strictly separated from official closed regime)
    if m15_live is not None and len(m15_live) >= 20:
        last15 = m15_live.iloc[-1]
        c15 = _safe_float(last15["close"])
        o15 = _safe_float(last15["open"])
        if c15 and o15 and ((c15 > o15) if direction == "LONG" else (c15 < o15)):
            score += 8.0
            factors_hit += 1
            reasons.append(f"M15_PROVISIONAL_BODY_{direction}")
    else:
        missing.append("m15_live_context")

    # Anti-single-indicator guardrail: 1 indicator alone can NEVER produce high-quality setup
    if factors_hit < 2:
        score = min(score, 45.0)
        reasons.append("SINGLE_INDICATOR_CAPPED")

    # Cap Path A early score at 77.0 — ONLY Path B (CONFIRMED + all hard gates) reaches >=78!
    score = round(max(0.0, min(77.0, score)), 2)
    details["factors_hit"] = factors_hit
    return score, reasons, missing, details


def _compute_trigger_and_invalidation(
    direction: str,
    current_price: float,
    m5_df: pd.DataFrame,
    atr: float,
) -> tuple[float, float]:
    """Compute actionable trigger price and structural invalidation level for WATCH/FORMING."""
    eff_atr = atr if (atr and atr > 0) else max(current_price * 0.0025, 1e-8)
    if m5_df is not None and len(m5_df) >= 6:
        recent_hi = float(m5_df["high"].iloc[-6:].max())
        recent_lo = float(m5_df["low"].iloc[-6:].min())
    else:
        recent_hi = current_price + 0.35 * eff_atr
        recent_lo = current_price - 0.35 * eff_atr

    if direction == "LONG":
        trigger = max(current_price, min(recent_hi, current_price + 0.40 * eff_atr))
        invalidation = min(recent_lo - 0.15 * eff_atr, current_price - 1.0 * eff_atr)
    else:
        trigger = min(current_price, max(recent_lo, current_price - 0.40 * eff_atr))
        invalidation = max(recent_hi + 0.15 * eff_atr, current_price + 1.0 * eff_atr)
    return round(float(trigger), 8), round(float(invalidation), 8)


def evaluate_early_signal(
    snapshot: MonitorSnapshot,
    *,
    now_ms: Optional[int] = None,
    config: Optional[PredictiveConfig] = None,
) -> Optional[PredictiveSignal]:
    """Path A — Symmetric BUY/SELL Predictive Early Signal Engine.

    - Strictly uses only data available at `now_ms` (zero look-ahead).
    - Official M15 regime is computed ONLY from closed M15 candles; M1 never alters M15 regime.
    - Never emits `CONFIRMED` or `PAPER_ENTRY` (Path B exclusive).
    - Never emits active signals on stale data.
    """
    cfg = config or PredictiveConfig()
    if not cfg.predictive_watchlist_enabled:
        return None
    eval_ms = int(now_ms if now_ms is not None else snapshot.local_receive_ms or int(time.time() * 1000))
    freshness_ms = max(0, eval_ms - int(snapshot.exchange_timestamp_ms or eval_ms))
    is_stale = bool(snapshot.is_stale or freshness_ms > int(cfg.stale_market_data_seconds) * 1000)
    if is_stale:
        return None

    m1_live = _filter_no_future(snapshot.frames_live.get("1m"), eval_ms, closed_only=False)
    m5_live = _filter_no_future(snapshot.frames_live.get("5m"), eval_ms, closed_only=False)
    m15_closed = _filter_no_future(snapshot.frames_closed.get("15m"), eval_ms, closed_only=True)
    m15_live = _filter_no_future(snapshot.frames_live.get("15m"), eval_ms, closed_only=False)

    # Official M15 closed regime (M1 NEVER modifies official M15 regime!)
    if len(m15_closed) >= 30:
        d15_closed = indicators(m15_closed)
        reg_info = detect_m15_regime(d15_closed)
        m15_regime = str(reg_info.get("regime", "UNKNOWN"))
    else:
        m15_regime = "UNKNOWN"

    # Separated provisional M15 context from live intrabar M15 candle
    provisional_ctx = "UNAVAILABLE"
    if len(m15_live) >= 30:
        d15_live = indicators(m15_live)
        prov_info = detect_m15_regime(d15_live)
        provisional_ctx = f"PROVISIONAL_M15:{prov_info.get('regime', 'UNKNOWN')}"
    elif len(m15_live) > 0:
        r15 = m15_live.iloc[-1]
        provisional_ctx = "PROVISIONAL_M15_BULL" if float(r15["close"]) >= float(r15["open"]) else "PROVISIONAL_M15_BEAR"

    current_price = float(snapshot.reference_price or snapshot.last_price)
    if current_price <= 0:
        return None

    # HIGH_VOLATILITY M15 regime -> explicitly blocked (Section G.1 & J.1)
    if m15_regime == REGIME_HIGH_VOLATILITY:
        return PredictiveSignal(
            signal_id=f"pw-{uuid4().hex[:12]}",
            symbol=snapshot.symbol,
            direction="LONG",
            stage=STAGE_NO_ENTRY_HIGH_VOLATILITY,
            early_score=0.0,
            m15_regime=m15_regime,
            provisional_context=provisional_ctx,
            detected_at_ms=eval_ms,
            updated_at_ms=eval_ms,
            detected_price=current_price,
            current_price=current_price,
            trigger_price=current_price,
            invalidation_price=current_price,
            provider=snapshot.provider,
            instrument_kind=snapshot.instrument_kind,
            confidence_label=_confidence_label_for_stage(STAGE_NO_ENTRY_HIGH_VOLATILITY),
            reason_codes=["M15_HIGH_VOLATILITY_BLOCKED"],
            data_freshness_ms=freshness_ms,
            is_stale=False,
            expires_at_ms=eval_ms + cfg.ttl_ms_for_stage(STAGE_PRE_WATCH),
            entry_validity="BLOCKED",
        )

    spread_pct = snapshot.spread_pct if (snapshot.bid > 0 and snapshot.ask > 0) else None
    long_score, long_reasons, long_missing, long_det = _score_direction(
        "LONG", m1_live, m5_live, m15_closed, m15_live, m15_regime, spread_pct
    )
    short_score, short_reasons, short_missing, short_det = _score_direction(
        "SHORT", m1_live, m5_live, m15_closed, m15_live, m15_regime, spread_pct
    )

    # Detect regime / directional conflict when both sides score strongly
    if (
        long_score >= cfg.pre_watch_threshold
        and short_score >= cfg.pre_watch_threshold
        and abs(long_score - short_score) < 6.0
    ):
        return PredictiveSignal(
            signal_id=f"pw-{uuid4().hex[:12]}",
            symbol=snapshot.symbol,
            direction="LONG" if long_score >= short_score else "SHORT",
            stage=STAGE_CONFLICT_WAIT,
            early_score=max(long_score, short_score),
            m15_regime=m15_regime,
            provisional_context=provisional_ctx,
            detected_at_ms=eval_ms,
            updated_at_ms=eval_ms,
            detected_price=current_price,
            current_price=current_price,
            trigger_price=current_price,
            invalidation_price=current_price,
            provider=snapshot.provider,
            instrument_kind=snapshot.instrument_kind,
            confidence_label=_confidence_label_for_stage(STAGE_CONFLICT_WAIT),
            reason_codes=["BIDIRECTIONAL_CONFLICT_WAIT"],
            missing_features=sorted(set(long_missing + short_missing)),
            data_freshness_ms=freshness_ms,
            is_stale=False,
            expires_at_ms=eval_ms + cfg.ttl_ms_for_stage(STAGE_PRE_WATCH),
            entry_validity="BLOCKED",
        )

    if long_score >= short_score:
        direction, score, reasons, missing, det = "LONG", long_score, long_reasons, long_missing, long_det
    else:
        direction, score, reasons, missing, det = "SHORT", short_score, short_reasons, short_missing, short_det

    if score < cfg.pre_watch_threshold:
        return None

    if score >= cfg.setup_forming_threshold:
        stage = STAGE_SETUP_FORMING
    elif score >= cfg.early_watch_threshold:
        stage = STAGE_EARLY_WATCH
    else:
        stage = STAGE_PRE_WATCH

    atr5 = float(det.get("m5_atr") or 0.0)
    trigger_px, inval_px = _compute_trigger_and_invalidation(direction, current_price, m5_live, atr5)
    dist_trig_pct = abs(trigger_px - current_price) / current_price * 100.0 if current_price > 0 else 0.0
    dist_inval_pct = abs(current_price - inval_px) / current_price * 100.0 if current_price > 0 else 0.0
    atr_dist = abs(trigger_px - current_price) / atr5 if atr5 > 0 else 0.0
    strat_id = _infer_strategy_from_regime(m15_regime, bool(det.get("has_sweep")), bool(det.get("has_pullback")))

    return PredictiveSignal(
        signal_id=f"pw-{uuid4().hex[:12]}",
        symbol=snapshot.symbol,
        direction=direction,
        stage=stage,
        early_score=score,
        setup_quality_score=None,
        confirmation_score=None,
        m15_regime=m15_regime,
        provisional_context=provisional_ctx,
        strategy_id=strat_id,
        source_timeframes=tuple(tf for tf in ("1m", "5m", "15m") if tf in snapshot.frames_live),
        m1_status=str(det.get("m1_status", "NEUTRAL")),
        m5_status=str(det.get("m5_status", "NEUTRAL")),
        detected_at_ms=eval_ms,
        updated_at_ms=eval_ms,
        detected_price=current_price,
        current_price=current_price,
        trigger_price=trigger_px,
        invalidation_price=inval_px,
        distance_to_trigger_pct=round(dist_trig_pct, 4),
        distance_to_invalidation_pct=round(dist_inval_pct, 4),
        price_change_since_detected_pct=0.0,
        atr_value=atr5,
        atr_distance_to_trigger=round(atr_dist, 4),
        max_permitted_entry_distance_pct=float(cfg.max_chase_pct),
        signal_age_seconds=0.0,
        provider=snapshot.provider,
        instrument_kind=snapshot.instrument_kind,
        confidence_label=_confidence_label_for_stage(stage),
        reason_codes=reasons,
        missing_features=missing,
        data_freshness_ms=freshness_ms,
        is_stale=False,
        expires_at_ms=eval_ms + cfg.ttl_ms_for_stage(stage),
        entry_validity="WATCH_ONLY",
    )


# ---------------------------------------------------------------------------
# Signal Lifecycle State Machine (Section G.1–G.3)
# ---------------------------------------------------------------------------
@dataclass
class LifecycleTransitionResult:
    signal: PredictiveSignal
    events: list[dict[str, Any]] = field(default_factory=list)
    replaced_signal: Optional[PredictiveSignal] = None
    should_alert: bool = False
    alert_event_type: Optional[str] = None


def check_anti_chase(
    signal: PredictiveSignal,
    current_price: float,
    config: PredictiveConfig,
) -> tuple[bool, str]:
    """Return (is_chased, detail) when price has moved past trigger beyond permitted tolerance."""
    if current_price <= 0 or signal.trigger_price <= 0:
        return False, ""
    if signal.direction == "LONG":
        overshoot = current_price - signal.trigger_price
    else:
        overshoot = signal.trigger_price - current_price
    if overshoot <= 0:
        return False, ""
    overshoot_pct = overshoot / signal.trigger_price * 100.0
    overshoot_atr = (overshoot / signal.atr_value) if signal.atr_value > 0 else 0.0
    if overshoot_pct > float(config.max_chase_pct) or (signal.atr_value > 0 and overshoot_atr > float(config.max_chase_atr)):
        return True, f"overshoot_pct={overshoot_pct:.3f}% overshoot_atr={overshoot_atr:.2f}"
    return False, ""


def check_invalidation(signal: PredictiveSignal, current_price: float) -> bool:
    if current_price <= 0 or signal.invalidation_price <= 0:
        return False
    if signal.direction == "LONG":
        return current_price <= signal.invalidation_price
    return current_price >= signal.invalidation_price


class PredictiveLifecycleManager:
    """Deterministic state machine for Predictive Watchlist signals with anti-chase,
    invalidation, TTL expiry, direction change replacement, and Path B confirmation gating.
    """

    def __init__(self, config: Optional[PredictiveConfig] = None):
        self.config = config or PredictiveConfig()

    def _make_event(
        self,
        sig: PredictiveSignal,
        from_stage: str,
        to_stage: str,
        event_type: str,
        now_ms: int,
        reason: str = "",
    ) -> dict[str, Any]:
        return {
            "event_id": f"evt-{uuid4().hex[:14]}",
            "signal_id": sig.signal_id,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "from_stage": from_stage,
            "to_stage": to_stage,
            "event_type": event_type,
            "early_score": float(sig.early_score),
            "setup_quality_score": sig.setup_quality_score,
            "price": float(sig.current_price),
            "provider": sig.provider,
            "at_ms": int(now_ms),
            "reason": reason,
        }

    def process_early_update(
        self,
        existing: Optional[PredictiveSignal],
        candidate: Optional[PredictiveSignal],
        *,
        current_price: float,
        now_ms: int,
    ) -> Optional[LifecycleTransitionResult]:
        """Advance an existing active signal or register a new early signal (Path A)."""
        now_ms = int(now_ms)
        if existing is not None and existing.signal_state == "ACTIVE":
            # 1. Check TTL Expiry
            if existing.expires_at_ms > 0 and now_ms >= existing.expires_at_ms:
                prev_stage = existing.stage
                existing.stage = STAGE_EXPIRED
                existing.signal_state = "TERMINAL"
                existing.entry_validity = "EXPIRED"
                existing.updated_at_ms = now_ms
                existing.current_price = current_price or existing.current_price
                existing.signal_age_seconds = max(0.0, (now_ms - existing.detected_at_ms) / 1000.0)
                evt = self._make_event(existing, prev_stage, STAGE_EXPIRED, STAGE_EXPIRED, now_ms, "TTL expired")
                return LifecycleTransitionResult(existing, [evt], should_alert=True, alert_event_type=STAGE_EXPIRED)

            # 2. Check Invalidation
            px = float(current_price or existing.current_price)
            if check_invalidation(existing, px):
                prev_stage = existing.stage
                existing.stage = STAGE_SIGNAL_INVALIDATED
                existing.signal_state = "TERMINAL"
                existing.entry_validity = "INVALIDATED"
                existing.updated_at_ms = now_ms
                existing.current_price = px
                existing.signal_age_seconds = max(0.0, (now_ms - existing.detected_at_ms) / 1000.0)
                evt = self._make_event(existing, prev_stage, STAGE_SIGNAL_INVALIDATED, STAGE_SIGNAL_INVALIDATED, now_ms, "Invalidation level breached")
                return LifecycleTransitionResult(existing, [evt], should_alert=True, alert_event_type=STAGE_SIGNAL_INVALIDATED)

            # 3. Check Anti-Chase (MISSED_ENTRY)
            chased, chase_detail = check_anti_chase(existing, px, self.config)
            if chased and existing.stage in (STAGE_SETUP_FORMING, STAGE_CONFIRMED):
                prev_stage = existing.stage
                existing.stage = STAGE_MISSED_ENTRY
                existing.signal_state = "TERMINAL"
                existing.entry_validity = "CHASE_MISSED"
                existing.updated_at_ms = now_ms
                existing.current_price = px
                existing.signal_age_seconds = max(0.0, (now_ms - existing.detected_at_ms) / 1000.0)
                evt = self._make_event(existing, prev_stage, STAGE_MISSED_ENTRY, STAGE_MISSED_ENTRY, now_ms, f"Anti-chase triggered: {chase_detail}")
                return LifecycleTransitionResult(existing, [evt], should_alert=True, alert_event_type=STAGE_MISSED_ENTRY)

            # 4. If candidate is None, update live price metrics on existing
            if candidate is None:
                existing.current_price = px
                existing.updated_at_ms = now_ms
                existing.signal_age_seconds = max(0.0, (now_ms - existing.detected_at_ms) / 1000.0)
                if existing.detected_price > 0:
                    existing.price_change_since_detected_pct = round((px - existing.detected_price) / existing.detected_price * 100.0, 4)
                return LifecycleTransitionResult(existing, [], should_alert=False)

            # 5. HIGH_VOLATILITY block
            if candidate.stage == STAGE_NO_ENTRY_HIGH_VOLATILITY:
                prev_stage = existing.stage
                existing.stage = STAGE_NO_ENTRY_HIGH_VOLATILITY
                existing.signal_state = "TERMINAL"
                existing.entry_validity = "BLOCKED"
                existing.m15_regime = candidate.m15_regime
                existing.updated_at_ms = now_ms
                evt = self._make_event(existing, prev_stage, STAGE_NO_ENTRY_HIGH_VOLATILITY, STAGE_NO_ENTRY_HIGH_VOLATILITY, now_ms, "M15 HIGH_VOLATILITY")
                return LifecycleTransitionResult(existing, [evt], should_alert=True, alert_event_type=STAGE_NO_ENTRY_HIGH_VOLATILITY)

            # 6. Direction Change: cancel old signal (preserve history!), create NEW signal_id
            if candidate.direction != existing.direction and candidate.stage in ACTIVE_STAGES:
                prev_stage = existing.stage
                existing.stage = STAGE_DIRECTION_CHANGED
                existing.signal_state = "TERMINAL"
                existing.entry_validity = "INVALIDATED"
                existing.updated_at_ms = now_ms
                existing.current_price = px
                evt_cancel = self._make_event(
                    existing,
                    prev_stage,
                    STAGE_DIRECTION_CHANGED,
                    STAGE_DIRECTION_CHANGED,
                    now_ms,
                    f"Direction reversed {existing.direction} -> {candidate.direction}",
                )
                # Ensure brand new signal_id for the opposite direction
                if candidate.signal_id == existing.signal_id:
                    candidate.signal_id = f"pw-{uuid4().hex[:12]}"
                    candidate.dedup_key = f"{candidate.symbol}:{candidate.direction}:{candidate.signal_id}"
                evt_new = self._make_event(candidate, "", candidate.stage, candidate.stage, now_ms, "New opposite-direction signal created")
                return LifecycleTransitionResult(
                    signal=candidate,
                    events=[evt_cancel, evt_new],
                    replaced_signal=existing,
                    should_alert=True,
                    alert_event_type=STAGE_DIRECTION_CHANGED,
                )

            # 7. Same-direction evolution: validate transition, detect strengthening / weakening
            prev_stage = existing.stage
            prev_score = float(existing.early_score)
            target_stage = candidate.stage
            # Path A never downgrades a CONFIRMED signal back to PRE_WATCH silently
            if prev_stage == STAGE_CONFIRMED and target_stage in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING):
                target_stage = STAGE_CONFIRMED

            if not is_valid_transition(prev_stage, target_stage):
                return LifecycleTransitionResult(existing, [], should_alert=False)

            score_delta = float(candidate.early_score) - prev_score
            existing.early_score = float(candidate.early_score)
            existing.m15_regime = candidate.m15_regime
            existing.provisional_context = candidate.provisional_context
            existing.strategy_id = candidate.strategy_id or existing.strategy_id
            existing.m1_status = candidate.m1_status
            existing.m5_status = candidate.m5_status
            existing.current_price = px
            existing.updated_at_ms = now_ms
            existing.signal_age_seconds = max(0.0, (now_ms - existing.detected_at_ms) / 1000.0)
            existing.distance_to_trigger_pct = candidate.distance_to_trigger_pct
            existing.distance_to_invalidation_pct = candidate.distance_to_invalidation_pct
            existing.atr_distance_to_trigger = candidate.atr_distance_to_trigger
            existing.reason_codes = list(candidate.reason_codes)
            existing.missing_features = list(candidate.missing_features)
            existing.data_freshness_ms = candidate.data_freshness_ms
            if existing.detected_price > 0:
                existing.price_change_since_detected_pct = round((px - existing.detected_price) / existing.detected_price * 100.0, 4)

            events = []
            should_alert = False
            alert_type = None
            if target_stage != prev_stage:
                existing.stage = target_stage
                existing.confidence_label = _confidence_label_for_stage(target_stage)
                existing.expires_at_ms = max(existing.expires_at_ms, now_ms + self.config.ttl_ms_for_stage(target_stage))
                existing.signal_state = "ACTIVE" if target_stage in ACTIVE_STAGES else "TERMINAL"
                events.append(self._make_event(existing, prev_stage, target_stage, target_stage, now_ms, f"Stage transition {prev_stage}->{target_stage}"))
                should_alert = True
                alert_type = target_stage
            elif score_delta >= float(self.config.strengthening_delta):
                existing.trend_state = "STRENGTHENING"
                events.append(self._make_event(existing, prev_stage, prev_stage, STAGE_SIGNAL_STRENGTHENING, now_ms, f"Score +{score_delta:.1f}"))
                should_alert = True
                alert_type = STAGE_SIGNAL_STRENGTHENING
            elif score_delta <= float(self.config.weakening_delta):
                existing.trend_state = "WEAKENING"
                events.append(self._make_event(existing, prev_stage, prev_stage, STAGE_SIGNAL_WEAKENING, now_ms, f"Score {score_delta:.1f}"))
                should_alert = True
                alert_type = STAGE_SIGNAL_WEAKENING
            else:
                existing.trend_state = "STABLE"
            return LifecycleTransitionResult(existing, events, should_alert=should_alert, alert_event_type=alert_type)

        # No active existing signal -> register new candidate if present
        if candidate is None:
            return None
        if not is_valid_transition(None, candidate.stage):
            return None
        evt = self._make_event(candidate, "", candidate.stage, candidate.stage, now_ms, "Initial early signal detected")
        should_alert = candidate.stage in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING)
        return LifecycleTransitionResult(candidate, [evt], should_alert=should_alert, alert_event_type=candidate.stage if should_alert else None)

    def promote_to_confirmed(
        self,
        existing: Optional[PredictiveSignal],
        *,
        hybrid_result: Any,
        snapshot: Any,
        now_ms: int,
    ) -> Optional[LifecycleTransitionResult]:
        """Path B ONLY: Promote a signal to CONFIRMED strictly when Hybrid Adaptive v1.1 passes ALL hard gates,
        instrument is Futures Perpetual (NEVER Spot Reference), score >= 78, and not chased.
        """
        now_ms = int(now_ms)
        inst_kind = str(getattr(snapshot, "instrument_kind", "") or (hybrid_result.plan or {}).get("instrument_kind", ""))
        if inst_kind == "SPOT_REFERENCE":
            return None  # Hard gate: Spot Reference NEVER produces Futures CONFIRMED / PAPER_ENTRY

        plan = getattr(hybrid_result, "plan", None) or {}
        if getattr(hybrid_result, "status", "") != "VALID" or not plan:
            return None

        # Verify ALL checks passed (no GAGAL or TIDAK TERSEDIA)
        checks = getattr(hybrid_result, "checks", []) or []
        if not checks:
            return None
        for chk in checks:
            st = chk.get("Status")
            if st not in ("LULUS", "SIMULASI"):
                return None

        setup_score = float(
            plan.get("setup_quality_score")
            or (getattr(hybrid_result, "metrics", {}) or {}).get("setup_quality_score", 0.0)
        )
        if setup_score < float(self.config.confirmed_threshold):
            return None

        direction = str(getattr(hybrid_result, "direction", "") or plan.get("direction", "")).upper()
        if direction not in ("LONG", "SHORT"):
            return None

        current_px = float(getattr(snapshot, "reference_price", 0.0) or getattr(snapshot, "last", 0.0) or plan.get("entry", 0.0))
        trigger_px = float(plan.get("entry_trigger", plan.get("entry", current_px)))
        inval_px = float(plan.get("sl", current_px))
        atr_val = float((getattr(hybrid_result, "metrics", {}) or {}).get("atr", 0.0) or 0.0)
        m15_regime = str(plan.get("m15_regime") or (getattr(hybrid_result, "meta", {}) or {}).get("m15_regime", "TREND"))
        strat_id = str(plan.get("strategy") or (getattr(hybrid_result, "meta", {}) or {}).get("routed_strategy", "F"))
        provider = str(getattr(snapshot, "market_provider", "Binance Futures"))

        if existing is not None and existing.signal_state == "ACTIVE" and existing.direction == direction:
            sig = existing
            prev_stage = sig.stage
            if not is_valid_transition(prev_stage, STAGE_CONFIRMED):
                # Allow PRE_WATCH -> CONFIRMED only if it stepped through Path B validation
                if prev_stage != STAGE_PRE_WATCH:
                    return None
        else:
            prev_stage = ""
            sig = PredictiveSignal(
                signal_id=f"pw-{uuid4().hex[:12]}",
                symbol=str(getattr(snapshot, "symbol", plan.get("symbol", ""))).upper(),
                direction=direction,
                stage=STAGE_CONFIRMED,
                early_score=min(77.0, setup_score),
                detected_at_ms=now_ms,
                updated_at_ms=now_ms,
                detected_price=current_px,
                current_price=current_px,
                trigger_price=trigger_px,
                invalidation_price=inval_px,
                atr_value=atr_val,
                provider=provider,
                instrument_kind=inst_kind or "USD_M_PERPETUAL",
            )

        sig.trigger_price = trigger_px
        sig.invalidation_price = inval_px
        sig.current_price = current_px
        sig.atr_value = atr_val
        chased, chase_detail = check_anti_chase(sig, current_px, self.config)
        if chased:
            sig.stage = STAGE_MISSED_ENTRY
            sig.signal_state = "TERMINAL"
            sig.entry_validity = "CHASE_MISSED"
            sig.updated_at_ms = now_ms
            evt = self._make_event(sig, prev_stage, STAGE_MISSED_ENTRY, STAGE_MISSED_ENTRY, now_ms, f"Confirmed entry chased: {chase_detail}")
            return LifecycleTransitionResult(sig, [evt], should_alert=True, alert_event_type=STAGE_MISSED_ENTRY)

        sig.stage = STAGE_CONFIRMED
        sig.signal_state = "ACTIVE"
        sig.setup_quality_score = round(setup_score, 2)
        sig.confirmation_score = round(setup_score, 2)
        sig.m15_regime = m15_regime
        sig.strategy_id = strat_id
        sig.updated_at_ms = now_ms
        sig.expires_at_ms = int(plan.get("expires_ms") or (now_ms + self.config.ttl_ms_for_stage(STAGE_CONFIRMED)))
        sig.confidence_label = _confidence_label_for_stage(STAGE_CONFIRMED)
        sig.entry_validity = "VALID"
        sig.hard_gates_passed = True
        sig.confirmed_plan = dict(plan)
        sig.distance_to_trigger_pct = round(abs(trigger_px - current_px) / current_px * 100.0, 4) if current_px > 0 else 0.0
        sig.distance_to_invalidation_pct = round(abs(current_px - inval_px) / current_px * 100.0, 4) if current_px > 0 else 0.0
        evt = self._make_event(sig, prev_stage, STAGE_CONFIRMED, STAGE_CONFIRMED, now_ms, f"Hybrid Adaptive v1.1 confirmed (score={setup_score:.1f})")
        return LifecycleTransitionResult(sig, [evt], should_alert=True, alert_event_type=STAGE_CONFIRMED)

    def mark_paper_entry(self, sig: PredictiveSignal, now_ms: int, trade_id: str = "") -> Optional[LifecycleTransitionResult]:
        """Transition CONFIRMED -> PAPER_ENTRY strictly after paper engine opens simulated order."""
        if sig.stage != STAGE_CONFIRMED or not sig.hard_gates_passed or sig.instrument_kind == "SPOT_REFERENCE":
            return None
        if not is_valid_transition(sig.stage, STAGE_PAPER_ENTRY):
            return None
        sig.stage = STAGE_PAPER_ENTRY
        sig.signal_state = "TERMINAL"
        sig.entry_validity = "PAPER_EXECUTED"
        sig.updated_at_ms = int(now_ms)
        evt = self._make_event(sig, STAGE_CONFIRMED, STAGE_PAPER_ENTRY, STAGE_PAPER_ENTRY, int(now_ms), f"Paper order registered ({trade_id})")
        return LifecycleTransitionResult(sig, [evt], should_alert=True, alert_event_type=STAGE_PAPER_ENTRY)

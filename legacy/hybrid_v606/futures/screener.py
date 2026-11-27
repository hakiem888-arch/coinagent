"""Bounded whole-universe screening; Multi-Stage Market Scanner (M15 Regime, Reversal, Trend & Activity).

BUG-39, BUG-43, BUG-45, BUG-46, BUG-49, BUG-53 + P0-1, P0-4, P0-7 + v6.0.4 Hybrid Adaptive v1.1:
- Supports 50-candidate pipeline capacity (`STAGE1_MAX_CANDIDATES = 50`, `STAGE2_MAX_CANDIDATES = 50`)
  and up to 50 deep-analyzed candidates per cycle (default target 20 candidates per cycle, Section 3).
- Validates exchange contract metadata (`validate_exchange_symbol_spec`: active symbol, `PERPETUAL`,
  `TRADING`, `quoteAsset == USDT`, `PRICE_FILTER`, `LOT_SIZE`, `MIN_NOTIONAL`, `tickSize`, `stepSize`,
  `precision`, `contract_multiplier`) and separates `filtered_non_target_instrument` from
  `rejected_contract_spec` and `rejected_invalid_symbol` (Section 4).
- Guarantees exact mathematical reconciliation between stages:
  `universe_total == stage1_passed + stage1_rejected` (Section 3).
"""
import math
import json
import re
from pathlib import Path
import numpy as np
from .market import MarketError
from .indicators import indicators, confirmed_pivots
from .hybrid_adaptive import (
    detect_m15_regime,
    REGIME_STRONG_TREND_UP,
    REGIME_STRONG_TREND_DOWN,
    REGIME_TREND_UP,
    REGIME_TREND_DOWN,
    REGIME_BREAKOUT_UP,
    REGIME_BREAKOUT_DOWN,
    REGIME_RANGE,
    REGIME_TRANSITION,
    REGIME_HIGH_VOLATILITY,
)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "scanner.json"
try:
    _CONFIG = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    _CONFIG = {}
M15_TIMEFRAME = str(_CONFIG.get("setup_timeframe", "15m"))
M15_CANDLE_LIMIT = max(260, int(_CONFIG.get("m15_candle_limit", 260)))
M5_CANDLE_LIMIT = max(260, int(_CONFIG.get("m5_candle_limit", 260)))
MIN_HISTORY_CANDLES = int(_CONFIG.get("min_history_candles", 250))
STAGE1_MAX_CANDIDATES = int(_CONFIG.get("stage1_max_candidates", 50))
STAGE2_MAX_CANDIDATES = int(_CONFIG.get("stage2_max_candidates", 50))
TARGET_ANALYZED_PER_CYCLE = int(_CONFIG.get("target_analyzed_per_cycle", 20))
STAGE3_MAX_GROQ_CANDIDATES = int(_CONFIG.get("stage3_max_groq_candidates", 5))
SCORING_WEIGHTS = _CONFIG.get("scoring_weights", {
    "sweep": 15, "rsi_divergence": 10, "bb_reclaim": 10, "choch": 20,
    "stoch_rsi": 10, "volume": 10, "retest": 10, "regime": 5,
    "higher_timeframe": 5, "execution": 5,
})

M15_ERROR_CODES = (
    "HTTP_TIMEOUT",
    "RATE_LIMIT",
    "STALE_CANDLE",
    "CANDLE_NOT_CLOSED",
    "HISTORY_INSUFFICIENT",
    "EMPTY_RESPONSE",
    "INVALID_SYMBOL",
    "INVALID_OHLC",
    "CONNECTION_ERROR",
)

REJECTION_CATEGORIES = (
    "rejected_low_volume",
    "rejected_wide_spread",
    "rejected_low_trades",
    "rejected_stale_quote",
    "rejected_missing_candle",
    "rejected_invalid_symbol",
    "rejected_contract_spec",
    "rejected_intraday_range",
)


def empty_rejection_breakdown():
    return {k: 0 for k in REJECTION_CATEGORIES}


def validate_exchange_symbol_spec(raw_sym: dict, spot: bool = False, excluded_bases: set | None = None, has_active_ticker: bool = False) -> dict:
    """Section 4: Validate symbol and contract metadata from exchangeInfo / instruments-info as single source of truth.
    Distinguishes:
    - `NON_TARGET_INSTRUMENT`: non-USDT quote asset, delivery/non-perpetual contract, or settled/inactive symbol.
    - `INVALID_SYMBOL`: malformed symbol identifier.
    - `INVALID_CONTRACT_SPEC`: active target symbol with missing or invalid `PRICE_FILTER`, `LOT_SIZE`, `MIN_NOTIONAL`, `tickSize`, `stepSize`, or `contractSize`.
    - `VALID`: passes all symbol and contract checks.
    """
    if not isinstance(raw_sym, dict):
        return {"ok": False, "category": "INVALID_SYMBOL", "reason": "Metadata simbol bukan dictionary valid"}
    symbol = str(raw_sym.get("symbol") or "").strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol):
        quote_asset = str(raw_sym.get("quoteAsset") or "").strip().upper()
        if quote_asset and quote_asset != "USDT":
            return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": f"Quote asset non-USDT ({quote_asset})"}
        if "_" in symbol or "-" in symbol:
            return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": f"Kontrak delivery / non-perpetual ({symbol})"}
        return {"ok": False, "category": "INVALID_SYMBOL", "reason": f"Format simbol tidak valid ({symbol or 'EMPTY'})"}

    status = str(raw_sym.get("status") or "").strip().upper()
    if status != "TRADING":
        cat = "INVALID_SYMBOL" if has_active_ticker else "NON_TARGET_INSTRUMENT"
        return {"ok": False, "category": cat, "reason": f"Status simbol bukan TRADING ({status})"}

    quote_asset = str(raw_sym.get("quoteAsset") or "").strip().upper()
    if quote_asset != "USDT":
        return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": f"Quote asset bukan USDT ({quote_asset})"}

    base_asset = str(raw_sym.get("baseAsset") or symbol[:-4]).strip().upper()
    if spot:
        if not bool(raw_sym.get("isSpotTradingAllowed", False)):
            return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": "Spot trading tidak diizinkan"}
        if excluded_bases and base_asset in excluded_bases:
            return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": f"Stablecoin/excluded base ({base_asset})"}
    else:
        contract_type = str(raw_sym.get("contractType") or "").strip().upper()
        margin_asset = str(raw_sym.get("marginAsset") or "USDT").strip().upper()
        if contract_type != "PERPETUAL" or margin_asset != "USDT":
            return {"ok": False, "category": "NON_TARGET_INSTRUMENT", "reason": f"Bukan USDT Perpetual ({contract_type}/{margin_asset})"}

    filters_raw = raw_sym.get("filters")
    tick_size = step_size = min_qty = min_notional = 0.0
    contract_multiplier = float(raw_sym.get("contractSize", raw_sym.get("contractMultiplier", 1.0)) or 1.0)
    price_precision = int(raw_sym.get("pricePrecision", 8) or 8)
    qty_precision = int(raw_sym.get("quantityPrecision", 8) or 8)

    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
        return {"ok": False, "category": "INVALID_CONTRACT_SPEC", "reason": "Contract multiplier tidak valid"}
    if price_precision < 0 or qty_precision < 0:
        return {"ok": False, "category": "INVALID_CONTRACT_SPEC", "reason": "Precision kontrak negatif"}

    if isinstance(filters_raw, list) and len(filters_raw) > 0:
        try:
            f_map = {f["filterType"]: f for f in filters_raw if isinstance(f, dict) and "filterType" in f}
            pf = f_map.get("PRICE_FILTER")
            lf = f_map.get("LOT_SIZE")
            nf = f_map.get("MIN_NOTIONAL") or f_map.get("NOTIONAL")
            if not pf or not lf or not nf:
                return {"ok": False, "category": "INVALID_CONTRACT_SPEC", "reason": "Filter PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL tidak lengkap"}
            tick_size = float(pf.get("tickSize", 0))
            step_size = float(lf.get("stepSize", 0))
            min_qty = float(lf.get("minQty", 0))
            min_notional = float(nf.get("notional", nf.get("minNotional", 0)))
            if not all(math.isfinite(v) and v > 0 for v in (tick_size, step_size, min_qty, min_notional)):
                return {"ok": False, "category": "INVALID_CONTRACT_SPEC", "reason": "Nilai tickSize/stepSize/minQty/minNotional <= 0"}
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "category": "INVALID_CONTRACT_SPEC", "reason": "Gagal memvalidasi filter kontrak"}

    return {
        "ok": True,
        "category": "VALID",
        "symbol": symbol,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "contract_type": "SPOT" if spot else "PERPETUAL",
        "status": status,
        "tick_size": tick_size,
        "step_size": step_size,
        "min_qty": min_qty,
        "min_notional": min_notional,
        "contract_multiplier": contract_multiplier,
        "price_precision": price_precision,
        "quantity_precision": qty_precision,
    }


def redact_screener_error_detail(text):
    """Ensure M15 error messages never expose API keys, bearer tokens, or credential-bearing URLs."""
    s = str(text or "").strip()
    s = re.sub(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*[^\s,;\"']+", r"\1=[REDACTED]", s)
    s = re.sub(r"(?i)bearer\s+[a-z0-9_\-\.~=]+", "Bearer [REDACTED]", s)
    s = re.sub(r"gsk_[a-zA-Z0-9]{10,}", "gsk_[REDACTED]", s)
    s = re.sub(r"postgresql://[^\s]+", "postgresql://[REDACTED]", s)
    return s[:220]


def classify_m15_error(exc, symbol="", timeframe=M15_TIMEFRAME):
    """BUG-43 & P0-4: Classify Stage 2 M15 candidate screening failure into specific diagnostic code."""
    raw_msg = redact_screener_error_detail(str(exc or ""))
    msg_lower = raw_msg.lower()
    http_status = getattr(exc, "http_status", None)
    reason_code = str(getattr(exc, "reason_code", "") or "").upper()

    for explicit_code in M15_ERROR_CODES:
        if raw_msg.upper().startswith(explicit_code):
            detail = raw_msg.split(":", 1)[1].strip() if ":" in raw_msg else raw_msg
            return {
                "symbol": str(symbol or ""),
                "timeframe": str(timeframe or M15_TIMEFRAME),
                "code": explicit_code,
                "detail": detail or explicit_code,
            }

    if isinstance(exc, TimeoutError) or reason_code == "TIMEOUT" or "timeout" in msg_lower or "timed out" in msg_lower:
        code = "HTTP_TIMEOUT"
    elif http_status == 429 or reason_code == "RATE_LIMIT" or "429" in msg_lower or "rate limit" in msg_lower or "too many requests" in msg_lower:
        code = "RATE_LIMIT"
    elif "belum tertutup" in msg_lower or "not closed" in msg_lower or "unclosed" in msg_lower:
        code = "CANDLE_NOT_CLOSED"
    elif "stale" in msg_lower or "kedaluwarsa" in msg_lower:
        code = "STALE_CANDLE"
    elif "history_insufficient" in msg_lower or "minimal 250" in msg_lower:
        code = "HISTORY_INSUFFICIENT"
    elif (
        "empty" in msg_lower
        or "kosong" in msg_lower
        or "tidak memadai" in msg_lower
        or "tidak cukup" in msg_lower
        or "minimal 40" in msg_lower
    ):
        code = "EMPTY_RESPONSE"
    elif (
        http_status in (400, 404)
        or "invalid symbol" in msg_lower
        or "symbol tidak valid" in msg_lower
        or "unknown symbol" in msg_lower
    ):
        code = "INVALID_SYMBOL"
    elif (
        "ohlc" in msg_lower
        or "nan" in msg_lower
        or "infinite" in msg_lower
        or "skala screening" in msg_lower
        or "harga" in msg_lower
        or "negatif" in msg_lower
    ):
        code = "INVALID_OHLC"
    else:
        code = "CONNECTION_ERROR"

    return {
        "symbol": str(symbol or ""),
        "timeframe": str(timeframe or M15_TIMEFRAME),
        "code": code,
        "detail": raw_msg or code,
    }


def score_category(score):
    """Section 3: Kategori kandidat berdasarkan skor 0–100."""
    if not math.isfinite(score) or score < 55:
        return "REJECT"
    if score < 70:
        return "WATCH"
    if score < 80:
        return "PRIORITY WATCH"
    if score < 85:
        return "HIGH PRIORITY"
    if score < 90:
        return "ENTRY CANDIDATE"
    if score < 94:
        return "HIGH CONFIDENCE"
    if score < 97:
        return "VERY HIGH CONFIDENCE"
    return "EXCEPTIONAL SETUP"


def watchlist_stage_from_m15_score(score, m5_confirmations=0, entry_valid=False):
    """BUG-46: Classify candidate alert stage into PAPER_ENTRY, SETUP_FORMING, EARLY_WATCH, or NONE."""
    if entry_valid:
        return "PAPER_ENTRY"
    if not math.isfinite(score):
        return "NONE"
    if score >= 65 and int(m5_confirmations or 0) >= 3:
        return "SETUP_FORMING"
    if score >= 55:
        return "EARLY_WATCH"
    return "NONE"


def _pivot_indices(values, mode="low", left=2, right=2):
    out = []
    for i in range(left, len(values) - right):
        window = values[i-left:i+right+1]
        value = values[i]
        if mode == "low" and value == min(window) and sum(x == value for x in window) == 1:
            out.append(i)
        elif mode == "high" and value == max(window) and sum(x == value for x in window) == 1:
            out.append(i)
    return out


def _strategy_hint_for_regime(regime, sweep_confirmed=False):
    if regime in (REGIME_STRONG_TREND_UP, REGIME_STRONG_TREND_DOWN):
        return "A"
    if regime in (REGIME_TREND_UP, REGIME_TREND_DOWN):
        return "A"
    if regime in (REGIME_BREAKOUT_UP, REGIME_BREAKOUT_DOWN):
        return "B"
    if regime == REGIME_RANGE:
        return "E" if sweep_confirmed else "D"
    if regime == REGIME_TRANSITION:
        return "E"
    if regime == REGIME_HIGH_VOLATILITY:
        return "NONE"
    return "A"


def evaluate_m15_candidate(frame, row):
    """BUG-49 & P0-1: Score confirmed M15 reversal OR trend continuation evidence and detect M15 regime."""
    d = indicators(frame)
    if len(d) < 40:
        raise ValueError("EMPTY_RESPONSE: Minimal 40 candle M15 diperlukan untuk screening")
    last = d.iloc[-1]
    prev = d.iloc[-2]
    close, open_p, high, low = map(float, (last.close, last.open, last.high, last.low))
    rsi = float(last.rsi) if math.isfinite(last.rsi) else 50.0
    atr = float(last.atr) if math.isfinite(last.atr) and last.atr > 0 else max(1e-8, high - low)
    adx = float(last.adx) if math.isfinite(last.adx) else 20.0
    ema20 = float(last.ema20) if math.isfinite(last.ema20) else close
    ema50 = float(last.ema50) if math.isfinite(last.ema50) else close
    ema200 = float(last.ema200) if math.isfinite(last.ema200) else close
    ema50_prev5 = float(d.ema50.iloc[-6]) if len(d) >= 6 and math.isfinite(d.ema50.iloc[-6]) else ema50
    bb_upper = float(last.bb_upper) if math.isfinite(last.bb_upper) else high + atr
    bb_lower = float(last.bb_lower) if math.isfinite(last.bb_lower) else low - atr
    rel_vol = float(last.relative_volume) if math.isfinite(last.relative_volume) else 0.0
    k = float(last.stoch_rsi_k) if math.isfinite(last.stoch_rsi_k) else 50.0
    kd = float(last.stoch_rsi_d) if math.isfinite(last.stoch_rsi_d) else 50.0
    prev_k = float(d.stoch_rsi_k.iloc[-2]) if math.isfinite(d.stoch_rsi_k.iloc[-2]) else k
    prev_d = float(d.stoch_rsi_d.iloc[-2]) if math.isfinite(d.stoch_rsi_d.iloc[-2]) else kd

    lows = _pivot_indices(d.low.to_numpy(dtype=float), "low")
    highs = _pivot_indices(d.high.to_numpy(dtype=float), "high")
    recent_low = [i for i in lows if len(d) - 30 <= i <= len(d) - 3]
    recent_high = [i for i in highs if len(d) - 30 <= i <= len(d) - 3]
    long_sweep = short_sweep = bull_div = bear_div = False
    swept_low = swept_high = None
    if recent_low:
        idx = recent_low[-1]
        swept_low = float(d.low.iloc[idx])
        recent_min_low = float(d.low.iloc[idx + 1:].min())
        long_sweep = recent_min_low < swept_low and close > swept_low
    if recent_high:
        idx = recent_high[-1]
        swept_high = float(d.high.iloc[idx])
        recent_max_high = float(d.high.iloc[idx + 1:].max())
        short_sweep = recent_max_high > swept_high and close < swept_high
    if len(recent_low) >= 2:
        a_idx, b_idx = recent_low[-2:]
        bull_div = (
            float(d.low.iloc[b_idx]) < float(d.low.iloc[a_idx])
            and float(d.rsi.iloc[b_idx]) > float(d.rsi.iloc[a_idx])
            and 3 <= b_idx - a_idx <= 30
        )
    if len(recent_high) >= 2:
        a_idx, b_idx = recent_high[-2:]
        bear_div = (
            float(d.high.iloc[b_idx]) > float(d.high.iloc[a_idx])
            and float(d.rsi.iloc[b_idx]) < float(d.rsi.iloc[a_idx])
            and 3 <= b_idx - a_idx <= 30
        )

    long_trend_struct = close > ema20 > ema50 and ema50 > ema50_prev5 and close > float(prev.close)
    short_trend_struct = close < ema20 < ema50 and ema50 < ema50_prev5 and close < float(prev.close)

    long_bb = (low <= bb_lower and close > bb_lower) or (long_trend_struct and low <= ema20 + 0.25 * atr and close >= ema20)
    short_bb = (high >= bb_upper and close < bb_upper) or (short_trend_struct and high >= ema20 - 0.25 * atr and close <= ema20)

    prior_highs = [i for i in highs if len(d) - 12 <= i < len(d) - 1]
    prior_lows = [i for i in lows if len(d) - 12 <= i < len(d) - 1]
    long_choch = bool(
        (prior_highs and close > float(d.high.iloc[prior_highs[-1]]) and close > open_p)
        or (long_trend_struct and close > float(prev.high) and close > open_p)
    )
    short_choch = bool(
        (prior_lows and close < float(d.low.iloc[prior_lows[-1]]) and close < open_p)
        or (short_trend_struct and close < float(prev.low) and close < open_p)
    )

    long_stoch = bool((prev_k <= prev_d and k > kd and prev_k < 50) or (long_trend_struct and k >= kd and 45 <= rsi <= 70))
    short_stoch = bool((prev_k >= prev_d and k < kd and prev_k > 50) or (short_trend_struct and k <= kd and 30 <= rsi <= 55))

    long_retest = short_retest = False
    if prior_highs:
        level = float(d.high.iloc[prior_highs[-1]])
        for j in range(max(1, len(d) - 6), len(d) - 1):
            if float(d.close.iloc[j]) > level and float(d.close.iloc[j - 1]) <= level:
                long_retest = low <= level + .25 * atr and close > level and close > open_p
                break
    elif long_trend_struct:
        before_w = d.iloc[-6:-1]
        long_retest = bool(((before_w.low <= before_w.ema20 + 0.35 * atr) & (before_w.close >= before_w.ema50)).any() and close >= ema20)

    if prior_lows:
        level = float(d.low.iloc[prior_lows[-1]])
        for j in range(max(1, len(d) - 6), len(d) - 1):
            if float(d.close.iloc[j]) < level and float(d.close.iloc[j - 1]) >= level:
                short_retest = high >= level - .25 * atr and close < level and close < open_p
                break
    elif short_trend_struct:
        before_w = d.iloc[-6:-1]
        short_retest = bool(((before_w.high >= before_w.ema20 - 0.35 * atr) & (before_w.close <= before_w.ema50)).any() and close <= ema20)

    regime_ok = not (adx > 35 and ((close < ema200 and (long_sweep or bull_div)) or (close > ema200 and (short_sweep or bear_div))))
    spread_bps = float(row.get("spread_bps", 999.0))
    exec_ok = spread_bps <= 10.0
    vol_ok = rel_vol >= 1.0 if (long_trend_struct or short_trend_struct) else rel_vol >= 1.2

    def score(direction):
        is_long = direction == "LONG"
        sweep_or_struct = (long_sweep or long_trend_struct) if is_long else (short_sweep or short_trend_struct)
        div_or_rsi_slope = (
            (bull_div or (long_trend_struct and rsi > float(d.rsi.iloc[-4]) and 48 <= rsi <= 68))
            if is_long
            else (bear_div or (short_trend_struct and rsi < float(d.rsi.iloc[-4]) and 32 <= rsi <= 52))
        )
        vals = [
            sweep_or_struct,
            div_or_rsi_slope,
            long_bb if is_long else short_bb,
            long_choch if is_long else short_choch,
            long_stoch if is_long else short_stoch,
            vol_ok,
            long_retest if is_long else short_retest,
            regime_ok,
            close >= ema200 if is_long else close <= ema200,
            exec_ok,
        ]
        weights = [
            int(SCORING_WEIGHTS[k])
            for k in (
                "sweep", "rsi_divergence", "bb_reclaim", "choch", "stoch_rsi",
                "volume", "retest", "regime", "higher_timeframe", "execution",
            )
        ]
        return sum(w for ok, w in zip(vals, weights) if ok), vals

    long_score, long_flags = score("LONG")
    short_score, short_flags = score("SHORT")
    direction = "LONG" if long_score >= short_score else "SHORT"
    total_score, flags = (long_score, long_flags) if direction == "LONG" else (short_score, short_flags)
    category = score_category(total_score)
    wl_stage = watchlist_stage_from_m15_score(total_score)

    try:
        reg_info = detect_m15_regime(d)
        m15_regime = str(reg_info.get("regime") or "TRANSITION")
    except Exception:
        m15_regime = "TREND_UP" if long_trend_struct else ("TREND_DOWN" if short_trend_struct else "TRANSITION")

    sweep_conf = bool(long_sweep if direction == "LONG" else short_sweep)
    strategy_id = _strategy_hint_for_regime(m15_regime, sweep_confirmed=sweep_conf)
    history_sufficient = bool(len(d) >= MIN_HISTORY_CANDLES)
    m15_summary = (
        f"{direction} setup · Regime {m15_regime} (Strat {strategy_id}) · "
        f"Score {total_score}/100 ({category}) · Stage {wl_stage} · RSI {rsi:.1f} · ADX {adx:.1f} · Vol {rel_vol:.2f}x"
    )
    return {
        "candidate_direction": direction,
        "m15_score": int(total_score),
        "category": category,
        "watchlist_stage": wl_stage,
        "m15_regime": m15_regime,
        "regime": m15_regime,
        "strategy_id": strategy_id,
        "candle_count": int(len(d)),
        "history_sufficient": history_sufficient,
        "history_status": "OK" if history_sufficient else "HISTORY_INSUFFICIENT",
        "m15_summary": m15_summary,
        "sweep_confirmed": sweep_conf,
        "rsi_divergence": bool(bull_div if direction == "LONG" else bear_div),
        "bb_reclaim": bool(flags[2]),
        "choch_initial": bool(flags[3]),
        "stoch_rsi_ok": bool(flags[4]),
        "volume_ok": bool(flags[5]),
        "retest_confirmed": bool(flags[6]),
        "regime_ok": bool(flags[7]),
        "htf_context_ok": bool(flags[8]),
        "execution_quality_ok": bool(flags[9]),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "swept_level": swept_low if direction == "LONG" else swept_high,
    }


def shortlist(universe, min_volume=10_000_000, limit=10, allow_medium_liquidity_fallback=True):
    """Stage 1: Screening seluruh market dengan validasi exchangeInfo dan rekonsiliasi jumlah antar-tahap (Section 3 & 4)."""
    if not 1 <= limit <= 100:
        raise ValueError("Shortlist harus antara 1 hingga 100")
    spot = universe.get("instrument_kind") == "SPOT_REFERENCE"
    excluded = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "AEUR", "PAXG"}
    info_symbols = universe.get("info", {}).get("symbols", [])

    active_specs = {}
    filtered_non_target_count = 0
    invalid_symbol_meta_count = 0
    invalid_contract_spec_count = 0
    rejection_reasons_by_symbol = {}

    active_ticker_syms = {
        str(t.get("symbol") or "").strip().upper()
        for t in (universe.get("tickers") or [])
        if isinstance(t, dict) and float(t.get("quoteVolume") or 0) > 0
    }
    for raw_s in info_symbols:
        sym_u = str((raw_s or {}).get("symbol") or "").strip().upper()
        v_res = validate_exchange_symbol_spec(raw_s, spot=spot, excluded_bases=excluded, has_active_ticker=(sym_u in active_ticker_syms))
        sym_key = str((raw_s or {}).get("symbol") or "UNKNOWN")
        if v_res["ok"]:
            active_specs[v_res["symbol"]] = v_res
        elif v_res["category"] == "NON_TARGET_INSTRUMENT":
            filtered_non_target_count += 1
        elif v_res["category"] == "INVALID_CONTRACT_SPEC":
            invalid_contract_spec_count += 1
            rejection_reasons_by_symbol[sym_key] = f"rejected_contract_spec: {v_res['reason']}"
        else:
            invalid_symbol_meta_count += 1
            rejection_reasons_by_symbol[sym_key] = f"rejected_invalid_symbol: {v_res['reason']}"

    active = set(active_specs.keys())
    books = {x.get("symbol"): x for x in universe.get("books", [])}

    def _collect(vol_threshold, tier_label, max_lev):
        out = []
        rej = empty_rejection_breakdown()
        rej["rejected_invalid_symbol"] += invalid_symbol_meta_count
        rej["rejected_contract_spec"] += invalid_contract_spec_count
        seen_active = set()
        funnel = {
            "lolos_freshness": 0,
            "lolos_liquidity": 0,
            "lolos_spread": 0,
        }
        sym_reasons = dict(rejection_reasons_by_symbol)
        for ticker in universe.get("tickers", []):
            symbol = ticker.get("symbol")
            if symbol not in active or symbol in seen_active:
                continue
            seen_active.add(symbol)
            if symbol not in books:
                rej["rejected_missing_candle"] += 1
                sym_reasons[symbol] = "rejected_missing_candle: bookTicker tidak tersedia"
                continue
            try:
                book = books[symbol]
                last, volume = float(ticker["lastPrice"]), float(ticker["quoteVolume"])
                count = int(ticker["count"]) if ticker.get("count") is not None else None
                high, low, change = float(ticker["highPrice"]), float(ticker["lowPrice"]), float(ticker["priceChangePercent"])
                bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                book_ms = universe["book_observed_ms"] if spot else book["time"]
                ages = [universe["now_ms"] - int(book_ms), universe["now_ms"] - int(ticker["closeTime"])]
                if not all(math.isfinite(x) for x in (last, volume, high, low, change, bid, ask)):
                    rej["rejected_invalid_symbol"] += 1
                    sym_reasons[symbol] = "rejected_invalid_symbol: Nilai ticker/quote non-finite (NaN/Inf)"
                    continue
                if not (high >= last >= low > 0 and ask >= bid > 0):
                    rej["rejected_invalid_symbol"] += 1
                    sym_reasons[symbol] = "rejected_invalid_symbol: Struktur harga high/last/low atau ask/bid tidak valid"
                    continue
                if max(ages) > 60000 or min(ages) < 0:
                    rej["rejected_stale_quote"] += 1
                    sym_reasons[symbol] = f"rejected_stale_quote: Usia quote {max(ages)}ms > 60000ms"
                    continue
                funnel["lolos_freshness"] += 1
                if volume < vol_threshold:
                    rej["rejected_low_volume"] += 1
                    sym_reasons[symbol] = f"rejected_low_volume: Volume 24j ${volume:,.0f} < ${vol_threshold:,.0f}"
                    continue
                if count is not None and count < 1000:
                    rej["rejected_low_trades"] += 1
                    sym_reasons[symbol] = f"rejected_low_trades: Jumlah transaksi {count} < 1000"
                    continue
                funnel["lolos_liquidity"] += 1
                spread = (ask - bid) / ((ask + bid) / 2) * 10000
                if spread > 10:
                    rej["rejected_wide_spread"] += 1
                    sym_reasons[symbol] = f"rejected_wide_spread: Spread {spread:.2f} bps > 10 bps"
                    continue
                funnel["lolos_spread"] += 1
                movement = (high - low) / last * 100
                if not .5 <= movement <= 30:
                    rej["rejected_intraday_range"] += 1
                    sym_reasons[symbol] = f"rejected_intraday_range: Range {movement:.2f}% di luar 0.5%–30%"
                    continue
                score = math.log10(volume) + (.25 * math.log10(count) if count else 0.0) - spread / 10
                direction_hint = "UNASSESSED"
                spec_meta = active_specs.get(symbol, {})
                out.append({
                    "symbol": symbol,
                    "quote_volume": volume,
                    "trades": count,
                    "spread_bps": spread,
                    "range_pct": movement,
                    "change_pct": change,
                    "liquidity_score": score,
                    "liquidity_tier": tier_label,
                    "liquidity_max_leverage": max_lev,
                    "watchlist_side": direction_hint,
                    "contract_multiplier": spec_meta.get("contract_multiplier", 1.0),
                    "price_precision": spec_meta.get("price_precision", 8),
                    "quantity_precision": spec_meta.get("quantity_precision", 8),
                })
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                rej["rejected_invalid_symbol"] += 1
                sym_reasons[symbol] = f"rejected_invalid_symbol: {type(exc).__name__}"
                continue
        missing_tickers = max(0, len(active) - len(seen_active))
        if missing_tickers > 0:
            rej["rejected_missing_candle"] += missing_tickers
            for m_sym in (active - seen_active):
                sym_reasons[m_sym] = "rejected_missing_candle: ticker 24hr tidak ditemukan"
        return out, rej, funnel, sym_reasons

    rows, rej, funnel, sym_reasons = _collect(min_volume, "HIGH", 20)
    if allow_medium_liquidity_fallback and len(rows) < min(5, limit) and min_volume > 5_000_000:
        rows, rej, funnel, sym_reasons = _collect(5_000_000, "MEDIUM (MAX 3x)", 3)

    rows.sort(key=lambda r: (-r["liquidity_score"], r["symbol"]))
    shortlisted = rows[:limit]
    stage1_passed = len(rows)
    stage1_rejected = sum(rej.values())
    universe_count = stage1_passed + stage1_rejected
    return {
        "exchange_info_total": len(info_symbols),
        "filtered_non_target_instrument": filtered_non_target_count,
        "total_discovered": max(len(info_symbols), universe_count),
        "universe_count": universe_count,
        "eligible_count": stage1_passed,
        "stage1_passed": stage1_passed,
        "stage1_rejected": stage1_rejected,
        "shortlist_count": len(shortlisted),
        "stage2_shortlist": len(shortlisted),
        "rows": shortlisted,
        "long_watchlist": [],
        "short_watchlist": [],
        "rejected_count": stage1_rejected,
        "rejection_breakdown": rej,
        "rejection_reasons_by_symbol": sym_reasons,
        "funnel_counts": funnel,
    }


def _balance_long_short_ranking(ranked):
    """BUG-45 #4: Interleave top LONG and SHORT candidates so one side does not monopolize deep analysis."""
    longs = [r for r in ranked if r.get("candidate_direction") == "LONG"]
    shorts = [r for r in ranked if r.get("candidate_direction") == "SHORT"]
    others = [r for r in ranked if r.get("candidate_direction") not in ("LONG", "SHORT")]
    if not longs or not shorts:
        return list(ranked)
    balanced = []
    first_list, second_list = (longs, shorts) if longs[0]["screening_score"] >= shorts[0]["screening_score"] else (shorts, longs)
    i = j = 0
    while i < len(first_list) or j < len(second_list):
        if i < len(first_list):
            balanced.append(first_list[i])
            i += 1
        if j < len(second_list):
            balanced.append(second_list[j])
            j += 1
    balanced.extend(others)
    return balanced


def select_candidates(
    rows,
    market,
    now_ms,
    count=2,
    return_diagnostics=False,
    min_m15_score=0,
    balance_long_short=True,
    m15_cache=None,
    max_stage2=None,
    require_250_candles=False,
):
    """Rank up to 50 Stage 2 candidates using CLOSED M15 setup data (`limit=260` >= 250 candles) and select up to `count` (up to 50) for deep M5 analysis."""
    if not 1 <= int(count) <= 50:
        raise ValueError("Kandidat mendalam harus antara 1 hingga 50")
    cap = int(max_stage2 if max_stage2 is not None else max(STAGE2_MAX_CANDIDATES, len(rows)))
    ranked, errors, error_details = [], [], []
    for row in rows[:cap]:
        symbol = row["symbol"]
        try:
            d = market.closed_candles(symbol, M15_TIMEFRAME, now_ms, limit=M15_CANDLE_LIMIT)
            if d is None or len(d) == 0:
                raise ValueError("EMPTY_RESPONSE: Respons candle M15 kosong")
            if "close_time" in d:
                last_close_ms = int(d.close_time.iloc[-1])
                if last_close_ms >= int(now_ms):
                    raise ValueError(f"CANDLE_NOT_CLOSED: Candle {M15_TIMEFRAME} terakhir belum tertutup (close_time={last_close_ms})")
                if int(now_ms) - last_close_ms > 45 * 60 * 1000:
                    raise ValueError(f"STALE_CANDLE: Candle {M15_TIMEFRAME} kedaluwarsa ({int(now_ms - last_close_ms) // 1000}s > 2700s)")
            else:
                last_close_ms = int(now_ms)
            cols = ["open", "high", "low", "close", "volume"] if "open" in d else ["high", "low", "close", "volume"]
            a = d[cols].to_numpy(dtype=float)
            price_cols = 4 if "open" in d else 3
            if not np.isfinite(a).all() or (a[:, :price_cols] <= 0).any() or (a[:, -1] < 0).any():
                raise ValueError("INVALID_OHLC: Data OHLC/Volume mengandung NaN, Inf, atau nilai non-positif")
            if (d.high.to_numpy(dtype=float) < d.low.to_numpy(dtype=float)).any():
                raise ValueError("INVALID_OHLC: High lebih kecil dari Low pada candle M15")
            if require_250_candles and len(d) < MIN_HISTORY_CANDLES:
                raise ValueError(f"HISTORY_INSUFFICIENT: Candle M15 tertutup hanya {len(d)} (wajib minimal {MIN_HISTORY_CANDLES})")
            if len(d) < 30:
                raise ValueError(f"EMPTY_RESPONSE: Jumlah candle M15 tidak memadai ({len(d)}/30)")
            tr = np.maximum(d.high.to_numpy()[1:] - d.low.to_numpy()[1:], np.maximum(
                abs(d.high.to_numpy()[1:] - d.close.to_numpy()[:-1]),
                abs(d.low.to_numpy()[1:] - d.close.to_numpy()[:-1])))
            scale = float(tr[-14:].mean())
            previous_volume = float(d.volume.iloc[-21:-1].mean())
            if not math.isfinite(scale) or scale <= 0 or not math.isfinite(previous_volume) or previous_volume <= 0:
                raise ValueError("INVALID_OHLC: Skala ATR/volume screening M15 tidak valid (0 atau datar)")
            momentum = abs(float(d.close.iloc[-1] - d.close.iloc[-7])) / scale
            activity = float(d.volume.iloc[-1]) / previous_volume

            cache_key = (symbol, M15_TIMEFRAME, last_close_ms, len(d))
            if isinstance(m15_cache, dict) and cache_key in m15_cache:
                stage2 = dict(m15_cache[cache_key])
                stage2["m15_cached"] = True
            else:
                stage2 = evaluate_m15_candidate(d, row)
                stage2["m15_cached"] = False
                if isinstance(m15_cache, dict):
                    m15_cache[cache_key] = dict(stage2)

            rank = stage2["m15_score"] + min(momentum, 6) * 0.35 + min(activity, 3) * 0.20 + row["liquidity_score"] * 0.05
            item = dict(row, screening_score=round(rank, 6), activity=activity,
                        momentum_atr_proxy=momentum, screen_time_ms=last_close_ms,
                        screening_timeframe=M15_TIMEFRAME)
            item.update(stage2)
            ranked.append(item)
        except (MarketError, ValueError, KeyError, TypeError, IndexError, TimeoutError) as exc:
            if isinstance(exc, MarketError) and exc.access_blocked:
                raise
            diag = classify_m15_error(exc, symbol=symbol, timeframe=M15_TIMEFRAME)
            error_details.append(diag)
            errors.append(
                f"{symbol} [{M15_TIMEFRAME}] ({diag['code']}): data screening M15 belum tersedia ({diag['detail']})"
            )

    ranked.sort(key=lambda r: (-r["screening_score"], -r.get("m15_score", 0), r["symbol"]))
    eligible_for_deep = [r for r in ranked if int(r.get("m15_score", 0)) >= int(min_m15_score)]
    if balance_long_short:
        ordered_deep = _balance_long_short_ranking(eligible_for_deep)
    else:
        ordered_deep = eligible_for_deep
    selected = ordered_deep[:int(count)]
    if return_diagnostics:
        return selected, ranked, errors, error_details
    return selected, ranked, errors

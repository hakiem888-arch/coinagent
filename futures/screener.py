"""Bounded whole-universe screening; Multi-Stage Market Scanner (M15 Reversal & Activity)."""
import math
import json
from pathlib import Path
import numpy as np
from .market import MarketError
from .indicators import indicators, confirmed_pivots

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "scanner.json"
try:
    _CONFIG = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    _CONFIG = {}
M15_TIMEFRAME = str(_CONFIG.get("setup_timeframe", "15m"))
M15_CANDLE_LIMIT = int(_CONFIG.get("m15_candle_limit", 120))
STAGE2_MAX_CANDIDATES = int(_CONFIG.get("stage2_max_candidates", 20))
SCORING_WEIGHTS = _CONFIG.get("scoring_weights", {
    "sweep": 15, "rsi_divergence": 10, "bb_reclaim": 10, "choch": 20,
    "stoch_rsi": 10, "volume": 10, "retest": 10, "regime": 5,
    "higher_timeframe": 5, "execution": 5,
})


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


def evaluate_m15_candidate(frame, row):
    """Score confirmed M15 reversal evidence; potential conditions never masquerade as confirmations."""
    d = indicators(frame)
    if len(d) < 40:
        raise ValueError("Minimal 40 candle M15 diperlukan untuk screening")
    last = d.iloc[-1]
    close, open_p, high, low = map(float, (last.close, last.open, last.high, last.low))
    rsi = float(last.rsi) if math.isfinite(last.rsi) else 50.0
    atr = float(last.atr) if math.isfinite(last.atr) and last.atr > 0 else max(1e-8, high-low)
    adx = float(last.adx) if math.isfinite(last.adx) else 20.0
    ema200 = float(last.ema200) if math.isfinite(last.ema200) else close
    bb_upper = float(last.bb_upper) if math.isfinite(last.bb_upper) else high + atr
    bb_lower = float(last.bb_lower) if math.isfinite(last.bb_lower) else low - atr
    rel_vol = float(last.relative_volume) if math.isfinite(last.relative_volume) else 0.0
    k = float(last.stoch_rsi_k) if math.isfinite(last.stoch_rsi_k) else 50.0
    kd = float(last.stoch_rsi_d) if math.isfinite(last.stoch_rsi_d) else 50.0
    prev_k = float(d.stoch_rsi_k.iloc[-2]) if math.isfinite(d.stoch_rsi_k.iloc[-2]) else k
    prev_d = float(d.stoch_rsi_d.iloc[-2]) if math.isfinite(d.stoch_rsi_d.iloc[-2]) else kd

    # Confirmed pivots only: two candles to the right must already have closed.
    lows = _pivot_indices(d.low.to_numpy(dtype=float), "low")
    highs = _pivot_indices(d.high.to_numpy(dtype=float), "high")
    recent_low = [i for i in lows if len(d)-30 <= i <= len(d)-3]
    recent_high = [i for i in highs if len(d)-30 <= i <= len(d)-3]
    long_sweep = short_sweep = bull_div = bear_div = False
    swept_low = swept_high = None
    if recent_low:
        idx = recent_low[-1]
        swept_low = float(d.low.iloc[idx])
        # Sweep candle must be after pivot and close back above pivot price.
        long_sweep = low < swept_low and close > swept_low
    if recent_high:
        idx = recent_high[-1]
        swept_high = float(d.high.iloc[idx])
        short_sweep = high > swept_high and close < swept_high
    if len(recent_low) >= 2:
        a, b = recent_low[-2:]
        bull_div = (float(d.low.iloc[b]) < float(d.low.iloc[a]) and
                    float(d.rsi.iloc[b]) > float(d.rsi.iloc[a]) and 3 <= b-a <= 30)
    if len(recent_high) >= 2:
        a, b = recent_high[-2:]
        bear_div = (float(d.high.iloc[b]) > float(d.high.iloc[a]) and
                    float(d.rsi.iloc[b]) < float(d.rsi.iloc[a]) and 3 <= b-a <= 30)

    long_bb = low <= bb_lower and close > bb_lower
    short_bb = high >= bb_upper and close < bb_upper
    # Initial structure break: close crosses the latest internal swing, wick alone is insufficient.
    prior_highs = [i for i in highs if i < len(d)-1 and i >= len(d)-12]
    prior_lows = [i for i in lows if i < len(d)-1 and i >= len(d)-12]
    long_choch = bool(prior_highs and close > float(d.high.iloc[prior_highs[-1]]) and close > open_p)
    short_choch = bool(prior_lows and close < float(d.low.iloc[prior_lows[-1]]) and close < open_p)
    long_stoch = prev_k <= prev_d and k > kd and prev_k < 20
    short_stoch = prev_k >= prev_d and k < kd and prev_k > 80
    long_retest = short_retest = False
    if prior_highs:
        level = float(d.high.iloc[prior_highs[-1]])
        for j in range(max(1, len(d)-6), len(d)-1):
            if float(d.close.iloc[j]) > level and float(d.close.iloc[j-1]) <= level:
                long_retest = low <= level + .25*atr and close > level and close > open_p
                break
    if prior_lows:
        level = float(d.low.iloc[prior_lows[-1]])
        for j in range(max(1, len(d)-6), len(d)-1):
            if float(d.close.iloc[j]) < level and float(d.close.iloc[j-1]) >= level:
                short_retest = high >= level - .25*atr and close < level and close < open_p
                break
    regime_ok = not (adx > 35 and ((close < ema200 and (long_sweep or bull_div)) or (close > ema200 and (short_sweep or bear_div))))
    spread_bps = float(row.get("spread_bps", 999.0))
    exec_ok = spread_bps <= 10.0

    def score(direction):
        is_long = direction == "LONG"
        vals = [long_sweep if is_long else short_sweep,
                bull_div if is_long else bear_div,
                long_bb if is_long else short_bb,
                long_choch if is_long else short_choch,
                long_stoch if is_long else short_stoch,
                rel_vol >= 1.2,
                long_retest if is_long else short_retest,
                regime_ok,
                close >= ema200 if is_long else close <= ema200,
                exec_ok]
        weights = [int(SCORING_WEIGHTS[k]) for k in ("sweep", "rsi_divergence", "bb_reclaim", "choch", "stoch_rsi", "volume", "retest", "regime", "higher_timeframe", "execution")]
        return sum(w for ok,w in zip(vals,weights) if ok), vals
    long_score, long_flags = score("LONG")
    short_score, short_flags = score("SHORT")
    direction = "LONG" if long_score >= short_score else "SHORT"
    total_score, flags = (long_score, long_flags) if direction == "LONG" else (short_score, short_flags)
    category = score_category(total_score)
    m15_summary = f"{direction} setup · Score {total_score}/100 ({category}) · RSI {rsi:.1f} · ADX {adx:.1f} · Vol {rel_vol:.2f}x"
    return {
        "candidate_direction": direction, "m15_score": int(total_score), "category": category,
        "m15_summary": m15_summary, "sweep_confirmed": bool(flags[0]),
        "rsi_divergence": bool(flags[1]), "bb_reclaim": bool(flags[2]),
        "choch_initial": bool(flags[3]), "stoch_rsi_ok": bool(flags[4]),
        "volume_ok": bool(flags[5]), "retest_confirmed": bool(flags[6]),
        "regime_ok": bool(flags[7]), "htf_context_ok": bool(flags[8]),
        "execution_quality_ok": bool(flags[9]),
        "long_score": int(long_score), "short_score": int(short_score),
        "swept_level": swept_low if direction == "LONG" else swept_high,
    }


def shortlist(universe, min_volume=10_000_000, limit=10, allow_medium_liquidity_fallback=True):
    """Stage 1: Screening seluruh market dengan fallback likuiditas menengah 5 juta USDT bila kandidat sedikit."""
    if not 1 <= limit <= 20:
        raise ValueError("Shortlist maksimum 20")
    spot = universe.get("instrument_kind") == "SPOT_REFERENCE"
    # Same stable/fiat/gold exclusions as the original spot scanner.
    excluded = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "AEUR", "PAXG"}
    active = {x["symbol"] for x in universe["info"].get("symbols", [])
              if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT" and
              ((bool(x.get("isSpotTradingAllowed")) and x.get("baseAsset", x["symbol"][:-4]) not in excluded) if spot else
               (x.get("contractType") == "PERPETUAL" and x.get("marginAsset", "USDT") == "USDT"))}
    books = {x.get("symbol"): x for x in universe["books"]}

    def _collect(vol_threshold, tier_label, max_lev):
        out = []
        for ticker in universe["tickers"]:
            symbol = ticker.get("symbol")
            if symbol not in active or symbol not in books:
                continue
            try:
                book = books[symbol]
                last, volume, count = float(ticker["lastPrice"]), float(ticker["quoteVolume"]), int(ticker["count"])
                high, low, change = float(ticker["highPrice"]), float(ticker["lowPrice"]), float(ticker["priceChangePercent"])
                bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                book_ms = universe["book_observed_ms"] if spot else book["time"]
                ages = [universe["now_ms"] - int(book_ms), universe["now_ms"] - int(ticker["closeTime"])]
                if not all(math.isfinite(x) for x in (last, volume, high, low, change, bid, ask)):
                    continue
                if not (high >= last >= low > 0 and ask >= bid > 0 and count >= 1000 and volume >= vol_threshold):
                    continue
                spread = (ask - bid) / ((ask + bid) / 2) * 10000
                movement = (high - low) / last * 100
                if max(ages) > 60000 or min(ages) < 0 or spread > 10 or not .5 <= movement <= 30:
                    continue
                score = math.log10(volume) + .25 * math.log10(count) - spread / 10
                direction_hint = "UNASSESSED"
                out.append({"symbol": symbol, "quote_volume": volume, "trades": count,
                            "spread_bps": spread, "range_pct": movement, "change_pct": change,
                            "liquidity_score": score, "liquidity_tier": tier_label,
                            "liquidity_max_leverage": max_lev, "watchlist_side": direction_hint})
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        return out

    rows = _collect(min_volume, "HIGH", 20)
    # Section 2: Jika jumlah kandidat terlalu sedikit, volume minimum boleh diturunkan hingga 5 juta USDT
    if allow_medium_liquidity_fallback and len(rows) < min(5, limit) and min_volume > 5_000_000:
        rows = _collect(5_000_000, "MEDIUM (MAX 3x)", 3)

    rows.sort(key=lambda r: (-r["liquidity_score"], r["symbol"]))
    long_watchlist = []
    short_watchlist = []
    return {
        "universe_count": len(active),
        "eligible_count": len(rows),
        "rows": rows[:limit],
        "long_watchlist": long_watchlist,
        "short_watchlist": short_watchlist,
        "rejected_count": max(0, len(active) - len(rows)),
    }


def select_candidates(rows, market, now_ms, count=2):
    """Rank candidates using CLOSED M15 setup data; never label 1H data as M15.

    Uses 120 closed M15 candles and enriches each candidate
    with the 100-point Stage 2 score, category, and LONG/SHORT watchlist classification.
    """
    if count not in (1, 2):
        raise ValueError("Kandidat mendalam harus 1 atau 2")
    ranked, errors = [], []
    for row in rows[:STAGE2_MAX_CANDIDATES]:
        try:
            d = market.closed_candles(row["symbol"], M15_TIMEFRAME, now_ms, limit=M15_CANDLE_LIMIT)
            a = d[["high", "low", "close", "volume"]].to_numpy(dtype=float)
            if len(d) < 30 or not np.isfinite(a).all() or (a[:, :3] <= 0).any() or (a[:, 3] < 0).any():
                raise ValueError("Candle screening tidak memadai")
            tr = np.maximum(d.high.to_numpy()[1:] - d.low.to_numpy()[1:], np.maximum(
                abs(d.high.to_numpy()[1:] - d.close.to_numpy()[:-1]),
                abs(d.low.to_numpy()[1:] - d.close.to_numpy()[:-1])))
            scale = float(tr[-14:].mean())
            previous_volume = float(d.volume.iloc[-21:-1].mean())
            if scale <= 0 or previous_volume <= 0:
                raise ValueError("Skala screening tidak valid")
            momentum = abs(float(d.close.iloc[-1] - d.close.iloc[-7])) / scale
            activity = float(d.volume.iloc[-1]) / previous_volume
            # Numerical cap avoids one exceptional candle dominating the ranking.
            rank = min(momentum, 6) + min(activity, 3) + .15 * row["liquidity_score"]
            stage2 = evaluate_m15_candidate(d, row)
            # Rank real M15 setup quality first; liquidity/activity are tie-breakers.
            rank = stage2["m15_score"] + min(momentum, 6) * 0.35 + min(activity, 3) * 0.20 + row["liquidity_score"] * 0.05
            item = dict(row, screening_score=round(rank, 6), activity=activity,
                        momentum_atr_proxy=momentum, screen_time_ms=int(d.close_time.iloc[-1]),
                        screening_timeframe=M15_TIMEFRAME)
            item.update(stage2)
            ranked.append(item)
        except (MarketError, ValueError, KeyError, TypeError, IndexError) as exc:
            if isinstance(exc, MarketError) and exc.access_blocked:
                raise
            errors.append(row["symbol"] + ": data screening M15 belum tersedia")
    ranked.sort(key=lambda r: (-r["screening_score"], -r.get("m15_score", 0), r["symbol"]))
    return ranked[:count], ranked, errors

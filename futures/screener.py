"""Bounded whole-universe screening; Multi-Stage Market Scanner (M15 Reversal & Activity)."""
import math
import numpy as np
from .market import MarketError
from .indicators import indicators, confirmed_pivots


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


def evaluate_m15_candidate(frame, row):
    """Section 2 & 3: Screening teknikal awal & penilaian kandidat (skor 0–100)."""
    d = indicators(frame)
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) >= 2 else last
    recent = d.iloc[-10:-1] if len(d) >= 10 else d.iloc[:-1]

    close = float(last.close)
    open_p = float(last.open)
    high = float(last.high)
    low = float(last.low)
    rsi = float(last.rsi) if math.isfinite(last.rsi) else 50.0
    atr = float(last.atr) if math.isfinite(last.atr) and last.atr > 0 else max(1e-8, high - low)
    adx = float(last.adx) if math.isfinite(last.adx) else 20.0
    ema200 = float(last.ema200) if math.isfinite(last.ema200) else close
    bb_upper = float(last.bb_upper) if math.isfinite(last.bb_upper) else high + atr
    bb_lower = float(last.bb_lower) if math.isfinite(last.bb_lower) else low - atr
    rel_vol = float(last.relative_volume) if math.isfinite(last.relative_volume) else 1.0
    stoch_k = float(last.stoch_rsi_k) if math.isfinite(last.stoch_rsi_k) else 50.0
    stoch_d = float(last.stoch_rsi_d) if math.isfinite(last.stoch_rsi_d) else 50.0

    swing_low = float(recent.low.min()) if len(recent) else low
    swing_high = float(recent.high.max()) if len(recent) else high

    # Detect potential LONG vs SHORT reversal/momentum characteristics
    long_sweep = low <= swing_low + 0.25 * atr and close >= open_p
    short_sweep = high >= swing_high - 0.25 * atr and close <= open_p

    long_bb = low <= bb_lower + 0.5 * atr and close >= bb_lower
    short_bb = high >= bb_upper - 0.5 * atr and close <= bb_upper

    if long_sweep or (close >= open_p and rsi <= 55):
        direction = "LONG"
    elif short_sweep or (close < open_p and rsi >= 45):
        direction = "SHORT"
    else:
        direction = "LONG" if close >= float(d.close.iloc[-7]) else "SHORT"

    sign = 1 if direction == "LONG" else -1

    # 1. Liquidity sweep terkonfirmasi (15)
    sweep_ok = long_sweep if sign == 1 else short_sweep
    pts_sweep = 15 if sweep_ok else 10

    # 2. RSI divergence / kondisi ekstrem (10)
    rsi_div = (rsi > float(recent.rsi.min()) and rsi <= 65) if sign == 1 else (rsi < float(recent.rsi.max()) and rsi >= 35)
    pts_rsi = 10 if rsi_div else 7

    # 3. Bollinger Band reclaim/rejection (10)
    bb_ok = long_bb if sign == 1 else short_bb
    pts_bb = 10 if bb_ok else 7

    # 4. CHoCH atau perubahan struktur awal (20)
    choch_ok = (close > open_p and close > float(prev.high)) if sign == 1 else (close < open_p and close < float(prev.low))
    pts_choch = 20 if choch_ok else (15 if (close > open_p if sign == 1 else close < open_p) else 10)

    # 5. Stoch RSI (10)
    stoch_ok = (stoch_k >= stoch_d) if sign == 1 else (stoch_k <= stoch_d)
    pts_stoch = 10 if stoch_ok else 7

    # 6. Volume confirmation (10)
    pts_vol = 10 if rel_vol >= 1.2 else (8 if rel_vol >= 0.9 else 5)

    # 7. Retest area struktur (10)
    pts_retest = 10 if (low <= float(last.ema20) + 0.3 * atr if sign == 1 else high >= float(last.ema20) - 0.3 * atr) else 7

    # 8. Kesesuaian market regime (5)
    pts_regime = 5 if 15 <= adx <= 45 else 3

    # 9. Konteks timeframe lebih tinggi / EMA200 (5)
    pts_htf = 5 if (close >= ema200 if sign == 1 else close <= ema200) else 3

    # 10. Kualitas eksekusi dan likuiditas (5)
    spread_bps = float(row.get("spread_bps", 2.0))
    pts_exec = 5 if spread_bps <= 5.0 else 4

    total_score = int(min(100, max(0, pts_sweep + pts_rsi + pts_bb + pts_choch + pts_stoch + pts_vol + pts_retest + pts_regime + pts_htf + pts_exec)))
    category = score_category(total_score)
    m15_summary = (
        f"{direction} setup · Score {total_score}/100 ({category}) · "
        f"RSI {rsi:.1f} · ADX {adx:.1f} · Vol {rel_vol:.2f}x"
    )
    return {
        "candidate_direction": direction,
        "m15_score": total_score,
        "category": category,
        "m15_summary": m15_summary,
        "sweep_confirmed": bool(sweep_ok),
        "rsi_divergence": bool(rsi_div),
        "bb_reclaim": bool(bb_ok),
        "choch_initial": bool(choch_ok),
        "stoch_rsi_ok": bool(stoch_ok),
        "volume_ok": bool(rel_vol >= 1.2),
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
                direction_hint = "LONG" if change <= 0 else "SHORT"
                out.append({"symbol": symbol, "quote_volume": volume, "trades": count,
                            "spread_bps": spread, "range_pct": movement, "change_pct": change,
                            "liquidity_score": score, "liquidity_tier": tier_label,
                            "liquidity_max_leverage": max_lev, "watchlist_side": direction_hint})
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        return out

    rows = _collect(min_volume, "HIGH", 20)
    # Section 2: Jika jumlah kandidat terlalu sedikit, volume minimum boleh diturunkan hingga 5 juta USDT
    if allow_medium_liquidity_fallback and not rows and min_volume > 5_000_000:
        rows = _collect(5_000_000, "MEDIUM (MAX 3x)", 3)

    rows.sort(key=lambda r: (-r["liquidity_score"], r["symbol"]))
    long_watchlist = [r for r in rows if r.get("watchlist_side") == "LONG"][:20]
    short_watchlist = [r for r in rows if r.get("watchlist_side") == "SHORT"][:20]
    return {
        "universe_count": len(active),
        "eligible_count": len(rows),
        "rows": rows[:limit],
        "long_watchlist": long_watchlist,
        "short_watchlist": short_watchlist,
        "rejected_count": max(0, len(active) - len(rows)),
    }


def select_candidates(rows, market, now_ms, count=2):
    """Use CLOSED 1H raw momentum/activity + Multi-Stage M15/1H reversal scoring for ranking.

    Preserves the 100-candle 1H call for compatibility while enriching each candidate
    with the 100-point Stage 2 score, category, and LONG/SHORT watchlist classification.
    """
    if count not in (1, 2):
        raise ValueError("Kandidat mendalam harus 1 atau 2")
    ranked, errors = [], []
    for row in rows[:10]:
        try:
            d = market.closed_candles(row["symbol"], "1h", now_ms, limit=100)
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
            item = dict(row, screening_score=round(rank, 6), activity=activity,
                        momentum_atr_proxy=momentum, screen_time_ms=int(d.close_time.iloc[-1]))
            item.update(stage2)
            ranked.append(item)
        except (MarketError, ValueError, KeyError, TypeError, IndexError) as exc:
            if isinstance(exc, MarketError) and exc.access_blocked:
                raise
            errors.append(row["symbol"] + ": data screening 1H belum tersedia")
    ranked.sort(key=lambda r: (-r["screening_score"], -r.get("m15_score", 0), r["symbol"]))
    return ranked[:count], ranked, errors

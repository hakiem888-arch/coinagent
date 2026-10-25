"""Bounded whole-universe screening; heuristic ranks are not probabilities."""
import math
import numpy as np
from .market import MarketError


def shortlist(universe, min_volume=10_000_000, limit=10):
    if not 1 <= limit <= 10:
        raise ValueError("Shortlist maksimum 10")
    spot = universe.get("instrument_kind") == "SPOT_REFERENCE"
    # Same stable/fiat/gold exclusions as the original spot scanner.
    excluded = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "AEUR", "PAXG"}
    active = {x["symbol"] for x in universe["info"].get("symbols", [])
              if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT" and
              ((bool(x.get("isSpotTradingAllowed")) and x.get("baseAsset", x["symbol"][:-4]) not in excluded) if spot else
               (x.get("contractType") == "PERPETUAL" and x.get("marginAsset", "USDT") == "USDT"))}
    books = {x.get("symbol"): x for x in universe["books"]}
    rows = []
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
            if not (high >= last >= low > 0 and ask >= bid > 0 and count >= 1000 and volume >= min_volume):
                continue
            spread = (ask - bid) / ((ask + bid) / 2) * 10000
            movement = (high - low) / last * 100
            if max(ages) > 60000 or min(ages) < 0 or spread > 10 or not .5 <= movement <= 30:
                continue
            score = math.log10(volume) + .25 * math.log10(count) - spread / 10
            rows.append({"symbol": symbol, "quote_volume": volume, "trades": count,
                         "spread_bps": spread, "range_pct": movement, "change_pct": change,
                         "liquidity_score": score})
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    rows.sort(key=lambda r: (-r["liquidity_score"], r["symbol"]))
    return {"universe_count": len(active), "eligible_count": len(rows), "rows": rows[:limit]}


def select_candidates(rows, market, now_ms, count=2):
    """Use CLOSED 1H raw momentum/activity for ranking, not an entry decision.

    The later deep analysis uses >=500 candles and the complete masterprompt.
    This cheap 100-candle screen is direction-neutral and can pick either side.
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
            ranked.append(dict(row, screening_score=round(rank, 6), activity=activity,
                               momentum_atr_proxy=momentum, screen_time_ms=int(d.close_time.iloc[-1])))
        except (MarketError, ValueError, KeyError, TypeError, IndexError) as exc:
            if isinstance(exc, MarketError) and exc.access_blocked:
                raise
            errors.append(row["symbol"] + ": data screening 1H belum tersedia")
    ranked.sort(key=lambda r: (-r["screening_score"], r["symbol"]))
    return ranked[:count], ranked, errors

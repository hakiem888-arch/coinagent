"""The original scanner's public SPOT source, for reference-price PAPER only.

No futures endpoints, authenticated endpoints, order submission or automatic
host rotation. Spot REST quotes have no exchange event timestamps: freshness
uses request-start estimates anchored to server time, not invented event times.
"""
import math
import os
import re
import time
from pathlib import Path
import pandas as pd
from .market import FuturesMarket, MarketError
from .models import Contract, Snapshot, TIMEFRAMES

SPOT_BASE = "https://data-api.binance.vision"


def paper_db_path(root):
    """Keep the old futures ledger intact, including custom persistent paths."""
    if os.getenv("SPOT_HISTORY_DB"):
        return Path(os.environ["SPOT_HISTORY_DB"])
    if os.getenv("FUTURES_HISTORY_DB"):
        old = Path(os.environ["FUTURES_HISTORY_DB"])
        return old.with_name(old.stem + "_spot_reference" + old.suffix)
    return Path(root) / "data" / "spot_paper_history.sqlite3"


class SpotReferenceMarket(FuturesMarket):
    base = SPOT_BASE
    api_prefix = "/api/v3/"
    time_path = "/api/v3/time"
    market_label = "Binance Spot"
    instrument_kind = "SPOT_REFERENCE"
    allowed_paths = frozenset("/api/v3/" + p for p in (
        "time", "exchangeInfo", "ticker/24hr", "ticker/bookTicker", "ticker/price", "klines", "depth"))

    def _anchor(self):
        return int(self.get(self.time_path)["serverTime"]), time.monotonic()

    def _observed_get(self, path, anchor, params=None):
        server_ms, clock = anchor
        # Timestamp BEFORE I/O: a slow response cannot appear newly acquired.
        requested_ms = server_ms + int((time.monotonic() - clock) * 1000)
        return self.get(path, params), requested_ms

    @staticmethod
    def _check_elapsed(anchor, now):
        if now < anchor[0] or time.monotonic() - anchor[1] > 120:
            raise MarketError("Pengambilan snapshot spot terlalu lama atau waktu bursa tidak konsisten.")

    def exchange_info(self):
        return self.cached("spotExchangeInfo", time.monotonic() + 300,
                           lambda: self.get("/api/v3/exchangeInfo"))

    def candles(self, symbol, interval, limit=1000, mark=False):
        if mark:
            raise MarketError("Spot tidak menyediakan candle Mark Price futures.")
        rows = self.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list) or not rows:
            raise MarketError("Data candle spot kosong")
        try:
            frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                               "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
        except ValueError:
            raise MarketError("Struktur candle spot tidak valid") from None
        return frame.apply(pd.to_numeric, errors="coerce")

    def universe(self):
        info = self.exchange_info()
        anchor = self._anchor()
        tickers = self.get("/api/v3/ticker/24hr")
        books, observed = self._observed_get("/api/v3/ticker/bookTicker", anchor)
        now = int(self.get(self.time_path)["serverTime"])
        self._check_elapsed(anchor, now)
        if not isinstance(tickers, list) or not isinstance(books, list):
            raise MarketError("Respons seluruh market spot tidak valid")
        return {"info": info, "tickers": tickers, "books": books, "now_ms": now,
                "instrument_kind": self.instrument_kind, "book_observed_ms": observed,
                "source": self.base, "quote_time_basis": "REQUEST_START_ESTIMATE"}

    @staticmethod
    def _spec(raw):
        try:
            if raw["status"] != "TRADING" or raw["quoteAsset"] != "USDT" or not raw.get("isSpotTradingAllowed"):
                raise ValueError("Inactive spot pair")
            filters = {f["filterType"]: f for f in raw["filters"]}
            p, q = filters["PRICE_FILTER"], filters["LOT_SIZE"]
            mq = filters.get("MARKET_LOT_SIZE", {})
            # Zero disables some Spot MARKET_LOT_SIZE rules. Keep the LOT_SIZE
            # grid as a conservative simulator grid instead of dividing by zero.
            def market_value(key):
                value = float(mq.get(key, 0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid market lot")
                return value or float(q[key])
            notionals = [float(filters[k]["minNotional"]) for k in ("MIN_NOTIONAL", "NOTIONAL") if k in filters]
            if not notionals:
                raise ValueError("Missing notional")
            return Contract(tick=float(p["tickSize"]), step=float(q["stepSize"]),
                min_qty=float(q["minQty"]), max_qty=float(q["maxQty"]), min_notional=max(notionals),
                market_step=market_value("stepSize"), market_min_qty=market_value("minQty"),
                market_max_qty=market_value("maxQty"), min_price=float(p["minPrice"]),
                max_price=float(p["maxPrice"]) or float("inf"), status=raw["status"],
                contract_type="SPOT", quote_asset=raw["quoteAsset"])
        except (KeyError, TypeError, ValueError):
            raise MarketError("Pair spot tidak aktif atau spesifikasi tick/lot/notional tidak lengkap.") from None

    def load_modes(self, symbol, modes=tuple(TIMEFRAMES)):
        symbol = symbol.strip().upper()
        modes = tuple(dict.fromkeys(modes))
        if not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol) or not modes or any(m not in TIMEFRAMES for m in modes):
            raise MarketError("Gunakan simbol spot USDT dan mode yang tersedia.")
        anchor = self._anchor()
        initial = anchor[0]
        raw = next((s for s in self.exchange_info().get("symbols", []) if s.get("symbol") == symbol), None)
        if raw is None:
            raise MarketError("Simbol tidak ditemukan pada Binance Spot.")
        spec = self._spec(raw)
        intervals = tuple(dict.fromkeys(tf for m in modes for tf in TIMEFRAMES[m]))
        frames = {tf: self.closed_candles(symbol, tf, initial) for tf in intervals}
        daily = self.closed_candles(symbol, "1d", initial, 4)
        running = {}
        for tf in dict.fromkeys(TIMEFRAMES[m][1] for m in modes):
            current = self.candles(symbol, tf, 2)
            running[tf] = current[current.close_time >= initial]
        depth, depth_ms = self._observed_get("/api/v3/depth", anchor, {"symbol": symbol, "limit": 100})
        book, book_ms = self._observed_get("/api/v3/ticker/bookTicker", anchor, {"symbol": symbol})
        last, last_ms = self._observed_get("/api/v3/ticker/price", anchor, {"symbol": symbol})
        now = int(self.get(self.time_path)["serverTime"])
        self._check_elapsed(anchor, now)
        previous = daily[daily.open_time == (initial // 86400000) * 86400000 - 86400000]
        warnings = ["SPOT REFERENCE: harga, candle, volume dan depth berasal dari pasar spot.",
                    "Mark Price, funding dan kontrak perpetual tidak tersedia; SL/PnL simulasi memakai Last spot.",
                    "LONG/SHORT dan leverage hanya model PAPER. Cadangan funding adalah asumsi biaya tambahan.",
                    "Usia quote memakai estimasi awal permintaan berbasis jam bursa, bukan timestamp transaksi.",
                    "Tick/lot/minNotional spot dipakai untuk simulasi; filter dinamis order bursa tidak diverifikasi."]
        return {m: Snapshot(symbol=symbol, mode=m, now_ms=now, bias=frames[TIMEFRAMES[m][0]],
            execution=frames[TIMEFRAMES[m][1]], contract=spec, last=float(last["price"]), mark=None,
            bid=float(book["bidPrice"]), ask=float(book["askPrice"]),
            quote_times={"last": last_ms, "book": book_ms, "depth": depth_ms},
            bids=depth.get("bids", []), asks=depth.get("asks", []),
            previous_day_high=float(previous.iloc[-1].high) if len(previous) else None,
            previous_day_low=float(previous.iloc[-1].low) if len(previous) else None,
            open_bar_high=float(running[TIMEFRAMES[m][1]].high.max()) if len(running[TIMEFRAMES[m][1]]) else None,
            open_bar_low=float(running[TIMEFRAMES[m][1]].low.min()) if len(running[TIMEFRAMES[m][1]]) else None,
            source="Binance Spot public REST · " + self.base, warnings=list(warnings),
            instrument_kind=self.instrument_kind, quote_time_basis="REQUEST_START_ESTIMATE") for m in modes}

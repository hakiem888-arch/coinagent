"""Only public USD-M futures endpoints; no spot fallback and no order API."""
import time
import re
import threading
from collections import OrderedDict
import requests
import pandas as pd
from .models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS

BASE = "https://fapi.binance.com"


class MarketError(RuntimeError):
    """Safe diagnostic fields, without provider bodies or credential-bearing URLs."""

    def __init__(self, message, *, http_status=None, endpoint=None):
        super().__init__(message)
        self.http_status = http_status
        self.endpoint = endpoint

    @property
    def access_blocked(self):
        return self.http_status == 451

    def diagnostic(self):
        return {"http_status": self.http_status, "endpoint": self.endpoint,
                "access_blocked": self.access_blocked}


class FuturesMarket:
    base = BASE
    api_prefix = "/fapi/"
    time_path = "/fapi/v1/time"
    market_label = "Binance Futures"
    instrument_kind = "USD_M_PERPETUAL"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.cooldown_until = 0.0
        self.lock = threading.RLock()
        self.cache = OrderedDict()

    def cached(self, key, valid_until, loader):
        """Small process-wide cache for static metadata and CLOSED candles only."""
        with self.lock:
            if key in self.cache:
                expires, value = self.cache[key]
                if time.monotonic() < expires:
                    self.cache.move_to_end(key)
                    return value
            value = loader()
            self.cache[key] = (valid_until, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return value

    def closed_candles(self, symbol, interval, server_ms, limit=1000):
        # The server candle boundary is part of the key, so no TTL can carry an
        # old final candle over a new boundary. Live quotes are NEVER cached.
        step = INTERVAL_MS[interval]
        boundary = server_ms // step
        def download():
            raw = self.candles(symbol, interval, limit)
            closed = raw[raw.close_time < server_ms].reset_index(drop=True)
            if closed.empty or int(closed.close_time.iloc[-1]) != boundary * step - 1:
                raise MarketError("Candle terbaru selesai belum tersedia. Coba pada scan berikutnya.")
            return closed
        frame = self.cached((symbol, interval, boundary, limit), time.monotonic() + 300, download)
        return frame.copy()

    def get(self, path, params=None):
        if not path.startswith(self.api_prefix):
            raise MarketError("Endpoint tidak sesuai sumber data market")
        if hasattr(self, "allowed_paths") and path not in self.allowed_paths:
            raise MarketError("Hanya endpoint data pasar publik diizinkan")
        with self.lock:
            if time.time() < self.cooldown_until:
                raise MarketError("Bursa meminta jeda. Coba lagi setelah cooldown.")
            try:
                response = self.session.get(self.base + path, params=params, timeout=(5, 12), allow_redirects=False)
            except requests.RequestException:
                raise MarketError(f"Koneksi data {self.market_label} gagal. Periksa jaringan server hosting.", endpoint=path) from None
            if response.status_code == 451:
                raise MarketError(
                    f"Akses {self.market_label} dari server ini ditolak (HTTP 451). "
                    "Periksa pembatasan wilayah/IP hosting dengan Binance atau penyedia hosting.",
                    http_status=451, endpoint=path)
            if response.status_code in (418, 429):
                try:
                    delay = max(1, float(response.headers.get("Retry-After", "60")))
                except ValueError:
                    delay = 60
                self.cooldown_until = time.time() + delay
                raise MarketError(f"Batas permintaan bursa. Tunggu {delay:.0f} detik.",
                                  http_status=response.status_code, endpoint=path)
            if response.status_code != 200:
                raise MarketError(f"Data {self.market_label} tidak tersedia (HTTP {response.status_code}).",
                                  http_status=response.status_code, endpoint=path)
            try:
                return response.json()
            except ValueError:
                raise MarketError("Respons bursa bukan JSON valid.", http_status=200, endpoint=path) from None

    def candles(self, symbol, interval, limit=1000, mark=False):
        path = "/fapi/v1/markPriceKlines" if mark else "/fapi/v1/klines"
        rows = self.get(path, {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list) or not rows:
            raise MarketError("Data candle kosong")
        d = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                       "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
        for c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def exchange_info(self):
        return self.cached("exchangeInfo", time.monotonic() + 300, lambda: self.get("/fapi/v1/exchangeInfo"))

    def universe(self):
        info = self.exchange_info()
        tickers = self.get("/fapi/v1/ticker/24hr")
        books = self.get("/fapi/v1/ticker/bookTicker")
        now = int(self.get("/fapi/v1/time")["serverTime"])
        if not isinstance(tickers, list) or not isinstance(books, list):
            raise MarketError("Respons seluruh market tidak valid")
        return {"info": info, "tickers": tickers, "books": books, "now_ms": now}

    def load(self, symbol, mode):
        return self.load_modes(symbol, (mode,))[mode]

    def load_modes(self, symbol, modes=tuple(TIMEFRAMES)):
        """Share one fresh quote snapshot and closed candle downloads across modes."""
        symbol = symbol.strip().upper()
        modes = tuple(dict.fromkeys(modes))
        if not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol) or not modes or any(m not in TIMEFRAMES for m in modes):
            raise MarketError("Gunakan simbol USDT perpetual dan mode yang tersedia.")
        start_clock = time.monotonic()
        initial_server = int(self.get("/fapi/v1/time")["serverTime"])
        info = self.exchange_info()
        spec_raw = next((x for x in info.get("symbols", []) if x.get("symbol") == symbol), None)
        if spec_raw is None:
            raise MarketError("Simbol tidak ditemukan pada Binance USD-M Futures.")
        try:
            f = {x["filterType"]: x for x in spec_raw["filters"]}
            p, q, mq = f["PRICE_FILTER"], f["LOT_SIZE"], f["MARKET_LOT_SIZE"]
            percent = f.get("PERCENT_PRICE", {})
            spec = Contract(tick=float(p["tickSize"]), step=float(q["stepSize"]), min_qty=float(q["minQty"]),
                            max_qty=float(q["maxQty"]), min_notional=float(f["MIN_NOTIONAL"]["notional"]),
                            market_step=float(mq["stepSize"]), market_min_qty=float(mq["minQty"]), market_max_qty=float(mq["maxQty"]),
                            min_price=float(p["minPrice"]), max_price=float(p["maxPrice"]),
                            multiplier_down=float(percent.get("multiplierDown", 0)), multiplier_up=float(percent.get("multiplierUp", "inf")),
                            status=spec_raw["status"], contract_type=spec_raw["contractType"], quote_asset=spec_raw["quoteAsset"])
        except (KeyError, ValueError, TypeError):
            raise MarketError("Spesifikasi tick/lot/notional kontrak tidak lengkap.") from None
        intervals = tuple(dict.fromkeys(tf for mode in modes for tf in TIMEFRAMES[mode]))
        frames = {tf: self.closed_candles(symbol, tf, initial_server) for tf in intervals}
        daily = self.closed_candles(symbol, "1d", initial_server, 4)
        funding_interval, warnings = None, []
        try:
            rates = self.cached("fundingInfo", time.monotonic() + 60, lambda: self.get("/fapi/v1/fundingInfo"))
            for item in rates:
                if item.get("symbol") == symbol:
                    funding_interval = float(item["fundingIntervalHours"])
        except (MarketError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MarketError) and exc.access_blocked:
                raise
            warnings.append("Interval funding tidak tersedia dari fundingInfo; tidak diasumsikan 8 jam.")
        if funding_interval is None:
            warnings.append("Interval funding tidak dipastikan; biaya funding PAPER memakai cadangan simulasi.")
        running = {}
        for tf in dict.fromkeys(TIMEFRAMES[m][1] for m in modes):
            current = self.candles(symbol, tf, 2)
            current_marks = self.candles(symbol, tf, 2, mark=True)
            running[tf] = (current[current.close_time >= initial_server], current_marks[current_marks.close_time >= initial_server])
        # Live data is downloaded AFTER heavier candle work, never put in cache.
        depth = self.get("/fapi/v1/depth", {"symbol": symbol, "limit": 100})
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        mark = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        last = self.get("/fapi/v2/ticker/price", {"symbol": symbol})
        now = int(self.get("/fapi/v1/time")["serverTime"])
        if now < initial_server or time.monotonic() - start_clock > 120:
            raise MarketError("Pengambilan snapshot terlalu lama atau waktu bursa tidak konsisten.")
        day_start = (initial_server // 86400000) * 86400000
        previous = daily[daily.open_time == day_start - 86400000]
        snapshots = {}
        for mode in modes:
            bias_tf, exec_tf = TIMEFRAMES[mode]
            open_rows, mark_rows = running[exec_tf]
            snapshots[mode] = Snapshot(symbol=symbol, mode=mode, now_ms=now, bias=frames[bias_tf], execution=frames[exec_tf], contract=spec,
                last=float(last["price"]), mark=float(mark["markPrice"]), bid=float(book["bidPrice"]), ask=float(book["askPrice"]),
                quote_times={"last": int(last.get("time", 0)), "mark": int(mark.get("time", 0)),
                             "book": int(book.get("time", 0)), "depth": int(depth.get("T", depth.get("E", 0)))},
                bids=depth.get("bids", []), asks=depth.get("asks", []),
                previous_day_high=float(previous.iloc[-1].high) if len(previous) else None,
                previous_day_low=float(previous.iloc[-1].low) if len(previous) else None,
                funding_rate=float(mark["lastFundingRate"]), next_funding_ms=int(mark["nextFundingTime"]),
                funding_interval_hours=funding_interval,
                open_bar_high=float(open_rows.high.max()) if len(open_rows) else None,
                open_bar_low=float(open_rows.low.min()) if len(open_rows) else None,
                open_mark_high=float(mark_rows.high.max()) if len(mark_rows) else None,
                open_mark_low=float(mark_rows.low.min()) if len(mark_rows) else None, warnings=list(warnings))
        return snapshots

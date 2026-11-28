"""Binance public USD-M Futures market-data adapter (read-only; never places orders)."""
from collections import OrderedDict
import threading
import time
import requests
from requests.adapters import HTTPAdapter
import pandas as pd

from .models import INTERVAL_MS, validate_ohlcv_dataframe

BASE = "https://fapi.binance.com"


def build_pooled_session(pool_size: int = 16) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class MarketError(RuntimeError):
    """Safe diagnostic error fields without credential-bearing URLs."""

    def __init__(self, message, *, http_status=None, endpoint=None, ret_code=None, error_category=None, response_reason=None):
        super().__init__(message)
        self.http_status = http_status
        self.endpoint = endpoint
        self.ret_code = ret_code
        self.error_category = error_category
        self.response_reason = response_reason

    @property
    def access_blocked(self):
        return self.http_status == 451 or self.error_category == "REGIONAL_RESTRICTION"

    def diagnostic(self):
        d = {
            "http_status": self.http_status,
            "endpoint": self.endpoint,
            "access_blocked": self.access_blocked,
        }
        if self.ret_code is not None:
            d["ret_code"] = self.ret_code
        if self.error_category is not None:
            d["error_category"] = self.error_category
        return d


class FuturesMarket:
    base = BASE
    api_prefix = "/fapi/"
    time_path = "/fapi/v1/time"
    market_label = "Binance Futures"
    instrument_kind = "USD_M_PERPETUAL"
    source_label = "Binance USD-M Futures public REST"

    def __init__(self, session=None):
        self.session = session or build_pooled_session()
        self.cooldown_until = 0.0
        self.lock = threading.RLock()
        self.cache = OrderedDict()

    def cached(self, key, valid_until, loader):
        """Process-wide bounded cache for static metadata and CLOSED candles only."""
        with self.lock:
            if key in self.cache:
                expires, value = self.cache[key]
                if time.monotonic() < expires:
                    self.cache.move_to_end(key)
                    return value
        value = loader()
        with self.lock:
            self.cache[key] = (valid_until, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 512:
                self.cache.popitem(last=False)
        return value

    def get(self, path, params=None):
        if not path.startswith(self.api_prefix):
            raise MarketError("Endpoint tidak sesuai sumber data market")
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
                http_status=451, endpoint=path, error_category="REGIONAL_RESTRICTION",
            )
        if response.status_code in (418, 429):
            try:
                delay = max(1, float(response.headers.get("Retry-After", "60")))
            except ValueError:
                delay = 60
            with self.lock:
                self.cooldown_until = time.time() + delay
            raise MarketError(
                f"Batas permintaan bursa. Tunggu {delay:.0f} detik.",
                http_status=response.status_code, endpoint=path, error_category="RATE_LIMIT",
            )
        if response.status_code != 200:
            raise MarketError(
                f"Data {self.market_label} tidak tersedia (HTTP {response.status_code}).",
                http_status=response.status_code, endpoint=path,
            )
        try:
            return response.json()
        except ValueError:
            raise MarketError("Respons bursa bukan JSON valid.", http_status=200, endpoint=path, error_category="INVALID_RESPONSE") from None

    def candles(self, symbol, interval, limit=100):
        rows = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list) or not rows:
            raise MarketError("Data candle kosong")
        d = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ])
        for c in ("open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def closed_candles(self, symbol, interval, server_ms, limit=100):
        step = INTERVAL_MS[interval]
        boundary = int(server_ms) // step

        def download():
            raw = validate_ohlcv_dataframe(self.candles(symbol, interval, limit))
            closed = raw[raw.close_time < server_ms].reset_index(drop=True)
            if closed.empty or int(closed.close_time.iloc[-1]) != boundary * step - 1:
                raise MarketError("Candle terbaru selesai belum tersedia. Coba pada scan berikutnya.")
            return closed

        ttl = 900 if interval == "15m" else 300
        frame = self.cached((symbol, interval, boundary, limit), time.monotonic() + ttl, download)
        return frame.copy()

    def exchange_info(self):
        return self.cached("exchangeInfo", time.monotonic() + 300, lambda: self.get("/fapi/v1/exchangeInfo"))

    def universe(self):
        info = self.exchange_info()
        tickers = self.get("/fapi/v1/ticker/24hr")
        now = int(self.get("/fapi/v1/time")["serverTime"])
        if not isinstance(tickers, list):
            raise MarketError("Respons seluruh market tidak valid")
        return {"info": info, "tickers": tickers, "now_ms": now, "instrument_kind": self.instrument_kind}

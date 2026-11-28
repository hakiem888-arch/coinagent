"""Bybit public USDT-linear perpetual market-data adapter (read-only; never places orders)."""
from collections import OrderedDict
import os
import threading
import time
import requests
import pandas as pd

from .models import INTERVAL_MS, validate_ohlcv_dataframe
from .market import MarketError, build_pooled_session

# Bybit V5 Integration Guidance: Indonesia uses api.bybit.id; documented global
# mainnet endpoints are api.bybit.com and api.bytick.com.
BASE = os.environ.get("COINAGENT_BYBIT_BASE", "https://api.bybit.id").rstrip("/")
OFFICIAL_MAINNET_BASES = (
    "https://api.bybit.id",
    "https://api.bytick.com",
    "https://api.bybit.com",
)
FALLBACK_BASES = tuple(dict.fromkeys((BASE, *OFFICIAL_MAINNET_BASES)))
_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _resolve_proxy_map(proxy_url=None):
    proxy = (
        proxy_url
        or os.environ.get("COINAGENT_BYBIT_PROXY")
        or os.environ.get("COINAGENT_MARKET_PROXY")
        or ""
    ).strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


class BybitFuturesMarket:
    base = BASE
    time_path = "/v5/market/time"
    market_label = "Bybit USDT Perpetual"
    instrument_kind = "BYBIT_LINEAR_PERPETUAL"
    source_label = "Bybit Linear USDT Perpetual public REST"

    def __init__(self, session=None, allow_host_fallback=False, proxy_url=None):
        self.session = session or build_pooled_session()
        self.allow_host_fallback = bool(allow_host_fallback)
        self.proxy_url = proxy_url
        env_base = os.environ.get("COINAGENT_BYBIT_BASE", "").strip().rstrip("/")
        if env_base:
            self.base = env_base
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.cooldown_until = 0.0

    def _candidate_hosts(self):
        hosts = [self.base]
        if self.allow_host_fallback:
            for h in (self.base, *OFFICIAL_MAINNET_BASES):
                if h and h not in hosts:
                    hosts.append(h)
        return hosts

    def _request_one_base(self, base_url, path, params=None):
        kwargs = {"params": params, "timeout": (5, 12), "allow_redirects": False}
        if isinstance(self.session, requests.Session):
            kwargs["headers"] = _DEFAULT_HEADERS
            proxies = _resolve_proxy_map(self.proxy_url)
            if proxies:
                kwargs["proxies"] = proxies
        return self.session.get(base_url + path, **kwargs)

    def _get(self, path, params=None):
        if path not in {
            "/v5/market/time",
            "/v5/market/instruments-info",
            "/v5/market/tickers",
            "/v5/market/kline",
        }:
            raise MarketError("Endpoint Bybit tidak diizinkan")
        with self.lock:
            if time.time() < self.cooldown_until:
                raise MarketError("Bybit meminta jeda; tunggu sebelum scan berikutnya")

        hosts = self._candidate_hosts()
        last_exc = None
        attempts = []
        for idx, host in enumerate(hosts):
            try:
                r = self._request_one_base(host, path, params=params)
            except requests.RequestException:
                last_exc = MarketError("Koneksi data Bybit gagal. Periksa jaringan server hosting.", endpoint=path)
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                raise last_exc from None

            if r.status_code in (418, 429):
                with self.lock:
                    self.cooldown_until = time.time() + 60
                raise MarketError(
                    "Batas permintaan Bybit tercapai; jeda 60 detik.",
                    http_status=r.status_code, endpoint=path, error_category="RATE_LIMIT",
                )

            if r.status_code in (403, 451):
                body = (getattr(r, "text", "") or "").lower()
                rc = None
                try:
                    j_err = r.json()
                    if isinstance(j_err, dict) and j_err.get("retCode") is not None:
                        rc = int(j_err["retCode"])
                except Exception:
                    rc = None
                if "too frequent" in body or "access too frequent" in body or "rate limit" in body or rc == 10006:
                    with self.lock:
                        self.cooldown_until = time.time() + 600
                    raise MarketError(
                        f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. IP terkena batas frekuensi; tunggu 10 menit.",
                        http_status=r.status_code, endpoint=path, ret_code=rc, error_category="RATE_LIMIT",
                        response_reason=body[:120],
                    )
                if any(w in body for w in ("unauthorized", "api key", "signature", "permission", "invalid key")) or rc in (10003, 10004, 10005, 10007):
                    raise MarketError(
                        f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. Kesalahan otorisasi endpoint Bybit.",
                        http_status=r.status_code, endpoint=path, ret_code=rc, error_category="AUTH_ERROR",
                        response_reason=body[:120],
                    )
                if (
                    r.status_code == 451
                    or any(w in body for w in (
                        "geo", "country", "region", "restricted location", "jurisdiction", "geoblock",
                        "configured to block access from your country", "cloudfront",
                    ))
                    or rc == 10024
                ):
                    cat_403 = "REGIONAL_RESTRICTION"
                    detail = "Respons menyebut pembatasan wilayah. Periksa kelayakan akses Bybit dari lokasi server hosting."
                else:
                    cat_403 = "UNKNOWN_403"
                    detail = (
                        "HTTP 403 belum membuktikan geo-block atau pembatasan regional. Kemungkinan pembatasan IP hosting, "
                        "WAF/proxy, atau rate limit."
                    )
                attempts.append(f"{host.removeprefix('https://')}: HTTP {r.status_code} ({cat_403})")
                last_exc = MarketError(
                    f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path} via {host}. {detail}",
                    http_status=r.status_code, endpoint=path, ret_code=rc, error_category=cat_403,
                    response_reason=body[:120],
                )
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                if self.allow_host_fallback:
                    break
                raise last_exc

            if r.status_code != 200:
                cat_other = "AUTH_ERROR" if r.status_code == 401 else "NETWORK_ERROR"
                attempts.append(f"{host.removeprefix('https://')}: HTTP {r.status_code} ({cat_other})")
                last_exc = MarketError(
                    f"Data Bybit tidak tersedia (HTTP {r.status_code}) pada {path} via {host}.",
                    http_status=r.status_code, endpoint=path, error_category=cat_other,
                )
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                if self.allow_host_fallback:
                    break
                raise last_exc

            try:
                data = r.json()
            except ValueError:
                raise MarketError("Respons Bybit bukan JSON valid", endpoint=path, error_category="INVALID_RESPONSE") from None
            if data.get("retCode", 0) != 0:
                rc = int(data.get("retCode", 0))
                ret_msg = str(data.get("retMsg") or "").lower()
                if rc == 10024 or any(w in ret_msg for w in ("country", "region", "restricted", "geoblock")):
                    rc_cat = "REGIONAL_RESTRICTION"
                elif rc in (10006, 10018) or "rate limit" in ret_msg or "too many" in ret_msg:
                    rc_cat = "RATE_LIMIT"
                elif rc in (10003, 10004, 10005, 10007, 10010, 33004) or "api key" in ret_msg or "sign" in ret_msg:
                    rc_cat = "AUTH_ERROR"
                else:
                    rc_cat = "INVALID_RESPONSE"
                raise MarketError(
                    "Bybit menolak permintaan data publik: " + str(rc),
                    http_status=r.status_code, endpoint=path, ret_code=rc, error_category=rc_cat,
                    response_reason=ret_msg[:120],
                )
            with self.lock:
                if host != self.base:
                    self.base = host
            result = data.get("result", data)
            if path == "/v5/market/time" and isinstance(result, dict):
                result = dict(result, timeNano=data.get("timeNano"), timeSecond=data.get("timeSecond"))
            return result

        if last_exc is not None:
            if self.allow_host_fallback and attempts:
                summary = "; ".join(attempts)
                raise MarketError(
                    f"Bybit gagal pada semua endpoint resmi yang diuji untuk {path}. Percobaan: {summary}. "
                    "HTTP 403 generik tidak otomatis berarti geo-block; periksa IP egress hosting, WAF, "
                    "konfigurasi COINAGENT_BYBIT_PROXY, dan apakah host tersebut diizinkan oleh Bybit.",
                    http_status=getattr(last_exc, "http_status", None),
                    endpoint=path,
                    ret_code=getattr(last_exc, "ret_code", None),
                    error_category=getattr(last_exc, "error_category", None),
                    response_reason=getattr(last_exc, "response_reason", None),
                ) from last_exc
            raise last_exc
        raise MarketError("Data Bybit tidak tersedia.", endpoint=path)

    def get(self, path, params=None):
        if path != self.time_path:
            raise MarketError("Bybit adapter hanya menyediakan endpoint waktu melalui get")
        result = self._get(path)
        return {"serverTime": int(result.get("timeNano", "0")) // 1_000_000 or int(result.get("timeSecond", "0")) * 1000}

    def _cached(self, key, fn, ttl=180):
        now = time.monotonic()
        with self.lock:
            if key in self.cache and self.cache[key][0] > now:
                self.cache.move_to_end(key)
                return self.cache[key][1]
        value = fn()
        with self.lock:
            self.cache[key] = (time.monotonic() + ttl, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 512:
                self.cache.popitem(last=False)
        return value

    def _instruments(self):
        def load():
            rows = []
            cursor = None
            for _ in range(10):
                p = {"category": "linear", "limit": 1000}
                if cursor:
                    p["cursor"] = cursor
                result = self._get("/v5/market/instruments-info", p)
                rows.extend(result.get("list", []))
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            return rows
        return self._cached("instruments", load, 300)

    def universe(self):
        info_rows = self._instruments()
        tickers = self._get("/v5/market/tickers", {"category": "linear"}).get("list", [])
        now = self.get(self.time_path)["serverTime"]
        info = {"symbols": []}
        for x in info_rows:
            if x.get("settleCoin") == "USDT" and x.get("contractType") == "LinearPerpetual" and x.get("status") == "Trading":
                info["symbols"].append({
                    "symbol": x["symbol"],
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "baseAsset": x.get("baseCoin", ""),
                })
        normalized = []
        for x in tickers:
            sym = x.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            normalized.append({
                "symbol": sym,
                "lastPrice": x.get("lastPrice"),
                "quoteVolume": x.get("turnover24h"),
                "closeTime": now,
            })
        return {"info": info, "tickers": normalized, "now_ms": now, "instrument_kind": self.instrument_kind}

    def candles(self, symbol, interval, limit=100):
        if interval not in _INTERVAL:
            raise MarketError("Timeframe Bybit tidak didukung")
        result = self._get("/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": _INTERVAL[interval], "limit": min(1000, limit)})
        rows = result.get("list", [])
        if not rows:
            raise MarketError("Data candle Bybit kosong")
        rows = list(reversed(rows))
        step = INTERVAL_MS[interval]
        d = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "quote_volume"])
        d["open_time"] = pd.to_numeric(d.open_time, errors="coerce")
        d["close_time"] = d.open_time + step - 1
        for c in ("open", "high", "low", "close", "volume", "quote_volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def closed_candles(self, symbol, interval, server_ms, limit=100):
        step = INTERVAL_MS[interval]
        boundary = int(server_ms) // step

        def download():
            raw = validate_ohlcv_dataframe(self.candles(symbol, interval, limit))
            d = raw[raw.close_time < server_ms].reset_index(drop=True)
            if d.empty or int(d.close_time.iloc[-1]) != boundary * step - 1:
                raise MarketError("Candle Bybit terakhir belum terkonfirmasi closed")
            return d

        ttl = 900 if interval == "15m" else 300
        frame = self._cached((symbol, interval, boundary, limit), download, ttl)
        return frame.copy()

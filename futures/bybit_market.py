"""Bybit public USDT-linear perpetual market-data adapter. Read-only; never places orders."""
import re, time, threading, os
from collections import OrderedDict
import requests
import pandas as pd
from .models import Contract, Snapshot, TIMEFRAMES, MONITOR_TIMEFRAMES, INTERVAL_MS, IntrabarCandle, MonitorSnapshot
from .intrabar_stream import validate_ohlcv_dataframe
from .market import MarketError
from .security import GLOBAL_MONITOR

# Bybit V5 Integration Guidance (checked 2026-11-27): Indonesia uses
# api.bybit.id. The documented global mainnet endpoints are api.bybit.com and
# api.bytick.com. Do not probe removed/incorrect regional domains as fallbacks.
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
        self.session = session or requests.Session()
        self.allow_host_fallback = bool(allow_host_fallback)
        self.proxy_url = proxy_url
        env_base = os.environ.get("COINAGENT_BYBIT_BASE", "").strip().rstrip("/")
        if env_base:
            self.base = env_base
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self._live_cache = OrderedDict()
        self.cooldown_until = 0

    def _candidate_hosts(self):
        hosts = [self.base]
        if self.allow_host_fallback:
            for h in (self.base, *OFFICIAL_MAINNET_BASES):
                if h and h not in hosts:
                    hosts.append(h)
        return hosts

    def _request_one_base(self, base_url, path, params=None):
        kwargs = {"params": params, "timeout": (5, 12), "allow_redirects": False}
        # Only pass custom headers/proxies on real requests.Session so unit-test mock sessions still work
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
            "/v5/market/mark-price-kline",
            "/v5/market/orderbook",
            "/v5/market/funding/history",
        }:
            raise MarketError("Endpoint Bybit tidak diizinkan")
        if time.time() < self.cooldown_until:
            raise MarketError("Bybit meminta jeda; tunggu sebelum scan berikutnya")

        hosts = self._candidate_hosts()
        last_exc = None
        attempts = []
        for idx, host in enumerate(hosts):
            try:
                GLOBAL_MONITOR.increment('market_api_requests')
                r = self._request_one_base(host, path, params=params)
            except requests.RequestException:
                last_exc = MarketError("Koneksi data Bybit gagal. Periksa jaringan server hosting.", endpoint=path)
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                raise last_exc from None

            if r.status_code in (418, 429):
                self.cooldown_until = time.time() + 60
                raise MarketError(
                    "Batas permintaan Bybit tercapai; jeda 60 detik.",
                    http_status=r.status_code,
                    endpoint=path,
                    error_category="RATE_LIMIT",
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
                    self.cooldown_until = time.time() + 600
                    detail = "IP terkena batas frekuensi; hentikan request dan tunggu minimal 10 menit."
                    raise MarketError(
                        f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. {detail}",
                        http_status=r.status_code,
                        endpoint=path,
                        ret_code=rc,
                        error_category="RATE_LIMIT",
                        response_reason=body[:120],
                    )
                if any(w in body for w in ("unauthorized", "api key", "signature", "permission", "invalid key")) or rc in (10003, 10004, 10005, 10007):
                    detail = "Kesalahan otorisasi/autentikasi pada endpoint Bybit."
                    raise MarketError(
                        f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. {detail}",
                        http_status=r.status_code,
                        endpoint=path,
                        ret_code=rc,
                        error_category="AUTH_ERROR",
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
                    # HTTP 403 alone is ambiguous: Bybit also documents IP-rate-limit
                    # and source-IP restrictions. Do not label a generic/WAF 403 as geo.
                    cat_403 = "UNKNOWN_403"
                    detail = (
                        "HTTP 403 belum membuktikan geo-block atau pembatasan regional. Kemungkinan termasuk pembatasan IP hosting, "
                        "WAF/proxy, atau rate limit. Periksa host sumber dan respons HTTP."
                    )
                attempts.append(f"{host.removeprefix('https://')}: HTTP {r.status_code} ({cat_403})")
                last_exc = MarketError(
                    f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path} via {host}. {detail}",
                    http_status=r.status_code,
                    endpoint=path,
                    ret_code=rc,
                    error_category=cat_403,
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
                    http_status=r.status_code,
                    endpoint=path,
                    error_category=cat_other,
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
                    http_status=r.status_code,
                    endpoint=path,
                    ret_code=rc,
                    error_category=rc_cat,
                    response_reason=ret_msg[:120],
                )
            if host != self.base:
                self.base = host
            GLOBAL_MONITOR.increment('market_api_success')
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

    def server_time_ms(self):
        return int(self.get(self.time_path)["serverTime"])

    def _cached(self, key, fn, ttl=180):
        now = time.monotonic()
        if key in self.cache and self.cache[key][0] > now:
            return self.cache[key][1]
        value = fn()
        self.cache[key] = (now + ttl, value)
        while len(self.cache) > 64:
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
                    "marginAsset": "USDT",
                    "baseAsset": x.get("baseCoin", ""),
                })
        books = []
        normalized = []
        for x in tickers:
            sym = x.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            bid = float(x.get("bid1Price") or 0)
            ask = float(x.get("ask1Price") or 0)
            if not bid or not ask:
                continue
            books.append({"symbol": sym, "bidPrice": str(bid), "askPrice": str(ask), "time": now})
            normalized.append({
                "symbol": sym,
                "lastPrice": x.get("lastPrice"),
                "quoteVolume": x.get("turnover24h"),
                "highPrice": x.get("highPrice24h"),
                "lowPrice": x.get("lowPrice24h"),
                "priceChangePercent": str(float(x.get("price24hPcnt") or 0) * 100),
                "closeTime": now,
            })
        return {
            "info": info,
            "tickers": normalized,
            "books": books,
            "now_ms": now,
            "book_observed_ms": now,
            "instrument_kind": self.instrument_kind,
        }

    def candles(self, symbol, interval, limit=1000, mark=False):
        if interval not in _INTERVAL:
            raise MarketError("Timeframe Bybit tidak didukung")
        path = "/v5/market/mark-price-kline" if mark else "/v5/market/kline"
        result = self._get(path, {"category": "linear", "symbol": symbol, "interval": _INTERVAL[interval], "limit": min(1000, limit)})
        rows = result.get("list", [])
        if not rows:
            raise MarketError("Data candle Bybit kosong")
        rows = list(reversed(rows))
        step = INTERVAL_MS[interval]
        if mark:
            rows = [list(r[:5]) + ["0", "0"] for r in rows]
        d = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "quote_volume"])
        d["open_time"] = pd.to_numeric(d.open_time, errors="coerce")
        d["close_time"] = d.open_time + step - 1
        for c in ("open", "high", "low", "close", "volume", "quote_volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def closed_candles(self, symbol, interval, server_ms, limit=1000):
        step = INTERVAL_MS[interval]
        d = self.candles(symbol, interval, limit)
        d = d[d.close_time < server_ms].reset_index(drop=True)
        if d.empty or int(d.close_time.iloc[-1]) != (server_ms // step) * step - 1:
            raise MarketError("Candle Bybit terakhir belum terkonfirmasi closed")
        return d

    def load(self, symbol, mode):
        return self.load_modes(symbol, (mode,))[mode]

    def load_modes(self, symbol, modes=tuple(TIMEFRAMES)):
        symbol = symbol.strip().upper()
        modes = tuple(dict.fromkeys(modes))
        if not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol) or not modes or any(m not in TIMEFRAMES for m in modes):
            raise MarketError("Simbol/timeframe Bybit tidak valid")
        now = self.get(self.time_path)["serverTime"]
        raw = next(
            (x for x in self._instruments()
             if x.get("symbol") == symbol and x.get("settleCoin") == "USDT" and x.get("contractType") == "LinearPerpetual" and x.get("status") == "Trading"),
            None,
        )
        if not raw:
            raise MarketError("Kontrak USDT perpetual tidak tersedia di Bybit")
        lot = raw.get("lotSizeFilter", {})
        price = raw.get("priceFilter", {})
        try:
            step = float(lot["qtyStep"])
            tick = float(price["tickSize"])
            minq = float(lot["minOrderQty"])
            maxq = float(lot.get("maxOrderQty", lot.get("maxMktOrderQty")))
            spec = Contract(
                tick=tick, step=step, min_qty=minq, max_qty=maxq,
                min_notional=float(lot.get("minNotionalValue", 5)),
                market_step=step, market_min_qty=minq,
                market_max_qty=float(lot.get("maxMktOrderQty", maxq)),
                min_price=float(price.get("minPrice", 0)),
                max_price=float(price.get("maxPrice", float("inf"))),
                status="TRADING", contract_type="PERPETUAL", quote_asset="USDT",
            )
        except (KeyError, ValueError, TypeError):
            raise MarketError("Spesifikasi tick/lot Bybit tidak lengkap") from None
        frames = {tf: self.closed_candles(symbol, tf, now) for tf in dict.fromkeys(tf for m in modes for tf in TIMEFRAMES[m])}
        daily = self.closed_candles(symbol, "1d", now, 4)
        day = (now // 86400000) * 86400000
        prev = daily[daily.open_time == day - 86400000]
        ticker = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol}).get("list", [])
        book = self._get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 100})
        if not ticker:
            raise MarketError("Ticker Bybit tidak tersedia")
        t = ticker[0]
        bids = book.get("b", [])
        asks = book.get("a", [])
        if not bids or not asks:
            raise MarketError("Orderbook Bybit kosong")
        mark = float(t.get("markPrice") or t["lastPrice"])
        last = float(t["lastPrice"])
        funding = float(t["fundingRate"]) if t.get("fundingRate") not in (None, "") else None
        funding_interval = float(raw.get("fundingInterval", 0)) / 60 if raw.get("fundingInterval") not in (None, "", 0, "0") else None
        next_funding = int(t.get("nextFundingTime") or 0) or None
        snaps = {}
        for mode in modes:
            btf, etf = TIMEFRAMES[mode]
            openrows = self.candles(symbol, etf, 2)
            markrows = self.candles(symbol, etf, 2, True)
            opened = (now // INTERVAL_MS[etf]) * INTERVAL_MS[etf]
            op = openrows[openrows.open_time >= opened]
            mp = markrows[markrows.open_time >= opened]
            snaps[mode] = Snapshot(
                symbol=symbol, mode=mode, now_ms=now, bias=frames[btf], execution=frames[etf], contract=spec,
                last=last, mark=mark, bid=float(bids[0][0]), ask=float(asks[0][0]),
                quote_times={"last": now, "mark": now, "book": int(book.get("ts", now)), "depth": int(book.get("ts", now))},
                bids=bids, asks=asks,
                previous_day_high=float(prev.iloc[-1].high) if len(prev) else None,
                previous_day_low=float(prev.iloc[-1].low) if len(prev) else None,
                funding_rate=funding, next_funding_ms=next_funding,
                funding_interval_hours=funding_interval or 8,
                open_bar_high=float(op.high.max()) if len(op) else None,
                open_bar_low=float(op.low.min()) if len(op) else None,
                open_mark_high=float(mp.high.max()) if len(mp) else None,
                open_mark_low=float(mp.low.min()) if len(mp) else None,
                source=self.source_label, instrument_kind=self.instrument_kind,
                index_price=float(t.get("indexPrice") or mark),
                open_interest=float(t.get("openInterest") or 0.0) if t.get("openInterest") not in (None, "") else None,
                data_timestamp=int(book.get("ts", now)),
                received_timestamp=int(time.time() * 1000),
                market_provider=self.market_label,
                warnings=["Data berasal dari Bybit; harga dan likuiditas dapat berbeda dari Binance."]
                + ([] if funding_interval else ["Interval funding kontrak tidak tersedia; tidak diasumsikan."]),
            )
        return snaps

    def live_candles(self, symbol, interval, server_ms=None, limit=120):
        if interval not in _INTERVAL:
            raise MarketError(f"Interval Bybit tidak didukung: {interval}")
        now_ms = int(server_ms if server_ms is not None else self.server_time_ms())
        raw = validate_ohlcv_dataframe(self.candles(symbol, interval, limit))
        if raw.empty:
            raise MarketError("Data live candle Bybit kosong atau OHLC tidak valid.")
        raw["is_closed"] = raw["close_time"] < now_ms
        with self.lock:
            self._live_cache[(symbol, interval)] = (time.monotonic() + 3.0, now_ms, raw.copy())
            while len(self._live_cache) > 64:
                self._live_cache.popitem(last=False)
        return raw.copy()

    def current_candle(self, symbol, interval, server_ms=None, stale_threshold_ms=30000):
        now_ms = int(server_ms if server_ms is not None else self.server_time_ms())
        rx_ms = int(time.time() * 1000)
        df = self.live_candles(symbol, interval, server_ms=now_ms, limit=5)
        running = df[~df["is_closed"]]
        row = running.iloc[-1] if len(running) else df.iloc[-1]
        freshness = max(0, rx_ms - now_ms)
        return IntrabarCandle(
            symbol=str(symbol).upper(),
            timeframe=str(interval),
            open_time=int(row["open_time"]),
            close_time=int(row["close_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            is_closed=bool(row["is_closed"]),
            provider=self.market_label,
            instrument_kind=self.instrument_kind,
            exchange_timestamp_ms=now_ms,
            local_receive_ms=rx_ms,
            data_freshness_ms=freshness,
            is_stale=freshness > int(stale_threshold_ms),
        )

    def monitor_snapshot(self, symbol, intervals=MONITOR_TIMEFRAMES, server_ms=None, stale_threshold_ms=30000, limit=120):
        sym = str(symbol).strip().upper()
        now_ms = int(server_ms if server_ms is not None else self.server_time_ms())
        frames_closed, frames_live, current_bars = {}, {}, {}
        for tf in intervals:
            live_df = self.live_candles(sym, tf, server_ms=now_ms, limit=limit)
            frames_live[tf] = live_df
            frames_closed[tf] = live_df[live_df["is_closed"]].reset_index(drop=True)
            running = live_df[~live_df["is_closed"]]
            row = running.iloc[-1] if len(running) else live_df.iloc[-1]
            rx_ms = int(time.time() * 1000)
            fresh = max(0, rx_ms - now_ms)
            current_bars[tf] = IntrabarCandle(
                symbol=sym,
                timeframe=tf,
                open_time=int(row["open_time"]),
                close_time=int(row["close_time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                is_closed=bool(row["is_closed"]),
                provider=self.market_label,
                instrument_kind=self.instrument_kind,
                exchange_timestamp_ms=now_ms,
                local_receive_ms=rx_ms,
                data_freshness_ms=fresh,
                is_stale=fresh > int(stale_threshold_ms),
            )
        tk_res = self._get("/v5/market/tickers", {"category": "linear", "symbol": sym})
        tlist = tk_res.get("list") or (tk_res.get("result") or {}).get("list") or []
        if not tlist:
            raise MarketError(f"Ticker Bybit tidak ditemukan untuk {sym}")
        tk = tlist[0]
        rx_ms = int(time.time() * 1000)
        ex_ts = int(tk_res.get("time") or now_ms)
        freshness = max(0, rx_ms - ex_ts)
        return MonitorSnapshot(
            symbol=sym,
            provider=self.market_label,
            instrument_kind=self.instrument_kind,
            exchange_timestamp_ms=ex_ts,
            local_receive_ms=rx_ms,
            last_price=float(tk["lastPrice"]),
            mark_price=float(tk["markPrice"]),
            index_price=float(tk["indexPrice"]) if tk.get("indexPrice") not in (None, "") else float(tk["markPrice"]),
            bid=float(tk.get("bid1Price") or tk["lastPrice"]),
            ask=float(tk.get("ask1Price") or tk["lastPrice"]),
            frames_closed=frames_closed,
            frames_live=frames_live,
            current_candles=current_bars,
            data_freshness_ms=freshness,
            is_stale=freshness > int(stale_threshold_ms),
            transport="REST",
        )

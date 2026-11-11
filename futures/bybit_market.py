"""Bybit public USDT-linear perpetual market-data adapter. Read-only; never places orders."""
import re, time, threading, os
from collections import OrderedDict
import requests
import pandas as pd
from .models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS
from .market import MarketError

# Bybit's official API integration guidance specifies api.bybit.id for Indonesia,
# plus official global/mirror endpoints (api.bytick.com, api.bybit.com) when cloud
# runners (e.g. GitHub Actions US/EU) are blocked by regional CloudFront rules.
BASE = os.environ.get("COINAGENT_BYBIT_BASE", "https://api.bybit.id").rstrip("/")
FALLBACK_BASES = (
    BASE,
    "https://api.bytick.com",
    "https://api.bybit.com",
    "https://api.bybit.nl",
    "https://api.bybit-tr.com",
)
_INTERVAL = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class BybitFuturesMarket:
    base = BASE
    time_path = "/v5/market/time"
    market_label = "Bybit USDT Perpetual"
    instrument_kind = "BYBIT_LINEAR_PERPETUAL"
    source_label = "Bybit Linear USDT Perpetual public REST"

    def __init__(self, session=None, allow_host_fallback=False):
        self.session = session or requests.Session()
        self.allow_host_fallback = bool(allow_host_fallback)
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.cooldown_until = 0

    def _request_one_base(self, base_url, path, params=None):
        kwargs = {"params": params, "timeout": (5, 12), "allow_redirects": False}
        # Only pass custom headers on real requests.Session so unit-test mock sessions without headers param still work
        if isinstance(self.session, requests.Session):
            kwargs["headers"] = _DEFAULT_HEADERS
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

        hosts = [self.base]
        if self.allow_host_fallback:
            for h in FALLBACK_BASES:
                if h not in hosts:
                    hosts.append(h)

        last_exc = None
        for idx, host in enumerate(hosts):
            try:
                r = self._request_one_base(host, path, params=params)
            except requests.RequestException:
                last_exc = MarketError("Koneksi data Bybit gagal. Periksa jaringan server hosting.", endpoint=path)
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                raise last_exc from None

            if r.status_code in (418, 429):
                self.cooldown_until = time.time() + 60
                raise MarketError("Batas permintaan Bybit tercapai; jeda 60 detik.", http_status=r.status_code, endpoint=path)

            if r.status_code in (403, 451):
                body = (getattr(r, "text", "") or "").lower()
                if "too frequent" in body or "access too frequent" in body:
                    self.cooldown_until = time.time() + 600
                    detail = "IP terkena batas frekuensi; hentikan request dan tunggu minimal 10 menit."
                    raise MarketError(f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. {detail}", http_status=r.status_code, endpoint=path)
                detail = "Periksa endpoint regional Bybit dan pembatasan wilayah/IP hosting."
                last_exc = MarketError(f"Akses Bybit ditolak (HTTP {r.status_code}) pada {path}. {detail}", http_status=r.status_code, endpoint=path)
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                raise last_exc

            if r.status_code != 200:
                last_exc = MarketError(f"Data Bybit tidak tersedia (HTTP {r.status_code}) pada {path}.", http_status=r.status_code, endpoint=path)
                if self.allow_host_fallback and idx + 1 < len(hosts):
                    continue
                raise last_exc

            try:
                data = r.json()
            except ValueError:
                raise MarketError("Respons Bybit bukan JSON valid", endpoint=path) from None
            if data.get("retCode", 0) != 0:
                raise MarketError("Bybit menolak permintaan data publik: " + str(data.get("retCode")), endpoint=path)
            if host != self.base:
                self.base = host
            result = data.get("result", data)
            if path == "/v5/market/time" and isinstance(result, dict):
                result = dict(result, timeNano=data.get("timeNano"), timeSecond=data.get("timeSecond"))
            return result

        if last_exc is not None:
            raise last_exc
        raise MarketError("Data Bybit tidak tersedia.", endpoint=path)

    def get(self, path, params=None):
        if path != self.time_path:
            raise MarketError("Bybit adapter hanya menyediakan endpoint waktu melalui get")
        result = self._get(path)
        return {"serverTime": int(result.get("timeNano", "0")) // 1_000_000 or int(result.get("timeSecond", "0")) * 1000}

    def _cached(self, key, fn, ttl=180):
        now = time.monotonic()
        if key in self.cache and self.cache[key][0] > now:
            return self.cache[key][1]
        value = fn()
        self.cache[key] = (now + ttl, value)
        while len(self.cache) > 128:
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
                warnings=["Data berasal dari Bybit; harga dan likuiditas dapat berbeda dari Binance."]
                + ([] if funding_interval else ["Interval funding kontrak tidak tersedia; tidak diasumsikan."]),
            )
        return snaps

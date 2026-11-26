"""Multi-tier market-provider failover:
1. Binance USD-M Futures (PRIMARY — `USD_M_PERPETUAL`)
2. Bybit USDT Linear Perpetual (SECONDARY FAILOVER — `BYBIT_LINEAR_PERPETUAL`)
3. Bitget USDT-M Perpetual (TERTIARY FAILOVER — `BITGET_USDT_PERPETUAL`, authentic Perpetual Futures)
4. Binance Spot Reference (`data-api.binance.vision`) (REFERENCE ONLY — `SPOT_REFERENCE`, never a Futures execution source;
   when no Futures provider is valid, system status is `FUTURES_DATA_UNAVAILABLE`).
"""
from collections import OrderedDict
import os
import time
import requests
import pandas as pd

from .market import FuturesMarket, MarketError
from .bybit_market import BybitFuturesMarket
from .spot_market import SpotReferenceMarket
from .models import INTERVAL_MS
from .security import GLOBAL_MONITOR

VALID_FUTURES_KINDS = ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "BITGET_USDT_PERPETUAL")

_BITGET_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}
_BITGET_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class BitgetFuturesMarket:
    """Bitget public USDT-Futures perpetual adapter. Read-only; never places orders."""

    base = os.environ.get("COINAGENT_BITGET_BASE", "https://api.bitget.com").rstrip("/")
    time_path = "/api/v2/public/time"
    market_label = "Bitget USDT Perpetual"
    instrument_kind = "BITGET_USDT_PERPETUAL"
    source_label = "Bitget USDT-Futures public REST"

    def __init__(self, session=None, proxy_url=None):
        self.session = session or requests.Session()
        self.proxy_url = proxy_url
        env_base = os.environ.get("COINAGENT_BITGET_BASE", "").strip().rstrip("/")
        if env_base:
            self.base = env_base
        self.cache = OrderedDict()
        self.cooldown_until = 0

    def _proxies(self):
        proxy = (
            self.proxy_url
            or os.environ.get("COINAGENT_BITGET_PROXY")
            or os.environ.get("COINAGENT_MARKET_PROXY")
            or ""
        ).strip()
        return {"http": proxy, "https": proxy} if proxy else None

    def _get(self, path, params=None):
        if path not in {
            "/api/v2/public/time",
            "/api/v2/mix/market/contracts",
            "/api/v2/mix/market/tickers",
            "/api/v2/mix/market/candles",
        }:
            raise MarketError("Endpoint Bitget tidak diizinkan", endpoint=path)
        if time.time() < self.cooldown_until:
            raise MarketError("Bitget meminta jeda; tunggu sebelum scan berikutnya", endpoint=path)
        kwargs = {"params": params, "timeout": (5, 12), "allow_redirects": False}
        if isinstance(self.session, requests.Session):
            kwargs["headers"] = _BITGET_HEADERS
            proxies = self._proxies()
            if proxies:
                kwargs["proxies"] = proxies
        try:
            GLOBAL_MONITOR.increment("market_api_requests")
            r = self.session.get(self.base + path, **kwargs)
        except requests.RequestException:
            raise MarketError("Koneksi data Bitget gagal. Periksa jaringan server hosting.", endpoint=path) from None

        if r.status_code in (418, 429):
            self.cooldown_until = time.time() + 60
            raise MarketError(
                "Batas permintaan Bitget tercapai; jeda 60 detik.",
                http_status=r.status_code,
                endpoint=path,
                error_category="RATE_LIMIT",
            )
        if r.status_code in (403, 451):
            cat = "REGIONAL_RESTRICTION" if r.status_code == 451 else "UNKNOWN_403"
            raise MarketError(
                f"Akses Bitget ditolak (HTTP {r.status_code}) pada {path}.",
                http_status=r.status_code,
                endpoint=path,
                error_category=cat,
                response_reason=(getattr(r, "text", "") or "")[:120],
            )
        if r.status_code != 200:
            raise MarketError(
                f"Data Bitget tidak tersedia (HTTP {r.status_code}) pada {path}.",
                http_status=r.status_code,
                endpoint=path,
                error_category="NETWORK_ERROR",
            )
        try:
            payload = r.json()
        except ValueError:
            raise MarketError("Respons Bitget bukan JSON valid", endpoint=path, error_category="INVALID_RESPONSE") from None
        if str(payload.get("code", "")) != "00000":
            raise MarketError(
                f"Bitget menolak permintaan data publik: {payload.get('msg') or payload.get('code')}",
                http_status=r.status_code,
                endpoint=path,
                error_category="INVALID_RESPONSE",
            )
        GLOBAL_MONITOR.increment("market_api_success")
        return payload.get("data")

    def get(self, path, params=None):
        if path != self.time_path:
            raise MarketError("Bitget adapter hanya menyediakan endpoint waktu melalui get")
        data = self._get(self.time_path)
        server_time = int((data or {}).get("serverTime") or int(time.time() * 1000))
        return {"serverTime": server_time}

    def _cached(self, key, fn, ttl=180):
        now = time.monotonic()
        if key in self.cache and self.cache[key][0] > now:
            return self.cache[key][1]
        val = fn()
        self.cache[key] = (now + ttl, val)
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        return val

    def universe(self):
        contracts = self._cached(
            "contracts",
            lambda: self._get("/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"}) or [],
            300,
        )
        tickers = self._get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"}) or []
        now = self.get(self.time_path)["serverTime"]

        info = {"symbols": []}
        for x in contracts:
            sym = str(x.get("symbol") or "").upper()
            status = str(x.get("symbolStatus") or "").lower()
            quote = str(x.get("quoteCoin") or "USDT").upper()
            if sym.endswith("USDT") and quote == "USDT" and status == "normal":
                info["symbols"].append({
                    "symbol": sym,
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "baseAsset": str(x.get("baseCoin") or "").upper(),
                })

        books = []
        normalized = []
        for x in tickers:
            sym = str(x.get("symbol") or "").upper()
            if not sym.endswith("USDT"):
                continue
            bid = float(x.get("bidPr") or x.get("lastPr") or 0)
            ask = float(x.get("askPr") or x.get("lastPr") or 0)
            if not bid or not ask:
                continue
            books.append({"symbol": sym, "bidPrice": str(bid), "askPrice": str(ask), "time": now})
            normalized.append({
                "symbol": sym,
                "lastPrice": x.get("lastPr"),
                "quoteVolume": x.get("usdtVolume") or x.get("quoteVolume") or "0",
                "highPrice": x.get("high24h"),
                "lowPrice": x.get("low24h"),
                "priceChangePercent": str(float(x.get("change24h") or 0) * 100),
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

    def candles(self, symbol, interval, limit=200):
        if interval not in _BITGET_INTERVAL:
            raise MarketError("Timeframe Bitget tidak didukung")
        rows = self._get(
            "/api/v2/mix/market/candles",
            {
                "symbol": str(symbol).upper(),
                "productType": "USDT-FUTURES",
                "granularity": _BITGET_INTERVAL[interval],
                "limit": min(200, int(limit)),
            },
        ) or []
        if not rows:
            raise MarketError("Data candle Bitget kosong")
        step = INTERVAL_MS[interval]
        d = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "quote_volume"])
        d["open_time"] = pd.to_numeric(d.open_time, errors="coerce")
        d = d.dropna(subset=["open_time"]).sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
        d["close_time"] = d.open_time + step - 1
        for c in ("open", "high", "low", "close", "volume", "quote_volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def closed_candles(self, symbol, interval, server_ms, limit=200):
        step = INTERVAL_MS[interval]
        d = self.candles(symbol, interval, limit)
        d = d[d.close_time < server_ms].reset_index(drop=True)
        if d.empty or int(d.close_time.iloc[-1]) != (server_ms // step) * step - 1:
            raise MarketError("Candle Bitget terakhir belum terkonfirmasi closed")
        return d


def classify_provider_error(exc):
    """Classify MarketError into REGIONAL_RESTRICTION, RATE_LIMIT, AUTH_ERROR, UNKNOWN_403, NETWORK_ERROR, or INVALID_RESPONSE."""
    explicit_cat = getattr(exc, "error_category", None)
    if explicit_cat in ("REGIONAL_RESTRICTION", "RATE_LIMIT", "AUTH_ERROR", "UNKNOWN_403", "NETWORK_ERROR", "INVALID_RESPONSE"):
        return explicit_cat
    rc = getattr(exc, "ret_code", None)
    if rc is not None:
        if rc == 10024:
            return "REGIONAL_RESTRICTION"
        if rc in (10006, 10018):
            return "RATE_LIMIT"
        if rc in (10003, 10004, 10005, 10007, 10010, 33004):
            return "AUTH_ERROR"
        if rc != 0:
            return "INVALID_RESPONSE"
    status = getattr(exc, "http_status", None)
    msg = (str(exc or "") + " " + str(getattr(exc, "response_reason", "") or "")).lower()
    if status == 451:
        return "REGIONAL_RESTRICTION"
    if status == 401:
        return "AUTH_ERROR"
    if status == 403:
        if any(w in msg for w in ("too frequent", "rate limit", "batas frekuensi", "batas permintaan", "10006")):
            return "RATE_LIMIT"
        if any(w in msg for w in ("unauthorized", "api key", "signature", "permission", "invalid key", "auth_error", "10003", "10004", "10005")):
            return "AUTH_ERROR"
        if any(w in msg for w in ("unknown", "unverified", "csrf", "generic")):
            return "UNKNOWN_403"
        if any(w in msg for w in ("geo", "country", "region", "wilayah", "cloudfront", "restricted location", "blocked", "http 403 forbidden", "10024")):
            return "REGIONAL_RESTRICTION"
        return "UNKNOWN_403"
    if status in (418, 429) or "rate limit" in msg or "batas permintaan" in msg or "meminta jeda" in msg:
        return "RATE_LIMIT"
    if (
        "bukan json valid" in msg
        or "retcode" in msg
        or "menolak permintaan data publik" in msg
        or "tidak lengkap" in msg
        or "tidak valid" in msg
        or "tidak diizinkan" in msg
    ):
        return "INVALID_RESPONSE"
    if status is None or (isinstance(status, int) and status >= 500) or "koneksi" in msg or "timeout" in msg or "tidak tersedia" in msg:
        return "NETWORK_ERROR"
    return "PROVIDER_ERROR"


class FuturesProviderRouter:
    def __init__(
        self,
        primary=None,
        fallback=None,
        spot_fallback=None,
        fallback_enabled=True,
        spot_fallback_enabled=None,
        tertiary=None,
        tertiary_enabled=None,
    ):
        self.primary = primary or FuturesMarket()
        self.fallback = fallback or BybitFuturesMarket(allow_host_fallback=True)
        if tertiary_enabled is None:
            tertiary_enabled = (primary is None and fallback is None) or (tertiary is not None)
        self.tertiary = tertiary or (BitgetFuturesMarket() if tertiary_enabled else None)
        if spot_fallback_enabled is None:
            spot_fallback_enabled = (primary is None and fallback is None) or (spot_fallback is not None)
        self.spot_fallback = spot_fallback or (SpotReferenceMarket() if spot_fallback_enabled else None)

        self.fallback_enabled = bool(fallback_enabled)
        self.tertiary_enabled = bool(tertiary_enabled and self.tertiary is not None)
        self.spot_fallback_enabled = bool(spot_fallback_enabled and self.spot_fallback is not None)

        self.active = self.primary
        self.failover_reason = None
        self.last_error_classification = None
        self.futures_data_unavailable = False
        self.primary_http_status = None
        self.primary_error_class = None
        self.fallback_http_status = None
        self.fallback_error_class = None
        self.tertiary_http_status = None
        self.tertiary_error_class = None
        self.last_successful_futures_ms = None

    @property
    def futures_available(self) -> bool:
        """True only when an authentic Perpetual Futures provider is active and reachable."""
        return (
            not self.futures_data_unavailable
            and getattr(self.active, "instrument_kind", "") in VALID_FUTURES_KINDS
        )

    @property
    def data_status(self) -> str:
        if not self.futures_available:
            return "FUTURES_DATA_UNAVAILABLE"
        return "OK"

    @staticmethod
    def _remap_time_args(provider, name, args):
        if name == "get" and args and isinstance(args[0], str) and args[0].endswith("/time"):
            return (getattr(provider, "time_path", args[0]),) + tuple(args[1:])
        return args

    def _try_tertiary(self, name, args, kwargs, prior_reason: str):
        self.active = self.tertiary
        self.failover_reason = (
            f"{prior_reason}; switched to Bitget USDT perpetual public market data (BITGET_USDT_PERPETUAL)"
        )
        t_args = self._remap_time_args(self.active, name, args)
        try:
            res = getattr(self.active, name)(*t_args, **kwargs)
            self.futures_data_unavailable = False
            self.last_successful_futures_ms = int(time.time() * 1000)
            return res
        except MarketError as tert_exc:
            tert_class = classify_provider_error(tert_exc)
            self.tertiary_http_status = getattr(tert_exc, "http_status", None)
            self.tertiary_error_class = tert_class
            self.last_error_classification = tert_class
            self.futures_data_unavailable = True
            raise

    def _invoke(self, name, *args, **kwargs):
        call_args = self._remap_time_args(self.active, name, args)
        method = getattr(self.active, name)
        try:
            res = method(*call_args, **kwargs)
            if getattr(self.active, "instrument_kind", "") in VALID_FUTURES_KINDS:
                self.futures_data_unavailable = False
                self.last_successful_futures_ms = int(time.time() * 1000)
            else:
                self.futures_data_unavailable = True
            return res
        except MarketError as exc:
            err_class = classify_provider_error(exc)
            self.last_error_classification = err_class
            if self.active is self.primary:
                self.primary_http_status = getattr(exc, "http_status", None)
                self.primary_error_class = err_class
            elif self.active is self.fallback:
                self.fallback_http_status = getattr(exc, "http_status", None)
                self.fallback_error_class = err_class
            elif self.active is self.tertiary:
                self.tertiary_http_status = getattr(exc, "http_status", None)
                self.tertiary_error_class = err_class

            # Tier 1 -> Tier 2: Binance blocked (451/403) -> switch to Bybit Perpetual
            if (
                self.active is self.primary
                and self.fallback_enabled
                and (getattr(exc, "access_blocked", False) or err_class in ("REGIONAL_RESTRICTION", "UNKNOWN_403"))
            ):
                self.active = self.fallback
                self.failover_reason = (
                    f"Binance Futures HTTP {exc.http_status or 451} ({err_class}); "
                    "switched to Bybit USDT perpetual public market data (BYBIT_LINEAR_PERPETUAL)"
                )
                fb_args = self._remap_time_args(self.active, name, args)
                try:
                    res = getattr(self.active, name)(*fb_args, **kwargs)
                    self.futures_data_unavailable = False
                    self.last_successful_futures_ms = int(time.time() * 1000)
                    return res
                except MarketError as bybit_exc:
                    bybit_class = classify_provider_error(bybit_exc)
                    self.fallback_http_status = getattr(bybit_exc, "http_status", None)
                    self.fallback_error_class = bybit_class
                    self.last_error_classification = bybit_class
                    self.futures_data_unavailable = True

                    # Tier 2 -> Tier 3: Bybit also blocked (403/451) -> switch to Bitget USDT Perpetual
                    if (
                        self.tertiary_enabled
                        and (
                            bybit_class in ("REGIONAL_RESTRICTION", "UNKNOWN_403")
                            or getattr(bybit_exc, "http_status", None) in (403, 451)
                        )
                    ):
                        prior = (
                            f"Binance HTTP {exc.http_status or 451} ({err_class}) & "
                            f"Bybit HTTP {bybit_exc.http_status} ({bybit_class})"
                        )
                        try:
                            return self._try_tertiary(name, args, kwargs, prior)
                        except MarketError:
                            if not self.spot_fallback_enabled:
                                raise

                    # Tier 4 (Reference only): only if explicitly enabled & geo-blocked
                    if (
                        self.spot_fallback_enabled
                        and bybit_class == "REGIONAL_RESTRICTION"
                        and getattr(bybit_exc, "http_status", None) in (403, 451)
                    ):
                        self.active = self.spot_fallback
                        self.failover_reason = (
                            f"FUTURES_DATA_UNAVAILABLE: Binance Futures HTTP {exc.http_status or 451} (REGIONAL_RESTRICTION) & "
                            f"Bybit HTTP {bybit_exc.http_status} (REGIONAL_RESTRICTION); "
                            "switched to Binance Spot Reference (SPOT_REFERENCE — REFERENCE ONLY, NO FUTURES PAPER_ENTRY)"
                        )
                        spot_args = self._remap_time_args(self.active, name, args)
                        return getattr(self.active, name)(*spot_args, **kwargs)
                    raise

            # Already on Tier 2 (Bybit) and gets blocked on subsequent call -> Tier 3 (Bitget) then optional Spot Reference
            if (
                self.active is self.fallback
                and (err_class in ("REGIONAL_RESTRICTION", "UNKNOWN_403") or getattr(exc, "http_status", None) in (403, 451))
            ):
                if self.tertiary_enabled:
                    return self._try_tertiary(name, args, kwargs, f"Bybit HTTP {exc.http_status} ({err_class})")
                if self.spot_fallback_enabled and err_class == "REGIONAL_RESTRICTION":
                    self.futures_data_unavailable = True
                    self.active = self.spot_fallback
                    self.failover_reason = (
                        f"FUTURES_DATA_UNAVAILABLE: Bybit HTTP {exc.http_status} (REGIONAL_RESTRICTION); "
                        "switched to Binance Spot Reference (SPOT_REFERENCE — REFERENCE ONLY, NO FUTURES PAPER_ENTRY)"
                    )
                    spot_args = self._remap_time_args(self.active, name, args)
                    return getattr(self.active, name)(*spot_args, **kwargs)

            self.futures_data_unavailable = True
            raise

    def get(self, *args, **kwargs):
        return self._invoke("get", *args, **kwargs)

    def universe(self, *args, **kwargs):
        return self._invoke("universe", *args, **kwargs)

    def candles(self, *args, **kwargs):
        return self._invoke("candles", *args, **kwargs)

    def closed_candles(self, *args, **kwargs):
        return self._invoke("closed_candles", *args, **kwargs)

    def live_candles(self, *args, **kwargs):
        return self._invoke("live_candles", *args, **kwargs)

    def current_candle(self, *args, **kwargs):
        return self._invoke("current_candle", *args, **kwargs)

    def monitor_snapshot(self, *args, **kwargs):
        snap = self._invoke("monitor_snapshot", *args, **kwargs)
        if snap is not None and self.failover_reason and not getattr(snap, "failover_reason", None):
            snap.failover_reason = self.failover_reason
        return snap

    def load(self, *args, **kwargs):
        return self._invoke("load", *args, **kwargs)

    def load_modes(self, *args, **kwargs):
        return self._invoke("load_modes", *args, **kwargs)

    @property
    def time_path(self):
        return self.active.time_path

    @property
    def instrument_kind(self):
        return self.active.instrument_kind

    @property
    def market_label(self):
        return self.active.market_label

    @property
    def source_label(self):
        return getattr(self.active, "source_label", getattr(self.active, "market_label", "Binance USD-M Futures public REST"))

    def provider_diagnostics(self) -> dict:
        return {
            "active_provider": self.market_label,
            "instrument_kind": self.instrument_kind,
            "futures_available": self.futures_available,
            "data_status": self.data_status,
            "primary_http_status": self.primary_http_status,
            "primary_error_class": self.primary_error_class,
            "fallback_http_status": self.fallback_http_status,
            "fallback_error_class": self.fallback_error_class,
            "tertiary_http_status": self.tertiary_http_status,
            "tertiary_error_class": self.tertiary_error_class,
            "last_successful_futures_ms": self.last_successful_futures_ms,
            "provider_failover_reason": self.failover_reason,
        }

    def verify_futures_providers(self) -> dict:
        out = {
            "primary": {"provider": self.primary.market_label, "instrument_kind": self.primary.instrument_kind, "ok": False, "http_status": None, "error_class": None, "error_detail": ""},
            "fallback": {"provider": self.fallback.market_label, "instrument_kind": self.fallback.instrument_kind, "ok": False, "http_status": None, "error_class": None, "error_detail": ""},
            "futures_available": False,
            "data_status": "FUTURES_DATA_UNAVAILABLE",
        }
        roles = [("primary", self.primary), ("fallback", self.fallback)]
        if self.tertiary is not None:
            out["tertiary"] = {"provider": self.tertiary.market_label, "instrument_kind": self.tertiary.instrument_kind, "ok": False, "http_status": None, "error_class": None, "error_detail": ""}
            roles.append(("tertiary", self.tertiary))

        for role, prov in roles:
            try:
                prov.get(getattr(prov, "time_path", "/fapi/v1/time"))
                out[role]["ok"] = True
                out[role]["http_status"] = 200
            except MarketError as exc:
                cls = classify_provider_error(exc)
                out[role]["ok"] = False
                out[role]["http_status"] = getattr(exc, "http_status", None)
                out[role]["error_class"] = cls
                out[role]["error_detail"] = str(exc)[:180]
                if role == "primary":
                    self.primary_http_status = out[role]["http_status"]
                    self.primary_error_class = cls
                elif role == "fallback":
                    self.fallback_http_status = out[role]["http_status"]
                    self.fallback_error_class = cls
                else:
                    self.tertiary_http_status = out[role]["http_status"]
                    self.tertiary_error_class = cls
            except Exception as exc:
                out[role]["ok"] = False
                out[role]["error_class"] = "NETWORK_ERROR"
                out[role]["error_detail"] = str(exc)[:180]

        any_ok = any(out[r]["ok"] for r, _ in roles)
        out["futures_available"] = bool(any_ok)
        out["data_status"] = "OK" if any_ok else "FUTURES_DATA_UNAVAILABLE"
        return out


def validate_futures_contract_spec(contract, symbol: str = "") -> tuple[bool, list[str]]:
    errors = []
    if contract is None:
        return False, ["CONTRACT_SPEC_MISSING"]
    if str(getattr(contract, "status", "")).upper() != "TRADING":
        errors.append(f"CONTRACT_NOT_TRADING:{getattr(contract, 'status', '')}")
    if str(getattr(contract, "contract_type", "")).upper() != "PERPETUAL":
        errors.append(f"NOT_PERPETUAL:{getattr(contract, 'contract_type', '')}")
    if str(getattr(contract, "quote_asset", "")).upper() != "USDT":
        errors.append(f"NON_USDT_QUOTE:{getattr(contract, 'quote_asset', '')}")
    for fld in ("tick", "step", "min_qty", "min_notional"):
        val = getattr(contract, fld, 0)
        if val is None or float(val) <= 0:
            errors.append(f"INVALID_{fld.upper()}:{val}")
    return (len(errors) == 0), errors

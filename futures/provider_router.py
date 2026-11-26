"""Multi-tier market-provider failover (BUG-12, BUG-30 & v6.0.4 Section 2 Futures Primary):
1. Binance USD-M Futures (PRIMARY — `USD_M_PERPETUAL`)
2. Bybit USDT Linear Perpetual (SECONDARY FAILOVER — `BYBIT_LINEAR_PERPETUAL` on HTTP 451 REGIONAL_RESTRICTION)
3. Binance Spot Reference (`data-api.binance.vision`) (REFERENCE ONLY — `SPOT_REFERENCE`, never a Futures execution source;
   when no Futures provider is valid, system status is `FUTURES_DATA_UNAVAILABLE`).
"""
from .market import FuturesMarket, MarketError
from .bybit_market import BybitFuturesMarket
from .spot_market import SpotReferenceMarket


def classify_provider_error(exc):
    """BUG-30 & BUG-35: Classify MarketError into REGIONAL_RESTRICTION, RATE_LIMIT, AUTH_ERROR, UNKNOWN_403, NETWORK_ERROR, or INVALID_RESPONSE."""
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
        if any(w in msg for w in ("unknown", "unverified", "waf", "csrf", "generic")):
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
    def __init__(self, primary=None, fallback=None, spot_fallback=None, fallback_enabled=True, spot_fallback_enabled=None):
        self.primary = primary or FuturesMarket()
        self.fallback = fallback or BybitFuturesMarket(allow_host_fallback=True)
        if spot_fallback_enabled is None:
            spot_fallback_enabled = (primary is None and fallback is None) or (spot_fallback is not None)
        self.spot_fallback = spot_fallback or (SpotReferenceMarket() if spot_fallback_enabled else None)
        self.fallback_enabled = bool(fallback_enabled)
        self.spot_fallback_enabled = bool(spot_fallback_enabled and self.spot_fallback is not None)
        self.active = self.primary
        self.failover_reason = None
        self.last_error_classification = None
        self.futures_data_unavailable = False
        self.primary_http_status = None
        self.primary_error_class = None
        self.fallback_http_status = None
        self.fallback_error_class = None
        self.last_successful_futures_ms = None

    @property
    def futures_available(self) -> bool:
        """Section 2: True only when an authentic Perpetual Futures provider (Binance USD-M or Bybit Linear) is active and reachable."""
        return (
            not self.futures_data_unavailable
            and getattr(self.active, "instrument_kind", "") in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL")
        )

    @property
    def data_status(self) -> str:
        """Section 2: Return FUTURES_DATA_UNAVAILABLE whenever no valid Futures provider is available."""
        if not self.futures_available:
            return "FUTURES_DATA_UNAVAILABLE"
        return "OK"

    @staticmethod
    def _remap_time_args(provider, name, args):
        if name == "get" and args and isinstance(args[0], str) and args[0].endswith("/time"):
            return (getattr(provider, "time_path", args[0]),) + tuple(args[1:])
        return args

    def _invoke(self, name, *args, **kwargs):
        call_args = self._remap_time_args(self.active, name, args)
        method = getattr(self.active, name)
        try:
            res = method(*call_args, **kwargs)
            if getattr(self.active, "instrument_kind", "") in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL"):
                import time as _t_pr
                self.futures_data_unavailable = False
                self.last_successful_futures_ms = int(_t_pr.time() * 1000)
            else:
                self.futures_data_unavailable = True
            return res
        except MarketError as exc:
            err_class = classify_provider_error(exc)
            self.last_error_classification = err_class
            # Tier 1 -> Tier 2: Binance Futures regional restriction (HTTP 451) -> switch to Bybit Perpetual
            if self.active is self.primary:
                self.primary_http_status = getattr(exc, "http_status", None)
                self.primary_error_class = err_class
            elif self.active is self.fallback:
                self.fallback_http_status = getattr(exc, "http_status", None)
                self.fallback_error_class = err_class
            if self.active is self.primary and self.fallback_enabled and (exc.access_blocked or err_class == "REGIONAL_RESTRICTION"):
                self.active = self.fallback
                self.failover_reason = (
                    f"Binance Futures HTTP {exc.http_status or 451} ({err_class}); "
                    "switched to Bybit USDT perpetual public market data (BYBIT_LINEAR_PERPETUAL)"
                )
                fb_args = self._remap_time_args(self.active, name, args)
                try:
                    res = getattr(self.active, name)(*fb_args, **kwargs)
                    self.futures_data_unavailable = False
                    return res
                except MarketError as bybit_exc:
                    bybit_class = classify_provider_error(bybit_exc)
                    self.fallback_http_status = getattr(bybit_exc, "http_status", None)
                    self.fallback_error_class = bybit_class
                    self.last_error_classification = bybit_class
                    self.futures_data_unavailable = True
                    # BUG-30 & v6.0.4 Section 2: Tier 2 -> Tier 3 (REFERENCE ONLY) ONLY when Bybit is explicitly geo-blocked.
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
            # If already on Tier 2 (Bybit) and it gets geo-blocked on a subsequent call -> switch to Tier 3 (Spot Reference Only)
            if (
                self.active is self.fallback
                and self.spot_fallback_enabled
                and err_class == "REGIONAL_RESTRICTION"
                and getattr(exc, "http_status", None) in (403, 451)
            ):
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
        """P0-03: Detailed Futures provider health & failover diagnostics."""
        return {
            "active_provider": self.market_label,
            "instrument_kind": self.instrument_kind,
            "futures_available": self.futures_available,
            "data_status": self.data_status,
            "primary_http_status": self.primary_http_status,
            "primary_error_class": self.primary_error_class,
            "fallback_http_status": self.fallback_http_status,
            "fallback_error_class": self.fallback_error_class,
            "last_successful_futures_ms": self.last_successful_futures_ms,
            "provider_failover_reason": self.failover_reason,
        }

    def verify_futures_providers(self) -> dict:
        """P0-04: Independently probe Binance USD-M Futures (primary) and Bybit Linear (fallback)
        and report separated primary & fallback HTTP status and error classes.
        """
        out = {
            "primary": {"provider": self.primary.market_label, "instrument_kind": self.primary.instrument_kind, "ok": False, "http_status": None, "error_class": None, "error_detail": ""},
            "fallback": {"provider": self.fallback.market_label, "instrument_kind": self.fallback.instrument_kind, "ok": False, "http_status": None, "error_class": None, "error_detail": ""},
            "futures_available": False,
            "data_status": "FUTURES_DATA_UNAVAILABLE",
        }
        for role, prov in (("primary", self.primary), ("fallback", self.fallback)):
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
                else:
                    self.fallback_http_status = out[role]["http_status"]
                    self.fallback_error_class = cls
            except Exception as exc:
                out[role]["ok"] = False
                out[role]["error_class"] = "NETWORK_ERROR"
                out[role]["error_detail"] = str(exc)[:180]
        out["futures_available"] = bool(out["primary"]["ok"] or out["fallback"]["ok"])
        out["data_status"] = "OK" if out["futures_available"] else "FUTURES_DATA_UNAVAILABLE"
        return out


def validate_futures_contract_spec(contract, symbol: str = "") -> tuple[bool, list[str]]:
    """P0-4: Validate Futures perpetual contract specification (status, type, quote, tick, step, min_qty, min_notional)."""
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

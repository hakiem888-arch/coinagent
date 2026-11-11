"""Multi-tier market-provider failover:
1. Binance USD-M Futures (primary)
2. Bybit USDT Linear Perpetual (secondary failover on HTTP 451, with multi-host support)
3. Binance Spot Reference (`data-api.binance.vision`) (tertiary failover when both Futures & Bybit are geo-blocked on cloud/CI runners)
"""
from .market import FuturesMarket, MarketError
from .bybit_market import BybitFuturesMarket
from .spot_market import SpotReferenceMarket


class FuturesProviderRouter:
    def __init__(self, primary=None, fallback=None, spot_fallback=None, fallback_enabled=True, spot_fallback_enabled=None):
        self.primary = primary or FuturesMarket()
        self.fallback = fallback or BybitFuturesMarket(allow_host_fallback=True)
        # Enable tertiary Spot fallback automatically when using default providers,
        # while keeping two-argument unit tests (primary, fallback) deterministic.
        if spot_fallback_enabled is None:
            spot_fallback_enabled = (primary is None and fallback is None) or (spot_fallback is not None)
        self.spot_fallback = spot_fallback or (SpotReferenceMarket() if spot_fallback_enabled else None)
        self.fallback_enabled = bool(fallback_enabled)
        self.spot_fallback_enabled = bool(spot_fallback_enabled and self.spot_fallback is not None)
        self.active = self.primary
        self.failover_reason = None

    @staticmethod
    def _remap_time_args(provider, name, args):
        if name == "get" and args and isinstance(args[0], str) and args[0].endswith("/time"):
            return (getattr(provider, "time_path", args[0]),) + tuple(args[1:])
        return args

    def _invoke(self, name, *args, **kwargs):
        call_args = self._remap_time_args(self.active, name, args)
        method = getattr(self.active, name)
        try:
            return method(*call_args, **kwargs)
        except MarketError as exc:
            # Tier 1 -> Tier 2: Binance Futures blocked (HTTP 451) -> switch to Bybit Perpetual
            if self.active is self.primary and self.fallback_enabled and exc.access_blocked:
                self.active = self.fallback
                self.failover_reason = "Binance Futures HTTP 451; switched to Bybit USDT perpetual public market data"
                fb_args = self._remap_time_args(self.active, name, args)
                try:
                    return getattr(self.active, name)(*fb_args, **kwargs)
                except MarketError as bybit_exc:
                    # Tier 2 -> Tier 3: Bybit also geo-blocked (HTTP 403/451) -> switch to Binance Spot Reference
                    if self.spot_fallback_enabled and getattr(bybit_exc, "http_status", None) in (403, 451, None):
                        self.active = self.spot_fallback
                        self.failover_reason = (
                            f"Binance Futures HTTP 451 & Bybit HTTP {bybit_exc.http_status or 'blocked'}; "
                            "switched to Binance Spot Reference (data-api.binance.vision)"
                        )
                        spot_args = self._remap_time_args(self.active, name, args)
                        return getattr(self.active, name)(*spot_args, **kwargs)
                    raise
            # If already on Tier 2 (Bybit) and it gets geo-blocked on a subsequent call -> switch to Tier 3 (Spot)
            if self.active is self.fallback and self.spot_fallback_enabled and getattr(exc, "http_status", None) in (403, 451):
                self.active = self.spot_fallback
                self.failover_reason = (
                    f"Bybit HTTP {exc.http_status}; switched to Binance Spot Reference (data-api.binance.vision)"
                )
                spot_args = self._remap_time_args(self.active, name, args)
                return getattr(self.active, name)(*spot_args, **kwargs)
            raise

    def get(self, *args, **kwargs):
        return self._invoke("get", *args, **kwargs)

    def universe(self, *args, **kwargs):
        return self._invoke("universe", *args, **kwargs)

    def closed_candles(self, *args, **kwargs):
        return self._invoke("closed_candles", *args, **kwargs)

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

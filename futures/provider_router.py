"""Strict futures-provider failover: Binance primary, Bybit linear perpetual backup on HTTP 451 only."""
from .market import FuturesMarket, MarketError
from .bybit_market import BybitFuturesMarket

class FuturesProviderRouter:
    def __init__(self, primary=None, fallback=None, fallback_enabled=True):
        self.primary = primary or FuturesMarket()
        self.fallback = fallback or BybitFuturesMarket()
        self.fallback_enabled = bool(fallback_enabled)
        self.active = self.primary
        self.failover_reason = None

    def _invoke(self, name, *args, **kwargs):
        method = getattr(self.active, name)
        try:
            return method(*args, **kwargs)
        except MarketError as exc:
            if self.active is self.primary and self.fallback_enabled and exc.access_blocked:
                self.active = self.fallback
                self.failover_reason = "Binance Futures HTTP 451; switched to Bybit USDT perpetual public market data"
                return getattr(self.active, name)(*args, **kwargs)
            raise

    def get(self, *args, **kwargs): return self._invoke("get", *args, **kwargs)
    def universe(self, *args, **kwargs): return self._invoke("universe", *args, **kwargs)
    def closed_candles(self, *args, **kwargs): return self._invoke("closed_candles", *args, **kwargs)
    def load(self, *args, **kwargs): return self._invoke("load", *args, **kwargs)
    def load_modes(self, *args, **kwargs): return self._invoke("load_modes", *args, **kwargs)

    @property
    def time_path(self): return self.active.time_path
    @property
    def instrument_kind(self): return self.active.instrument_kind
    @property
    def market_label(self): return self.active.market_label
    @property
    def source_label(self): return getattr(self.active, "source_label", "Binance USD-M Futures public REST")

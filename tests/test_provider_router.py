import pytest
from futures.market import MarketError
from futures.provider_router import FuturesProviderRouter

class Primary:
    time_path="/fapi/v1/time"; instrument_kind="USD_M_PERPETUAL"; market_label="Binance Futures"
    def get(self,*a,**k): raise MarketError("blocked",http_status=451,endpoint="/fapi/v1/time")

class Backup:
    time_path="/v5/market/time"; instrument_kind="BYBIT_LINEAR_PERPETUAL"; market_label="Bybit USDT Perpetual"
    def get(self,*a,**k): return {"serverTime":123}

def test_router_switches_to_bybit_only_on_http_451_and_retries_same_call():
    router=FuturesProviderRouter(Primary(),Backup())
    assert router.get(router.time_path)=={"serverTime":123}
    assert router.market_label=="Bybit USDT Perpetual"
    assert router.instrument_kind=="BYBIT_LINEAR_PERPETUAL"
    assert "451" in router.failover_reason

def test_router_does_not_failover_on_other_errors():
    class Busy(Primary):
        def get(self,*a,**k): raise MarketError("rate limited",http_status=429)
    router=FuturesProviderRouter(Busy(),Backup())
    with pytest.raises(MarketError): router.get(router.time_path)
    assert router.market_label=="Binance Futures"

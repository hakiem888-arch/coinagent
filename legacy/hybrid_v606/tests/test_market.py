import pytest
from futures.market import FuturesMarket, MarketError
from tests.fixtures import setup, NOW


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self.data


class FakeBinance:
    def __init__(self):
        self.calls = []
        self.s, _ = setup()

    def get(self, url, params=None, **kwargs):
        assert url.startswith("https://fapi.binance.com/fapi/")
        self.calls.append(url)
        if url.endswith("/time"):
            data = {"serverTime": NOW}
        elif url.endswith("/exchangeInfo"):
            data = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "100000"},
                                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                                 {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                                 {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
        elif url.endswith("/klines") or url.endswith("/markPriceKlines"):
            interval = params["interval"]
            if interval == "1d":
                day = NOW//86400000*86400000
                data = [[day-86400000, "100", "110", "90", "105", "100", day-1, "1", 1, "1", "1", "0"]]
            else:
                d = self.s.bias if interval == "4h" else self.s.execution
                data = [[int(r.open_time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), int(r.close_time), "1", 1, "1", "1", "0"] for _, r in d.iterrows()]
                step = 14400000 if interval == "4h" else 900000
                opened = NOW//step*step
                data.append([opened, "113.9", "113.99", "113.8", "113.9", "1", opened+step-1, "1", 1, "1", "1", "0"])
        elif url.endswith("/fundingInfo"):
            data = []  # No record must not silently become an eight-hour interval.
        elif url.endswith("/depth"):
            data = {"bids": [["113.89", "100"]], "asks": [["113.91", "100"]], "T": NOW-1}
        elif url.endswith("/bookTicker"):
            data = {"bidPrice": "113.89", "askPrice": "113.91", "time": NOW-1}
        elif url.endswith("/premiumIndex"):
            data = {"markPrice": "113.9", "time": NOW-1, "lastFundingRate": "0.0001", "nextFundingTime": NOW+3600000}
        elif url.endswith("/price"):
            data = {"price": "113.9", "time": NOW-1}
        else:
            raise AssertionError(url)
        return Response(data)


def test_only_futures_closed_candles_and_contract_filters():
    fake = FakeBinance()
    snap = FuturesMarket(fake).load("BTCUSDT", "INTRADAY")
    assert len(snap.bias) >= 500 and len(snap.execution) >= 500
    assert snap.execution.close_time.max() < NOW
    assert snap.contract.tick == .01
    assert snap.previous_day_high == 110
    assert snap.funding_interval_hours is None
    assert snap.open_bar_high == 113.99
    assert all("/api/v3/" not in x for x in fake.calls)


def test_no_spot_fallback_for_451():
    class Blocked:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return Response({}, 451)
    session = Blocked()
    with pytest.raises(MarketError, match="451") as error:
        FuturesMarket(session).load("BTCUSDT", "INTRADAY")
    assert session.calls == 1
    assert error.value.access_blocked
    assert error.value.diagnostic() == {"http_status": 451, "endpoint": "/fapi/v1/time", "access_blocked": True}


def test_manual_retry_can_reach_same_futures_host_after_451():
    class Recovering:
        calls = 0
        def get(self, url, **kwargs):
            assert url == "https://fapi.binance.com/fapi/v1/time"
            self.calls += 1
            return Response({}, 451) if self.calls == 1 else Response({"serverTime": NOW})
    session = Recovering()
    market = FuturesMarket(session)
    with pytest.raises(MarketError):
        market.get("/fapi/v1/time")
    assert market.get("/fapi/v1/time")["serverTime"] == NOW
    assert session.calls == 2


def test_funding_endpoint_restriction_stops_snapshot():
    class FundingBlocked(FakeBinance):
        def get(self, url, params=None, **kwargs):
            if url.endswith('/fundingInfo'):
                return Response({}, 451)
            return super().get(url, params, **kwargs)
    session = FundingBlocked()
    with pytest.raises(MarketError, match="451"):
        FuturesMarket(session).load("BTCUSDT", "INTRADAY")
    assert not any(url.endswith('/premiumIndex') for url in session.calls)


def test_rate_limit_blocks_followup_calls():
    class Limited:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return Response({}, 429, {"Retry-After": "120"})
    session = Limited()
    market = FuturesMarket(session)
    for _ in range(2):
        with pytest.raises(MarketError):
            market.get("/fapi/v1/time")
    assert session.calls == 1


def test_reject_non_futures_path():
    with pytest.raises(MarketError):
        FuturesMarket().get("/api/v3/klines")


def test_metadata_and_closed_candles_cached_but_quotes_are_fresh():
    fake = FakeBinance()
    m = FuturesMarket(fake)
    first = m.load("BTCUSDT", "INTRADAY")
    count = len(fake.calls)
    # Caller mutation cannot contaminate cached candle data.
    first.execution.loc[0, "close"] = -999
    second = m.load("BTCUSDT", "INTRADAY")
    assert second.execution.loc[0, "close"] > 0
    assert fake.calls.count("https://fapi.binance.com/fapi/v1/exchangeInfo") == 1
    assert fake.calls.count("https://fapi.binance.com/fapi/v1/fundingInfo") == 1
    assert fake.calls.count("https://fapi.binance.com/fapi/v1/premiumIndex") == 2
    assert len(fake.calls)-count == 8 < count


def test_new_candle_boundary_invalidates_cache(monkeypatch):
    from tests.fixtures import candles
    import numpy as np
    now, calls = [NOW], []
    def raw(*args, **kwargs):
        calls.append(1)
        return candles(np.arange(1000)+100., "15m", now=now[0])
    m = FuturesMarket()
    monkeypatch.setattr(m, "candles", raw)
    old = m.closed_candles("BTCUSDT", "15m", now[0])
    m.closed_candles("BTCUSDT", "15m", now[0]+1000)
    assert len(calls) == 1
    now[0] += 900000
    new = m.closed_candles("BTCUSDT", "15m", now[0])
    assert len(calls) == 2 and new.close_time.iloc[-1] > old.close_time.iloc[-1]


def test_batch_three_modes_share_fresh_quotes_and_timeframes():
    from tests.fixtures import candles
    from futures.models import TIMEFRAMES, INTERVAL_MS
    import numpy as np
    class BatchBinance(FakeBinance):
        def get(self,url,params=None,**kwargs):
            if url.endswith('/klines') or url.endswith('/markPriceKlines'):
                self.calls.append(url)
                tf=params['interval']; step=INTERVAL_MS[tf]
                d=candles(np.linspace(90,114,700),tf)
                rows=[[int(r.open_time),str(r.open),str(r.high),str(r.low),str(r.close),str(r.volume),int(r.close_time),'1',1,'1','1','0']
                      for _,r in d.tail(params['limit']).iterrows()]
                opened=NOW//step*step
                rows.append([opened,'113.9','113.99','113.8','113.9','1',opened+step-1,'1',1,'1','1','0'])
                return Response(rows)
            return super().get(url,params,**kwargs)
    fake=BatchBinance(); result=FuturesMarket(fake).load_modes('BTCUSDT')
    assert set(result)==set(TIMEFRAMES)
    assert fake.calls.count('https://fapi.binance.com/fapi/v1/premiumIndex')==1
    assert fake.calls.count('https://fapi.binance.com/fapi/v1/depth')==1
    assert fake.calls.count('https://fapi.binance.com/fapi/v1/ticker/bookTicker')==1
    assert len({s.now_ms for s in result.values()})==1
    for mode,snap in result.items():
        for frame,tf in zip((snap.bias,snap.execution),TIMEFRAMES[mode]):
            assert len(frame)>=500
            assert frame.close_time.iloc[-1]==NOW//INTERVAL_MS[tf]*INTERVAL_MS[tf]-1

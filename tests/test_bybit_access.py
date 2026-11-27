from futures.bybit_market import BASE, FALLBACK_BASES, BybitFuturesMarket
from futures.market import MarketError


class Response:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {"retCode": 0, "timeNano": "1700000000000000000", "result": {}}

    def json(self):
        return self._payload


class Session:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responder(url)


def test_indonesia_default_and_only_documented_global_fallbacks():
    assert BASE == "https://api.bybit.id"
    assert FALLBACK_BASES == (
        "https://api.bybit.id",
        "https://api.bytick.com",
        "https://api.bybit.com",
    )
    assert all("bybit.nl" not in host and "bybit-tr.com" not in host for host in FALLBACK_BASES)


def test_rotates_to_official_global_endpoint_after_first_host_403():
    session = Session(lambda url: Response(403, "WAF denied") if "api.bybit.id" in url else Response(200))
    market = BybitFuturesMarket(session=session, allow_host_fallback=True)
    result = market._get("/v5/market/time")
    assert result["timeNano"] == "1700000000000000000"
    assert len(session.calls) == 2
    assert "api.bytick.com" in session.calls[1]


def test_generic_403_is_not_mislabeled_as_regional_and_lists_hosts():
    session = Session(lambda _url: Response(403, "Access denied"))
    market = BybitFuturesMarket(session=session, allow_host_fallback=True)
    try:
        market._get("/v5/market/instruments-info", {"category": "linear"})
        assert False, "expected MarketError"
    except MarketError as exc:
        assert exc.http_status == 403
        assert exc.error_category == "UNKNOWN_403"
        assert "api.bybit.id" in str(exc)
        assert "api.bytick.com" in str(exc)
        assert "api.bybit.com" in str(exc)
        assert "tidak otomatis berarti geo-block" in str(exc)


def test_explicit_geo_403_is_regional_restriction():
    session = Session(lambda _url: Response(403, "Access denied due to restricted country"))
    market = BybitFuturesMarket(session=session, allow_host_fallback=False)
    try:
        market._get("/v5/market/time")
        assert False, "expected MarketError"
    except MarketError as exc:
        assert exc.error_category == "REGIONAL_RESTRICTION"


def test_router_failover_to_bitget_when_binance_451_and_bybit_403():
    from futures.provider_router import FuturesProviderRouter

    class BlockedBinance:
        time_path = "/fapi/v1/time"
        market_label = "Binance USD-M Futures"
        instrument_kind = "USD_M_PERPETUAL"

        def universe(self):
            raise MarketError("Restricted", http_status=451, error_category="REGIONAL_RESTRICTION")

    class BlockedBybit:
        time_path = "/v5/market/time"
        market_label = "Bybit USDT Perpetual"
        instrument_kind = "BYBIT_LINEAR_PERPETUAL"

        def universe(self):
            raise MarketError("WAF denied", http_status=403, error_category="UNKNOWN_403")

    class HealthyBitget:
        time_path = "/api/v2/public/time"
        market_label = "Bitget USDT Perpetual"
        instrument_kind = "BITGET_USDT_PERPETUAL"

        def universe(self):
            return {"info": {"symbols": []}, "tickers": [], "now_ms": 1700000000000, "instrument_kind": self.instrument_kind}

    router = FuturesProviderRouter(
        primary=BlockedBinance(),
        fallback=BlockedBybit(),
        tertiary=HealthyBitget(),
        spot_fallback_enabled=False,
    )
    res = router.universe()
    assert res["instrument_kind"] == "BITGET_USDT_PERPETUAL"
    assert router.instrument_kind == "BITGET_USDT_PERPETUAL"
    assert router.futures_available is True
    assert "Bitget USDT perpetual" in (router.failover_reason or "")

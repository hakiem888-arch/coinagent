from futures.bybit_market import BybitFuturesMarket, BASE
from futures.market import MarketError

class Response:
    def __init__(self, status, text=''):
        self.status_code = status
        self.text = text
    def json(self):
        return {'retCode': 0, 'timeNano': '1700000000000000000', 'result': {}}

class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.response

def test_default_bybit_host_is_indonesia_regional_endpoint():
    assert BASE == 'https://api.bybit.id'
    market = BybitFuturesMarket(session=Session(Response(200)))
    market.get(market.time_path)
    assert market.session.calls[0].startswith('https://api.bybit.id/')

def test_403_reports_endpoint_and_regional_hint():
    market = BybitFuturesMarket(session=Session(Response(403)))
    try:
        market.get(market.time_path)
        assert False, 'expected MarketError'
    except MarketError as exc:
        assert exc.http_status == 403
        assert exc.endpoint == '/v5/market/time'
        assert 'regional' in str(exc).lower()

def test_403_access_too_frequent_sets_ten_minute_cooldown():
    market = BybitFuturesMarket(session=Session(Response(403, '403, access too frequent')))
    try:
        market.get(market.time_path)
        assert False, 'expected MarketError'
    except MarketError as exc:
        assert '10 menit' in str(exc)
        assert market.cooldown_until > 0

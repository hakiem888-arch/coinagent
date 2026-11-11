import pytest
import check_market_access
from futures.market import MarketError


def test_check_access_success_does_not_need_keys_or_ledger():
    class PublicMarket:
        calls = []
        def get(self, path):
            self.calls.append(path)
            if path.endswith('/time'):
                return {'serverTime': 123456}
            if path.endswith('/exchangeInfo'):
                return {'symbols': [{'symbol': 'BTCUSDT'}]}
            return [{'symbol': 'BTCUSDT'}]
    market = PublicMarket()
    report = check_market_access.diagnose(market)
    assert report['ok'] and len(report['checks']) == len(market.calls) == 4


def test_check_access_stops_on_451(monkeypatch, capsys):
    class Blocked:
        calls = 0
        def get(self, path):
            self.calls += 1
            raise MarketError('HTTP 451', http_status=451, endpoint=path)
    market = Blocked()
    monkeypatch.setattr(check_market_access, 'FuturesMarket', lambda: market)
    monkeypatch.setattr(check_market_access, 'BybitFuturesMarket', lambda: market)
    assert check_market_access.main() == 2
    assert market.calls == 2
    assert '451' in capsys.readouterr().out


@pytest.mark.parametrize('payload', [None, [], {'serverTime': None}, {'serverTime': 0}])
def test_check_access_rejects_invalid_response(payload):
    class Invalid:
        def get(self, path):
            return payload
    report = check_market_access.diagnose(Invalid())
    assert not report['ok'] and len(report['checks']) == 1
    assert report['checks'][0]['http_status'] == 200

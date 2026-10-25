"""Offline regressions for restoring the original Spot data source."""
from copy import deepcopy
import json
import pytest
from futures.market import MarketError
from futures.models import TIMEFRAMES, INTERVAL_MS
from futures.spot_market import SpotReferenceMarket, SPOT_BASE, paper_db_path
from futures.strategy import evaluate, fingerprint
from futures.screener import shortlist
from futures.paper import PaperLedger
from futures.telegram import eligible, format_alert, candle_key
from futures.groq_rotation import GroqRotation
from tests.fixtures import setup, NOW
from tests.test_market import Response
from tests.test_strategy import short_setup
from tests.test_paper import approved
from tests.test_market_scanner import Market, engine
from tests.test_screener import universe


def spot_snapshot(s):
    s.instrument_kind = "SPOT_REFERENCE"
    s.contract.contract_type = "SPOT"
    s.mark = s.funding_rate = s.next_funding_ms = s.funding_interval_hours = None
    s.open_mark_high = s.open_mark_low = None
    s.quote_times.pop("mark", None)
    s.quote_time_basis = "REQUEST_START_ESTIMATE"
    s.source = "SYNTHETIC SPOT TEST ONLY"
    return s


def spot_data(direction="LONG"):
    s, a = setup() if direction == "LONG" else short_setup()
    s = spot_snapshot(s)
    r = evaluate(s, a, paper=True)
    assert r.status == "VALID", r.reasons
    return dict(snapshot=s, result=r, ai=dict(ok=True, decision=direction, reasons=["Fixture"], cautions=[]),
                status="VALID", monotonic=100)


def spot_quote(data, price, delta):
    s = deepcopy(data["snapshot"])
    s.now_ms = NOW + delta
    s.last, s.bid, s.ask = price, price-.0001, price+.0001
    s.bids, s.asks = [[s.bid, 10000]], [[s.ask, 10000]]
    s.quote_times = {k: s.now_ms-1 for k in s.quote_keys}
    return s


def spot_universe(count=30):
    u = universe(count)
    u.update(instrument_kind="SPOT_REFERENCE", book_observed_ms=NOW)
    for s in u["info"]["symbols"]:
        s.pop("contractType")
        s.pop("marginAsset")
        s["isSpotTradingAllowed"] = True
    for b in u["books"]:
        b.pop("time")
    return u


class FakeSpot:
    def __init__(self):
        self.calls = []
        self.s, _ = setup()

    def get(self, url, params=None, **kwargs):
        assert url.startswith(SPOT_BASE + "/api/v3/")
        assert kwargs["allow_redirects"] is False
        path = url[len(SPOT_BASE):]
        self.calls.append((path, params))
        if path.endswith("/time"):
            data = {"serverTime": NOW}
        elif path.endswith("/exchangeInfo"):
            data = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT",
                "baseAsset": "BTC", "isSpotTradingAllowed": True,
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "100000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "9000"},
                {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "1000000"}]}]}
        elif path.endswith("/klines"):
            tf = params["interval"]
            step = INTERVAL_MS[tf]
            frame = self.s.bias.copy() if tf in ("1h", "4h", "1d") else self.s.execution.copy()
            end = NOW // step * step
            frame["open_time"] = list(range(end-len(frame)*step, end, step))
            frame["close_time"] = frame.open_time + step - 1
            data = [[int(r.open_time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume),
                     int(r.close_time), "1", 1, "1", "1", "0"] for r in frame.itertuples()]
            data.append([end, "113.9", "113.99", "113.8", "113.9", "1", end+step-1, "1", 1, "1", "1", "0"])
            data = data[-params["limit"]:]
        elif path.endswith("/depth"):
            data = {"lastUpdateId": 25, "bids": [["113.89", "100"]], "asks": [["113.91", "100"]]}
        elif path.endswith("/bookTicker"):
            data = {"symbol": "BTCUSDT", "bidPrice": "113.89", "askPrice": "113.91"}
            if not params:
                data = [data]
        elif path.endswith("/24hr"):
            data = [dict(spot_universe(1)["tickers"][0], symbol="BTCUSDT", lastPrice="113.9", highPrice="118", lowPrice="110")]
        elif path.endswith("/price"):
            data = {"symbol": "BTCUSDT", "price": "113.9"}
        else:
            raise AssertionError(path)
        return Response(data)


def test_adapter_uses_spot_only_and_keeps_unavailable_fields_null(monkeypatch):
    monkeypatch.setattr("futures.spot_market.time.monotonic", lambda: 100)
    fake = FakeSpot()
    market = SpotReferenceMarket(fake)
    snapshots = market.load_modes("BTCUSDT")
    assert set(snapshots) == set(TIMEFRAMES)
    for s in snapshots.values():
        assert len(s.bias) >= 500 and len(s.execution) >= 500
        assert s.mark is s.funding_rate is s.next_funding_ms is s.open_mark_high is None
        assert s.reference_price == s.last
        assert s.contract.contract_type == "SPOT"
        assert s.contract.min_notional == 5 and s.contract.market_step == .001
        assert s.contract.market_min_qty == .001 and s.contract.market_max_qty == 9000
        assert s.quote_times == {k: NOW for k in ("last", "book", "depth")}
        assert s.quote_time_basis == "REQUEST_START_ESTIMATE"
    assert sum(p == "/api/v3/depth" for p, _ in fake.calls) == 1
    assert not any("fapi" in p or "funding" in p or "premium" in p for p, _ in fake.calls)
    u = market.universe()
    assert "time" not in u["books"][0] and u["book_observed_ms"] == NOW
    assert shortlist(u)["rows"][0]["symbol"] == "BTCUSDT"


def test_quote_freshness_does_not_hide_slow_requests(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("futures.spot_market.time.monotonic", lambda: clock[0])
    fake = FakeSpot()
    original = fake.get
    def slow(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/depth"):
            clock[0] += 31
        if url.endswith("/time"):
            response.data["serverTime"] = NOW + int((clock[0]-100)*1000)
        return response
    fake.get = slow
    s = SpotReferenceMarket(fake).load("BTCUSDT", "INTRADAY")
    assert s.now_ms - s.quote_times["depth"] == 31000
    _, a = setup()
    r = evaluate(s, a, paper=True)
    assert r.status != "VALID" and "Usia snapshot" in r.reasons


@pytest.mark.parametrize("path", ["/fapi/v1/time", "/api/v3/account", "/api/v3/order"])
def test_adapter_rejects_futures_and_private_endpoints(path):
    fake = FakeSpot()
    with pytest.raises(MarketError):
        SpotReferenceMarket(fake).get(path)
    assert not fake.calls


@pytest.mark.parametrize("status", [451, 429])
def test_access_failure_is_not_replaced_with_fake_data_or_other_host(status):
    class Blocked:
        calls = []
        def get(self, url, **kwargs):
            self.calls.append(url)
            return Response({}, status, {"Retry-After": "60"})
    fake = Blocked()
    with pytest.raises(MarketError) as error:
        SpotReferenceMarket(fake).load("BTCUSDT", "INTRADAY")
    assert error.value.http_status == status
    assert fake.calls == [SPOT_BASE + "/api/v3/time"]


@pytest.mark.parametrize("mutation", ["stale_book", "inactive", "spot_disabled", "stable", "stale_ticker"])
def test_spot_universe_filters_without_futures_metadata(mutation):
    u = spot_universe(1)
    assert shortlist(u)["universe_count"] == 1 and len(shortlist(u)["rows"]) == 1
    if mutation == "stale_book": u["book_observed_ms"] -= 60001
    if mutation == "inactive": u["info"]["symbols"][0]["status"] = "BREAK"
    if mutation == "spot_disabled": u["info"]["symbols"][0]["isSpotTradingAllowed"] = False
    if mutation == "stable": u["info"]["symbols"][0]["baseAsset"] = "USDC"
    if mutation == "stale_ticker": u["tickers"][0]["closeTime"] -= 60001
    assert shortlist(u)["rows"] == []


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_spot_simulation_fill_stop_and_pnl_without_mark_or_funding(direction, tmp_path):
    d = spot_data(direction)
    p = d["result"].plan
    assert eligible(d, lambda: 100)
    assert "SPOT REFERENCE" in format_alert(d) and "Last spot" in format_alert(d)
    assert candle_key(p).startswith("PAPER:SPOT:")
    assert fingerprint(p) != fingerprint(dict(p, instrument_kind="USD_M_PERPETUAL"))
    ledger = PaperLedger(tmp_path / "spot.db")
    assert ledger.queue(d, lambda: 100)[0]
    ledger.advance(spot_quote(d, (p["entry"]+p["entry_trigger"])/2, 1000))
    assert ledger.trades()[0]["status"] == "OPEN"
    sign = 1 if direction == "LONG" else -1
    exit_quote = spot_quote(d, p["sl"]-sign*.2, 2000)
    ledger.advance(exit_quote)
    trade = ledger.trades()[0]
    assert trade["status"] == "CLOSED" and trade["note"] == "STOP_LOSS"
    fill = (exit_quote.bid if sign == 1 else exit_quote.ask) - sign*p["slippage_per_unit"]
    expected = -p["qty"]*(p["entry"]*p["fee"]+p["funding_per_unit"])
    expected += p["qty"]*(sign*(fill-p["entry"])-fill*p["fee"])
    assert trade["realized_pnl"] == pytest.approx(expected)
    assert ledger.summary(exit_quote.now_ms)["equity"] == pytest.approx(1000+expected)


def test_spot_exceptions_do_not_relax_live_or_other_gates():
    d = spot_data()
    _, a = setup()
    assert evaluate(d["snapshot"], a).status == "NO_TRADE"
    check = next(c for c in d["result"].checks if c["Kriteria"] == "Spread")
    check["Status"] = "SIMULASI"
    assert not eligible(d, lambda: 100)
    s = deepcopy(d["snapshot"])
    s.quote_times["book"] -= 31000
    assert evaluate(s, a, paper=True).status != "VALID"
    a.news_source = ""
    assert evaluate(d["snapshot"], a, paper=True).status == "WAIT"


def test_cannot_monitor_old_perpetual_trade_using_spot_quotes(tmp_path):
    ledger = PaperLedger(tmp_path / "old.db")
    old = approved()
    assert ledger.queue(old, lambda: 100)[0]
    before = ledger.export_json()
    p = old["result"].plan
    with pytest.raises(ValueError, match="Sumber jurnal berbeda"):
        ledger.advance(spot_quote(spot_data(), p["entry"], 1000))
    assert ledger.export_json() == before


def test_full_spot_scan_keeps_two_candidates_three_modes_and_paper_alerts(tmp_path):
    class SpotMarket(Market):
        instrument_kind = "SPOT_REFERENCE"
        time_path = "/api/v3/time"
        def get(self, path, params=None):
            assert path == self.time_path
            return {"serverTime": NOW}
        def universe(self):
            u = super().universe()
            u.update(instrument_kind=self.instrument_kind, book_observed_ms=NOW)
            for s in u["info"]["symbols"]:
                s.pop("contractType")
                s["isSpotTradingAllowed"] = True
            for b in u["books"]:
                b.pop("time")
            return u
        def snapshot(self, symbol, mode):
            return spot_snapshot(super().snapshot(symbol, mode))
    e, c = engine(tmp_path)
    e.market = SpotMarket()
    report = e.run(c)
    assert len(report["shortlist"]) == 10 and len(report["selected"]) == 2
    assert len(e.ledger.active()) == len(e.alerts.calls) == 2, report["errors"]
    assert all(len(d["result"].meta["mode_comparison"]) == 3 for d in report["analyses"])
    assert all("SPOT REFERENCE" in text for text in e.alerts.calls)
    e.run(c, manual=True)
    assert len(e.alerts.calls) == 2


def test_ai_receives_explicit_spot_exceptions():
    from tests.test_groq_rotation import Session
    session = Session()
    pool = GroqRotation(["test-key"], session)
    assert pool.review(spot_data()["result"])["ok"]
    payload = session.calls[0][1]["json"]
    assert "ADAPTASI SCANNER AWAL" in payload["messages"][0]["content"]
    assert "SPOT_REFERENCE" in payload["messages"][1]["content"]
    assert "NaN" not in json.dumps(payload)


def test_default_and_custom_db_paths_preserve_old_ledger(monkeypatch, tmp_path):
    monkeypatch.delenv("SPOT_HISTORY_DB", raising=False)
    monkeypatch.delenv("FUTURES_HISTORY_DB", raising=False)
    assert paper_db_path(tmp_path) == tmp_path/"data"/"spot_paper_history.sqlite3"
    old = tmp_path/"custom.db"
    old.write_text("old ledger placeholder")
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(old))
    assert paper_db_path(tmp_path) == tmp_path/"custom_spot_reference.db"
    assert old.read_text() == "old ledger placeholder"
    monkeypatch.setenv("SPOT_HISTORY_DB", str(tmp_path/"chosen.db"))
    assert paper_db_path(tmp_path) == tmp_path/"chosen.db"

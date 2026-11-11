import pytest
from futures.screener import shortlist, select_candidates
from tests.fixtures import NOW, candles
import numpy as np


def universe(count=30):
    info, tickers, books = [], [], []
    for i in range(count):
        symbol = f"COIN{i}USDT"
        info.append({"symbol":symbol, "status":"TRADING", "contractType":"PERPETUAL", "quoteAsset":"USDT", "marginAsset":"USDT"})
        tickers.append({"symbol":symbol, "lastPrice":"100", "highPrice":"105", "lowPrice":"95", "priceChangePercent":"2",
                        "quoteVolume":str(20_000_000+i*1_000_000), "count":5000, "closeTime":NOW})
        books.append({"symbol":symbol, "bidPrice":"99.99", "askPrice":"100.01", "time":NOW})
    return {"info":{"symbols":info}, "tickers":tickers, "books":books, "now_ms":NOW}


def test_whole_universe_is_inspected_and_shortlist_bounded():
    u = universe(300)
    out = shortlist(u)
    assert out["universe_count"] == out["eligible_count"] == 300
    assert len(out["rows"]) == 10
    assert out["rows"][0]["symbol"] == "COIN299USDT"


@pytest.mark.parametrize("bad", ["inactive", "spot", "usdc", "stale", "future", "wide_spread", "low_volume", "no_trades", "nan", "flat", "extreme"])
def test_unusable_markets_removed(bad):
    u=universe(1); info=u["info"]["symbols"][0]; t=u["tickers"][0]; b=u["books"][0]
    if bad=="inactive": info["status"]="SETTLING"
    if bad=="spot": info["contractType"]="SPOT"
    if bad=="usdc": info["quoteAsset"]="USDC"
    if bad=="stale": b["time"]=NOW-60001
    if bad=="future": b["time"]=NOW+1
    if bad=="wide_spread": b["askPrice"]="102"
    if bad=="low_volume": t["quoteVolume"]="1000"
    if bad=="no_trades": t["count"]=1
    if bad=="nan": t["lastPrice"]="NaN"
    if bad=="flat": t["highPrice"]=t["lowPrice"]="100"
    if bad=="extreme": t["highPrice"]="200"
    assert shortlist(u)["rows"]==[]


@pytest.mark.parametrize("count", [1,2])
def test_stage_two_ranks_ten_and_selects_at_most_two(count):
    rows=shortlist(universe())["rows"]
    class Market:
        calls=[]
        def closed_candles(self,symbol,interval,now_ms,limit):
            self.calls.append((symbol,interval,limit))
            return candles(100+np.arange(100)*.1,"15m")
    m=Market()
    selected,ranked,errors=select_candidates(rows,m,NOW,count)
    assert len(selected)==count and len(ranked)==10 and not errors
    assert all(interval=="15m" and limit==120 for _,interval,limit in m.calls)


def test_multi_stage_m15_scoring_and_categories():
    from futures.screener import score_category, evaluate_m15_candidate
    assert score_category(50) == "REJECT"
    assert score_category(60) == "WATCH"
    assert score_category(75) == "PRIORITY WATCH"
    assert score_category(82) == "HIGH PRIORITY"
    assert score_category(88) == "ENTRY CANDIDATE"
    assert score_category(92) == "HIGH CONFIDENCE"
    assert score_category(95) == "VERY HIGH CONFIDENCE"
    assert score_category(98) == "EXCEPTIONAL SETUP"
    d = candles(100 + np.arange(100) * .1, "15m")
    res = evaluate_m15_candidate(d, {"spread_bps": 2.0})
    assert 0 <= res["m15_score"] <= 100
    assert res["candidate_direction"] in ("LONG", "SHORT")
    assert "category" in res

from copy import deepcopy
import pytest
from futures.scanner import ScanSchedule, ScanEngine, ScanBusy, SCAN_INTERVALS
from tests.fixtures import setup, verified


@pytest.mark.parametrize("interval", SCAN_INTERVALS)
def test_schedule_immediate_start_tick_stop_and_no_overlap(interval):
    s = ScanSchedule()
    assert not s.claim(0, False, interval, "a")
    assert s.claim(1, True, interval, "a")
    assert not s.claim(2, True, interval, "a", manual=True)
    s.finish(3, interval)
    assert not s.claim(interval, True, interval, "a")
    assert s.claim(interval+1, True, interval, "a")
    s.finish(interval+2, interval)
    assert not s.claim(1000, False, interval, "a")
    assert s.claim(1001, False, interval, "a", manual=True)


def test_slow_scan_skips_backlog_and_context_change_scans_immediately():
    s = ScanSchedule()
    assert s.claim(0, True, 30, "BTC")
    s.finish(100, 30)
    assert not s.claim(101, True, 30, "BTC")
    assert s.claim(130, True, 30, "BTC")
    s.finish(132, 30)
    assert s.claim(133, True, 30, "ETH")


def test_bad_interval_rejected():
    with pytest.raises(ValueError):
        ScanSchedule().claim(0, True, 1, "x")


class Market:
    def __init__(self, snap, crossed=False):
        self.snap, self.crossed, self.calls = snap, crossed, 0

    def load(self, *args):
        self.calls += 1
        s = deepcopy(self.snap)
        if self.crossed and self.calls >= 2:
            s.open_bar_high = 120
        return s


class Pool:
    def __init__(self, decision="LONG"):
        self.decision, self.calls = decision, []

    def review(self, result, model):
        self.calls.append(result)
        return {"ok": True, "decision": self.decision, "reasons": ["Fixture"], "cautions": []}


@pytest.mark.parametrize("decision", ["LONG", "WAIT", "SHORT", "NO_TRADE"])
def test_ai_review_and_final_price_recheck(decision):
    snap, account = setup()
    verified(snap, account)
    m, p = Market(snap), Pool(decision)
    data = ScanEngine().run(m, snap.symbol, snap.mode, account, p, "test")
    assert len(p.calls) == 1
    assert data["status"] == {"LONG": "VALID", "WAIT": "WAIT", "SHORT": "WAIT", "NO_TRADE": "NO_TRADE"}[decision]
    assert m.calls == (2 if decision == "LONG" else 1)


@pytest.mark.parametrize("incomplete", [True, False])
def test_wait_and_no_trade_still_reviewed_by_ai_but_never_promoted(incomplete):
    snap, account = setup()
    if not incomplete:
        snap.execution.loc[snap.execution.index[-1], "volume"] = 0
    m, p = Market(snap), Pool()
    data = ScanEngine().run(m, snap.symbol, snap.mode, account, p, "test")
    assert len(p.calls) == 1 and m.calls == 1
    assert data["status"] in ("WAIT", "NO_TRADE")


def test_trigger_crossed_during_ai_is_wait_and_lock_released_after_error():
    snap, account = setup()
    verified(snap, account)
    engine = ScanEngine()
    data = engine.run(Market(snap, crossed=True), snap.symbol, snap.mode, account, Pool(), "test")
    assert data["status"] == "WAIT"
    class Broken:
        def load(self, *args):
            raise ValueError("fixture error")
    with pytest.raises(ValueError):
        engine.run(Broken(), snap.symbol, snap.mode, account, Pool(), "test")
    assert engine.lock.acquire(blocking=False)
    with pytest.raises(ScanBusy):
        engine.run(Market(snap), snap.symbol, snap.mode, account, Pool(), "test")
    engine.lock.release()

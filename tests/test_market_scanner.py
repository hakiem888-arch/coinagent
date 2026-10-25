from copy import deepcopy
from dataclasses import replace
import sqlite3
import numpy as np
import pytest
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.models import TIMEFRAMES, INTERVAL_MS
from futures.market import MarketError
from futures.process_lock import ProcessLock
from futures.scanner import ScanBusy
from futures.telegram import eligible, format_alert
from tests.fixtures import setup, NOW, candles
from tests.test_strategy import short_setup
from tests.test_screener import universe


class Market:
    def __init__(self,crossed=False):
        self.crossed,self.deep,self.fresh=False,[],[]
        self.crossed=crossed
    def get(self,*args,**kwargs): return {'serverTime':NOW}
    def universe(self):
        u=universe(120)
        for i,symbol in [(119,'BTCUSDT'),(118,'ETHUSDT')]:
            for name in ('tickers','books'): u[name][i]['symbol']=symbol
            u['info']['symbols'][i]['symbol']=symbol
        return u
    def closed_candles(self,*args,**kwargs): return candles(100+np.arange(100)*.1,'1h')
    def snapshot(self,symbol,mode):
        s,_=short_setup() if symbol=='ETHUSDT' else setup()
        s.symbol,s.mode=symbol,mode
        for name,tf in zip(('bias','execution'),TIMEFRAMES[mode]):
            d=getattr(s,name); step=INTERVAL_MS[tf]; end=NOW//step*step
            d['open_time']=list(range(end-len(d)*step,end,step)); d['close_time']=d.open_time+step-1
        return s
    def load_modes(self,symbol):
        self.deep.append(symbol)
        return {m:self.snapshot(symbol,m) for m in TIMEFRAMES}
    def load(self,symbol,mode):
        self.fresh.append((symbol,mode)); s=self.snapshot(symbol,mode)
        if self.crossed:
            if symbol=='ETHUSDT': s.open_bar_low=90
            else: s.open_bar_high=130
        return s


class Calendar:
    def __init__(self,ok=True): self.ok=ok
    def window(self,now):
        return {'ok':self.ok,'reason':'Fixture calendar' if self.ok else 'Calendar unavailable',
                'checked_ms':now,'from_ms':now-86400000,'until_ms':now+86400000,'source':'SYNTHETIC CALENDAR'}


class Pool:
    def __init__(self,decision=None): self.calls=[]; self.decision=decision
    def redact(self,text): return text
    def review(self,result,model):
        self.calls.append(result)
        return {'ok':True,'decision':self.decision or result.direction or 'WAIT','reasons':['Fixture review'],'cautions':[]}


class Alerts:
    def __init__(self,ledger): self.calls=[]; self.ledger=ledger
    def send(self,data):
        assert eligible(data,lambda:100)
        assert any(t['plan']['id']==data['result'].plan['id'] for t in self.ledger.trades())
        self.calls.append(format_alert(data))
        return 'Alert Telegram terkirim.'


def engine(tmp_path,decision=None,crossed=False,news=True):
    ledger=PaperLedger(tmp_path/'paper.db'); m=Market(crossed); p=Pool(decision); a=Alerts(ledger)
    e=MarketScanner(m,p,Calendar(news),ledger,a,clock=lambda:100)
    c=ScannerConfig(fee=.0004,slippage_bps=.1,funding=.00001)
    return e,c


def test_end_to_end_all_market_ten_two_three_modes_journal_then_alert(tmp_path):
    e,c=engine(tmp_path)
    report=e.run(c)
    assert report['universe_count']==120 and len(report['shortlist'])==10
    assert len(report['selected'])==len(report['analyses'])==len(e.pool.calls)==2
    assert len(e.market.deep)==2
    assert all(len(r.meta['mode_comparison'])==3 for r in e.pool.calls)
    assert len(e.ledger.active())==len(e.alerts.calls)==2, report['errors']
    assert all(t['status']=='PENDING' for t in e.ledger.active())
    assert any('PAPER | BUY / LONG' in x for x in e.alerts.calls)
    assert any('PAPER | SELL / SHORT' in x for x in e.alerts.calls)
    # A rerun or another cycle cannot duplicate the two admitted signals.
    e.run(c)
    e.run(c,manual=True)
    assert len(e.alerts.calls)==2 and len(e.ledger.trades())==2


@pytest.mark.parametrize('decision',['WAIT','NO_TRADE'])
def test_ai_veto_has_no_alert_or_paper_trade(tmp_path,decision):
    e,c=engine(tmp_path,decision)
    r=e.run(c)
    assert len(r['analyses'])==2
    assert not e.ledger.trades() and not e.alerts.calls


def test_post_ai_trigger_crossing_has_no_alert_or_paper_trade(tmp_path):
    e,c=engine(tmp_path,crossed=True)
    r=e.run(c)
    assert all(d['status']=='WAIT' for d in r['analyses'])
    assert not e.ledger.trades() and not e.alerts.calls


def test_news_missing_stops_deep_stage_but_screens_universe(tmp_path):
    e,c=engine(tmp_path,news=False)
    r=e.run(c)
    assert r['universe_count']==120 and len(r['shortlist'])==10
    assert not e.pool.calls and not e.market.deep and not e.ledger.trades()


def test_only_one_candidate_and_no_telegram_option(tmp_path):
    e,c=engine(tmp_path)
    r=e.run(replace(c,candidates=1,send_alerts=False))
    assert len(r['selected'])==len(e.ledger.trades())==1
    assert not e.alerts.calls


def test_exchange_error_keeps_wait_and_releases_lock(tmp_path,monkeypatch):
    e,c=engine(tmp_path)
    def failed(*args,**kwargs): raise MarketError('HTTP 451')
    monkeypatch.setattr(e.market,'universe',failed)
    r=e.run(c)
    assert any('451' in s for s in r['errors'])
    assert e.lock.acquire(blocking=False)
    e.lock.release()


def test_first_request_451_records_attempt_and_manual_retry_recovers(tmp_path, monkeypatch):
    e, c = engine(tmp_path, news=False)
    calls = []
    def blocked(*args, **kwargs):
        calls.append(1)
        raise MarketError('HTTP 451', http_status=451, endpoint='/fapi/v1/time')
    monkeypatch.setattr(e.market, 'get', blocked)
    first = e.run(c)
    assert first['started_at_ms'] > 0 and first['completed_at_ms'] >= first['started_at_ms']
    assert first['scan_status'] == 'FAILED' and first['now_ms'] == 0
    assert first['universe_loaded'] is False
    assert first['market_error']['access_blocked']
    assert e.ledger.last_scan_report()['scan_id'] == first['scan_id']
    # Even after the normal interval, automatic ticks do not repeat blocked HTTP.
    e.clock = lambda: 10000
    assert e.run(c) is first and len(calls) == 1
    second = e.run(c, manual=True)
    assert len(calls) == 2 and second['scan_id'] != first['scan_id']
    assert second['trigger'] == 'MANUAL'
    assert not e.pool.calls and not e.ledger.trades() and not e.alerts.calls
    monkeypatch.setattr(e.market, 'get', lambda *_args, **_kwargs: {'serverTime': NOW})
    recovered = e.run(c, manual=True)
    assert recovered['market_error'] == {} and recovered['universe_loaded']
    assert recovered['scan_status'] == 'COMPLETED_WITH_NOTES'
    # After recovery, the next scheduled scan can run normally.
    monkeypatch.setattr('futures.market_scanner.time.time', lambda: recovered['started_at_ms'] / 1000 + 200)
    e.clock = lambda: 10200
    assert e.run(c)['scan_id'] != recovered['scan_id']


@pytest.mark.parametrize('stage', ['screening', 'analysis', 'monitoring'])
def test_451_during_later_stages_stops_cycle_without_new_signals(tmp_path, monkeypatch, stage):
    e, c = engine(tmp_path)
    calls = []
    def blocked(*args, **kwargs):
        calls.append(1)
        raise MarketError('HTTP 451', http_status=451, endpoint='/fapi/v1/klines')
    if stage == 'screening':
        monkeypatch.setattr(e.market, 'closed_candles', blocked)
    elif stage == 'analysis':
        monkeypatch.setattr(e.market, 'load_modes', blocked)
    else:
        # Create an existing virtual position with fully mocked transports.
        e.run(c)
        monkeypatch.setattr(e.market, 'load', blocked)
    trades_before, alerts_before = len(e.ledger.trades()), len(e.alerts.calls)
    result = e.run(c, manual=True)
    assert result['scan_status'] == 'FAILED' and result['market_error']['access_blocked']
    assert len(calls) == 1
    assert len(e.ledger.trades()) == trades_before and len(e.alerts.calls) == alerts_before


def test_manual_scan_busy_returns_explicit_notice_without_changing_last(tmp_path):
    e, c = engine(tmp_path, news=False)
    first = e.run(c)
    e.lock.acquire()
    try:
        second = e.run(c, manual=True)
        assert 'sedang berjalan' in second['scan_notice']
        assert second['scan_id'] == first['scan_id']
        assert 'scan_notice' not in first
    finally:
        e.lock.release()
    process = ProcessLock(e.ledger.db)
    process.acquire()
    try:
        report = e.run(c, manual=True)
        assert 'Scanner/worker' in report['scan_notice']
    finally:
        process.release()


def test_journal_failure_also_releases_both_locks(tmp_path,monkeypatch):
    e,c=engine(tmp_path,news=False)
    def failed(*args): raise sqlite3.OperationalError('fixture')
    monkeypatch.setattr(e.ledger,'save_scan_report',failed)
    with pytest.raises(sqlite3.Error): e.run(c)
    assert e.lock.acquire(blocking=False); e.lock.release()
    lock=ProcessLock(e.ledger.db); lock.acquire(); lock.release()


def test_process_lock_prevents_worker_overlap(tmp_path):
    first=ProcessLock(tmp_path/'paper.db'); second=ProcessLock(tmp_path/'paper.db')
    first.acquire()
    try:
        with pytest.raises(ScanBusy): second.acquire()
    finally: first.release()
    second.acquire(); second.release()


def test_shared_database_enforces_interval_across_engine_instances(tmp_path):
    first,c=engine(tmp_path,news=False); first.run(c)
    other,_=engine(tmp_path,news=False)
    report=other.run(c)
    assert report['universe_count']==120 and not other.market.deep
    assert other.last is None  # Returned persisted summary without another scan.


def test_telegram_retry_uses_pending_journal_without_duplicate_trade(tmp_path):
    e,c=engine(tmp_path); c=replace(c,candidates=1)
    original=e.alerts.send
    def send(data):
        original(data)
        return 'Telegram rate limit; mengikuti retry_after' if len(e.alerts.calls)==1 else 'Alert Telegram terkirim.'
    e.alerts.send=send
    e.run(c)
    assert len(e.ledger.trades())==len(e.alerts.calls)==1
    e.pool.decision='WAIT'
    e.run(c,manual=True)
    assert len(e.ledger.trades())==1 and len(e.alerts.calls)==2
    assert 'terkirim' in e.ledger.trades()[0]['telegram']


def test_slow_first_candidate_does_not_starve_second(tmp_path):
    e,c=engine(tmp_path,decision='WAIT'); now=[100.]
    e.clock=lambda:now[0]
    original=e.market.load_modes
    def slow(symbol):
        now[0]+=61
        return original(symbol)
    e.market.load_modes=slow
    e.run(c)
    assert e.market.deep==['BTCUSDT'] and e.next_symbol=='ETHUSDT'
    e.run(c,manual=True)
    assert e.market.deep==['BTCUSDT','ETHUSDT']

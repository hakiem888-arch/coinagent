from copy import deepcopy
import json
import pytest
from futures.paper import PaperLedger
from futures.strategy import evaluate
from futures.telegram import eligible
from tests.fixtures import setup, NOW
from tests.test_strategy import short_setup


def approved(direction="LONG",symbol="BTCUSDT"):
    s,a=setup() if direction=="LONG" else short_setup()
    s.symbol=symbol
    r=evaluate(s,a,paper=True)
    assert r.status=="VALID", r.reasons
    return {"snapshot":s,"result":r,"ai":{"ok":True,"decision":direction,"reasons":["Fixture"],"cautions":[]},
            "status":"VALID","monotonic":100}


def quote(data,price,delta=1000,mark=None):
    s=deepcopy(data['snapshot']); s.now_ms=NOW+delta
    s.last=price; s.mark=price if mark is None else mark
    s.bid=price-.0001; s.ask=price+.0001
    s.bids=[[s.bid,10000]]; s.asks=[[s.ask,10000]]
    s.quote_times={k:s.now_ms-1 for k in ('last','mark','book','depth')}
    return s


def enter(ledger,data,delta=1000):
    p=data['result'].plan
    ledger.advance(quote(data,(p['entry_trigger']+p['entry'])/2,delta))
    t=ledger.trades()[0]
    assert t['status']=='OPEN'
    return t


def test_paper_adaptation_is_explicit_and_live_still_requires_liquidation():
    s,a=setup()
    assert evaluate(s,a).status=='WAIT'
    d=approved(); r=d['result']
    assert r.meta['scope']=='PAPER' and r.plan['liquidation'] is None
    assert any(c['Status']=='SIMULASI' for c in r.checks)
    assert eligible(d,lambda:100)
    a.news_source=''
    assert evaluate(s,a,paper=True).status=='WAIT'
    r.meta.pop('scope')
    assert not eligible(d,lambda:100)


def test_stale_dashboard_cannot_rewind_risk_day_or_reset_drawdown(tmp_path):
    ledger = PaperLedger(tmp_path / 'paper.db')
    old = ledger.summary(NOW - 86400000)
    current = ledger.summary(NOW)
    assert old['day'] < current['day']
    d = approved()
    assert ledger.queue(d, lambda:100)[0]
    enter(ledger, d)
    ledger.advance(quote(d, d['result'].plan['sl'] - .2, 2000))
    before = ledger.summary(NOW + 2000)
    assert before['equity'] < before['day_start'] and before['losses'] == 1
    stale = ledger.summary(NOW - 86400000)
    after = ledger.summary(NOW + 2000)
    assert stale['day'] == after['day'] == current['day']
    assert stale['losses'] == after['losses'] == 1
    assert stale['day_start'] == after['day_start'] == current['day_start']


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_pending_fill_fee_and_gap_aware_stop(direction,tmp_path):
    d=approved(direction); ledger=PaperLedger(tmp_path/'paper.db'); p=d['result'].plan
    assert ledger.queue(d,lambda:100)[0]
    assert ledger.trades()[0]['status']=='PENDING'
    assert not ledger.queue(d,lambda:100)[0]
    t=enter(ledger,d)
    cost=p['qty']*(p['entry']*p['fee']+p['funding_per_unit'])
    assert t['realized_pnl']==pytest.approx(-cost)
    sign=1 if direction=='LONG' else -1
    price=p['sl']-sign*.2
    s=quote(d,price,2000)
    ledger.advance(s)
    closed=ledger.trades()[0]
    assert closed['status']=='CLOSED' and closed['remaining_qty']==0
    exit_price=(s.bid if sign==1 else s.ask)-sign*p['slippage_per_unit']
    expected=-cost+p['qty']*(sign*(exit_price-p['entry'])-exit_price*p['fee'])
    assert closed['realized_pnl']==pytest.approx(expected)
    assert ledger.summary(s.now_ms)['equity']==pytest.approx(1000+expected)
    assert ledger.summary(s.now_ms)['losses']==1


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_tp_tranches_break_even_and_runner_are_not_repeated(direction,tmp_path):
    d=approved(direction); ledger=PaperLedger(tmp_path/'paper.db'); p=d['result'].plan
    ledger.queue(d,lambda:100); enter(ledger,d)
    sign=1 if direction=='LONG' else -1
    ledger.advance(quote(d,p['tp1']+sign*.01,2000))
    first=ledger.trades()[0]
    assert first['remaining_qty']==pytest.approx(p['qty']-p['tp1_qty'])
    assert sign*(first['stop']-p['entry'])>0
    ledger.advance(quote(d,p['tp1']+sign*.01,2500))
    assert ledger.trades()[0]['remaining_qty']==first['remaining_qty']
    ledger.advance(quote(d,p['tp2']+sign*.01,3000))
    second=ledger.trades()[0]
    assert second['remaining_qty']==pytest.approx(p['runner_qty'])
    assert second['tp1_ms'] and second['tp2_ms']
    ledger.advance(quote(d,second['stop']-sign*.01,4000))
    assert ledger.trades()[0]['status']=='CLOSED'
    events=json.loads(ledger.export_json())['events']
    assert [x['event'] for x in events].count('TP1')==1
    assert [x['event'] for x in events].count('TP2')==1


def test_trigger_does_not_chase_outside_limit_or_use_old_candle_touch(tmp_path):
    d=approved(); ledger=PaperLedger(tmp_path/'paper.db'); p=d['result'].plan
    ledger.queue(d,lambda:100)
    # Historical current-bar high is intentionally ignored for a new order.
    s=quote(d,p['entry_trigger']-.1); s.open_bar_high=p['entry']+100
    ledger.advance(s)
    assert ledger.trades()[0]['status']=='PENDING' and not ledger.trades()[0]['triggered']
    ledger.advance(quote(d,p['entry']+.2,2000))
    assert ledger.trades()[0]['status']=='PENDING' and ledger.trades()[0]['triggered']
    enter(ledger,d,3000)


def test_expiry_news_wait_stale_quote_and_restart(tmp_path):
    d=approved(); path=tmp_path/'paper.db'; ledger=PaperLedger(path)
    ledger.queue(d,lambda:100)
    bad=quote(d,d['snapshot'].last); bad.quote_times['mark']=bad.now_ms-30001
    with pytest.raises(ValueError): ledger.advance(bad)
    assert ledger.trades()[0]['status']=='PENDING'
    reloaded=PaperLedger(path,5000)
    assert reloaded.summary(NOW)['initial_equity']==1000
    reloaded.advance(quote(d,d['snapshot'].last),news_ok=False)
    assert reloaded.trades()[0]['status']=='CANCELLED'
    d=approved(symbol='ETHUSDT'); ledger.queue(d,lambda:100)
    ledger.expire_pending(d['result'].plan['expires_ms'])
    assert all(t['status']=='CANCELLED' for t in ledger.trades())


def test_offline_gap_not_backfilled(tmp_path):
    d=approved(); ledger=PaperLedger(tmp_path/'paper.db'); ledger.queue(d,lambda:100)
    ledger.advance(quote(d,d['snapshot'].last,200000))
    t=ledger.trades()[0]
    assert t['status']=='CANCELLED' and t['quality']=='DATA_GAP'
    ledger=PaperLedger(tmp_path/'open_gap.db')
    d2=approved(symbol='ETHUSDT'); ledger.queue(d2,lambda:100); enter(ledger,d2)
    ledger.advance(quote(d2,d2['result'].plan['entry'],210000))
    t=ledger.trades()[0]
    assert t['status']=='CLOSED' and t['quality']=='DATA_GAP'


def test_portfolio_slots_and_correlated_risk_are_reserved_atomically(tmp_path):
    ledger=PaperLedger(tmp_path/'paper.db')
    for symbol in ['BTCUSDT','ETHUSDT']:
        assert ledger.queue(approved(symbol=symbol),lambda:100)[0]
    assert not ledger.queue(approved(symbol='SOLUSDT'),lambda:100)[0]
    state=ledger.summary(NOW)
    assert state['active']==2 and state['total_risk']<=state['equity']*.01
    assert not ledger.queue(approved(direction='SHORT'),lambda:100)[0]


def test_malformed_depth_cannot_produce_fill(tmp_path):
    ledger=PaperLedger(tmp_path/'paper.db'); d=approved(); ledger.queue(d,lambda:100)
    s=quote(d,d['result'].plan['entry_trigger']); s.asks=[[s.ask,float('inf')]]
    with pytest.raises(ValueError): ledger.advance(s)
    assert ledger.trades()[0]['status']=='PENDING'


def test_pending_cancelled_when_liquidity_filter_fails(tmp_path):
    ledger=PaperLedger(tmp_path/'paper.db'); d=approved(); ledger.queue(d,lambda:100)
    s=quote(d,d['snapshot'].last); s.asks=[[s.ask,.00001]]
    ledger.advance(s)
    assert ledger.trades()[0]['status']=='CANCELLED'
    assert ledger.trades()[0]['note']=='FILTER_CHANGED'

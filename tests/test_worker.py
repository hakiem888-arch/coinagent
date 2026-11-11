import sys
from futures.market_scanner import MarketScanner


def test_worker_once_reads_secrets_without_logging_keys(monkeypatch,tmp_path,capsys):
    import worker
    calls=[]
    monkeypatch.setenv('FUTURES_HISTORY_DB',str(tmp_path/'paper.db'))
    monkeypatch.setenv('GROQ_API_KEY','test-private-groq')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','123:test-private-telegram')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','-100123')
    monkeypatch.setattr(sys,'argv',['worker.py','--once','--no-telegram','--interval','45','--candidates','1'])
    def run(self,config,manual=False):
        calls.append((self,config))
        return {'now_ms':1,'universe_count':300,'shortlist':[],'selected':[],'analyses':[],'errors':[]}
    monkeypatch.setattr(MarketScanner,'run',run)
    worker.main()
    engine,config=calls[0]
    assert engine.market.instrument_kind == "USD_M_PERPETUAL"
    assert engine.market.time_path == "/fapi/v1/time"
    assert config.interval==45 and config.candidates==1 and not config.send_alerts
    assert engine.pool.keys==('test-private-groq',)
    assert 'test-private' not in capsys.readouterr().out


def test_worker_stops_on_access_restriction_instead_of_retry_loop(monkeypatch, tmp_path, capsys):
    import worker
    monkeypatch.setenv('FUTURES_HISTORY_DB', str(tmp_path / 'paper.db'))
    monkeypatch.setattr(sys, 'argv', ['worker.py', '--no-telegram'])
    monkeypatch.setattr(MarketScanner, 'run', lambda *_args, **_kwargs: {
        'scan_status': 'FAILED', 'started_at_ms': 12345, 'market_error': {'access_blocked': True, 'http_status': 451}})
    assert worker.main() == 2
    output = capsys.readouterr().out
    assert 'STOPPED_HTTP_451' in output and '12345' in output

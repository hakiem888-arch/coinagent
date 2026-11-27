import json
import numpy as np
import pandas as pd

from simple_engine import (
    ScanSettings, SimpleStore, SimpleTradingEngine,
    bollinger_bands, evaluate_reversal, rsi_wilder,
)


def make_frame(closes, start_ms, step_ms):
    rows = []
    for i, close in enumerate(closes):
        open_price = close + (0.1 if i % 2 == 0 else -0.1)
        high = max(open_price, close) + 0.4
        low = min(open_price, close) - 0.4
        rows.append({
            "open_time": start_ms + i * step_ms,
            "close_time": start_ms + (i + 1) * step_ms - 1,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 100.0,
        })
    return pd.DataFrame(rows)


def test_bollinger_bands_default_20_2():
    close = pd.Series([100.0] * 19 + [110.0])
    bands = bollinger_bands(close)
    assert bands.iloc[-1].middle == 100.5
    assert bands.iloc[-1].upper > bands.iloc[-1].middle
    assert bands.iloc[-1].lower < bands.iloc[-1].middle


def test_rsi_wilder_bounds_and_rising_after_oversold():
    close = pd.Series(list(np.linspace(100, 70, 30)) + [75.0])
    rsi = rsi_wilder(close)
    assert 0 <= rsi.iloc[-2] <= 30
    assert rsi.iloc[-1] > rsi.iloc[-2]
    assert 0 <= rsi.iloc[-1] <= 100


def test_long_signal_requires_m15_lower_touch_m5_reversal_and_rsi():
    m15_closes = [100.0] * 24 + [95.0]
    m15 = make_frame(m15_closes, 0, 900_000)
    m15.loc[m15.index[-1], "low"] = 90.0
    m15.loc[m15.index[-1], "close_time"] = 25 * 900_000 - 1

    m5_closes = list(np.linspace(100.0, 70.0, 35)) + [75.0]
    m5 = make_frame(m5_closes, 25 * 900_000, 300_000)
    # Explicit bullish higher-high/higher-close confirmation.
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [71.0, 72.0, 68.0, 70.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [71.0, 77.0, 69.0, 75.0]
    out = evaluate_reversal(m15, m5, "TESTUSDT")
    assert out is not None
    assert out["direction"] == "LONG"
    assert out["m15_touch_band"] == "LOWER"
    assert out["entry"] == 75.0
    assert out["stop"] == 68.0
    assert out["target"] == 89.0


def test_no_signal_when_m15_did_not_touch_band():
    m15 = make_frame([100.0 + 0.3 * np.sin(i / 2) for i in range(25)], 0, 900_000)
    m15["high"] = m15["close"] + 0.05
    m15["low"] = m15["close"] - 0.05
    m5 = make_frame(list(np.linspace(100.0, 70.0, 35)) + [75.0], 25 * 900_000, 300_000)
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [71.0, 72.0, 68.0, 70.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [71.0, 77.0, 69.0, 75.0]
    assert evaluate_reversal(m15, m5, "TESTUSDT") is None


def test_no_signal_when_m5_candle_does_not_confirm_direction():
    m15 = make_frame([100.0] * 24 + [95.0], 0, 900_000)
    m15.loc[m15.index[-1], "low"] = 90.0
    m15.loc[m15.index[-1], "close_time"] = 25 * 900_000 - 1
    m5 = make_frame(list(np.linspace(100.0, 70.0, 35)) + [75.0], 25 * 900_000, 300_000)
    # Latest candle has a higher close but does not make a higher high.
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [71.0, 75.0, 68.0, 70.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [71.0, 74.0, 69.0, 73.0]
    assert evaluate_reversal(m15, m5, "TESTUSDT") is None


class FakeFuturesMarket:
    instrument_kind = "USD_M_PERPETUAL"
    market_label = "Fake Binance Futures"
    futures_available = True

    def __init__(self, m15, m5):
        self.frames = {"15m": m15, "5m": m5}

    def universe(self):
        return {
            "now_ms": 40_000_000,
            "info": {"symbols": [{"symbol": "TESTUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}]},
            "tickers": [{"symbol": "TESTUSDT", "quoteVolume": "1000000"}],
        }

    def closed_candles(self, symbol, timeframe, server_ms, limit=80):
        return self.frames[timeframe].copy()


def build_long_frames():
    m15 = make_frame([100.0] * 24 + [95.0], 0, 900_000)
    m15.loc[m15.index[-1], "low"] = 90.0
    m15.loc[m15.index[-1], "close_time"] = 25 * 900_000 - 1
    m5 = make_frame(list(np.linspace(100.0, 70.0, 35)) + [75.0], 25 * 900_000, 300_000)
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [71.0, 72.0, 68.0, 70.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [71.0, 77.0, 69.0, 75.0]
    return m15, m5


def test_simple_scanner_records_paper_signal(tmp_path):
    m15, m5 = build_long_frames()
    market = FakeFuturesMarket(m15, m5)
    store = SimpleStore(tmp_path / "simple_test.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=market, store=store, root=tmp_path, secret_getter=lambda _key: "")
    result = engine.scan(ScanSettings(symbols=10, send_telegram=False))
    assert result["status"] == "COMPLETED"
    assert result["scanned"] == 1
    assert len(result["signals"]) == 1
    assert result["signals"][0]["direction"] == "LONG"
    assert len(store.active_trades()) == 1
    assert store.latest_signals(1)[0]["paper_status"] == "PAPER_OPEN"
    # A repeated scan of the same closed candles must not duplicate the alert/trade.
    second = engine.scan(ScanSettings(symbols=10, initial_equity=2000.0, send_telegram=False))
    assert second["signals"] == []
    assert store.get_initial_equity() == 1000.0
    assert len(store.paper_trades(20)) == 1


def test_short_signal_requires_m15_upper_touch_m5_reversal_and_rsi():
    m15 = make_frame([100.0] * 24 + [105.0], 0, 900_000)
    m15.loc[m15.index[-1], "high"] = 110.0
    m15.loc[m15.index[-1], "close_time"] = 25 * 900_000 - 1
    m5_closes = list(np.linspace(100.0, 130.0, 35)) + [125.0]
    m5 = make_frame(m5_closes, 25 * 900_000, 300_000)
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [129.0, 132.0, 128.0, 130.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [129.0, 131.0, 123.0, 125.0]
    out = evaluate_reversal(m15, m5, "TESTUSDT")
    assert out is not None
    assert out["direction"] == "SHORT"
    assert out["m15_touch_band"] == "UPPER"
    assert out["entry"] == 125.0
    assert out["stop"] == 132.0
    assert out["target"] == 111.0


def test_no_signal_when_m5_confirmation_closed_before_m15_touch():
    m15, m5 = build_long_frames()
    m5.loc[m5.index[-1], "close_time"] = m15.iloc[-1].close_time
    assert evaluate_reversal(m15, m5, "TESTUSDT") is None


def test_paper_monitor_checks_all_unseen_m5_candles(tmp_path):
    store = SimpleStore(tmp_path / "monitor.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=object(), store=store, root=tmp_path, secret_getter=lambda _key: "")
    trade = {
        "signal_id": "monitor-test", "symbol": "TESTUSDT", "direction": "LONG",
        "entry": 100.0, "stop": 95.0, "target": 110.0, "quantity": 1.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0,
        "mark_price": 100.0,
    }
    store._init_schema()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
            "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("monitor-test", "TESTUSDT", "LONG", 100.0, 95.0, 110.0, 1.0, 2, 5.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()
    # The first M5 candle reaches SL; the last candle recovers. The earlier exit must win.
    bars = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100, "high": 102, "low": 94, "close": 98},
        {"open_time": 600_000, "close_time": 899_999, "open": 98, "high": 111, "low": 97, "close": 108},
    ])
    engine._monitor_trade(trade, bars)
    saved = store.paper_trades(5)[0]
    assert saved["status"] == "CLOSED_SL"
    assert saved["exit_price"] == 95.0
    assert saved["closed_ms"] == 599_999


def test_latest_telegram_signals_enriches_and_filters(tmp_path):
    m15, m5 = build_long_frames()
    market = FakeFuturesMarket(m15, m5)
    store = SimpleStore(tmp_path / "tele_table.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=market, store=store, root=tmp_path, secret_getter=lambda _key: "")
    result = engine.scan(ScanSettings(symbols=10, send_telegram=False))
    sid = result["signals"][0]["signal_id"]

    all_rows = store.latest_telegram_signals(limit=20, sent_only=False)
    assert len(all_rows) == 1
    assert all_rows[0]["telegram_sent"] is False
    assert all_rows[0]["rr_ratio"] == 2.0
    assert all_rows[0]["sl_pct"] > 0
    assert all_rows[0]["tp_pct"] > 0
    assert all_rows[0]["delta_rsi"] > 0
    assert store.latest_telegram_signals(limit=20, sent_only=True) == []

    store.update_alert_status(sid, "Alert Telegram terkirim")
    sent_rows = store.latest_telegram_signals(limit=20, sent_only=True)
    assert len(sent_rows) == 1
    assert sent_rows[0]["telegram_sent"] is True


def test_step_trailing_stop_long_moves_be_then_locks_half_r_and_exits_in_profit(tmp_path):
    store = SimpleStore(tmp_path / "trail_long.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=object(), store=store, root=tmp_path, secret_getter=lambda _key: "")
    # Entry 100, Initial SL 95 (1R = 5.0), TP 115 (3R)
    trade = {
        "signal_id": "trail-long-1", "symbol": "TESTUSDT", "direction": "LONG",
        "entry": 100.0, "stop": 95.0, "target": 115.0, "quantity": 2.0, "risk_usdt": 10.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0, "mark_price": 100.0,
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
            "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trail-long-1", "TESTUSDT", "LONG", 100.0, 95.0, 115.0, 2.0, 2, 10.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()

    # Bar 1 reaches +1.0R (105) -> moves SL to BE +0.05R (100.25)
    # Bar 2 reaches +1.5R (107.5) -> moves SL to +0.50R (102.50)
    # Bar 3 pulls back to 102.0 -> exits at trailed SL 102.50 with CLOSED_TRAIL_TP (+5.0 USDT)
    bars = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100.0, "high": 105.2, "low": 99.0, "close": 104.5},
        {"open_time": 600_000, "close_time": 899_999, "open": 104.5, "high": 108.0, "low": 103.0, "close": 106.0},
        {"open_time": 900_000, "close_time": 1_199_999, "open": 106.0, "high": 106.5, "low": 102.0, "close": 103.0},
    ])
    engine._monitor_trade(trade, bars, trailing_stop_enabled=True)
    saved = store.paper_trades(5)[0]
    assert saved["status"] == "CLOSED_TRAIL_TP"
    assert abs(saved["stop"] - 102.5) < 1e-9
    assert abs(saved["exit_price"] - 102.5) < 1e-9
    assert abs(saved["pnl_usdt"] - 5.0) < 1e-9


def test_step_trailing_stop_short_open_tp_extends_above_2r(tmp_path):
    store = SimpleStore(tmp_path / "trail_short.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=object(), store=store, root=tmp_path, secret_getter=lambda _key: "")
    # SHORT Entry 100, Initial SL 105 (1R = 5.0), Open TP (target=0.0)
    trade = {
        "signal_id": "trail-short-1", "symbol": "TESTUSDT", "direction": "SHORT",
        "entry": 100.0, "stop": 105.0, "target": 0.0, "quantity": 1.0, "risk_usdt": 5.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0, "mark_price": 100.0,
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
            "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trail-short-1", "TESTUSDT", "SHORT", 100.0, 105.0, 0.0, 1.0, 2, 5.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()

    # Bar 1 drops to 89.5 (+2.1R) -> moves SL to +1.20R (94.0)
    # Bar 2 drops to 87.0 (+2.6R) -> dynamic step locks +1.70R (91.5)
    # Bar 3 bounces to 92.0 -> exits at 91.5 (+8.5 USDT)
    bars = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100.0, "high": 101.0, "low": 89.5, "close": 90.0},
        {"open_time": 600_000, "close_time": 899_999, "open": 90.0, "high": 91.0, "low": 87.0, "close": 88.0},
        {"open_time": 900_000, "close_time": 1_199_999, "open": 88.0, "high": 92.0, "low": 87.5, "close": 91.8},
    ])
    engine._monitor_trade(trade, bars, trailing_stop_enabled=True)
    saved = store.paper_trades(5)[0]
    assert saved["status"] == "CLOSED_TRAIL_TP"
    assert abs(saved["exit_price"] - 91.5) < 1e-9
    assert abs(saved["pnl_usdt"] - 8.5) < 1e-9


def test_step_trailing_is_conservative_within_same_candle(tmp_path):
    store = SimpleStore(tmp_path / "trail_cons.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=object(), store=store, root=tmp_path, secret_getter=lambda _key: "")
    trade = {
        "signal_id": "trail-cons-1", "symbol": "TESTUSDT", "direction": "LONG",
        "entry": 100.0, "stop": 95.0, "target": 110.0, "quantity": 1.0, "risk_usdt": 5.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0, "mark_price": 100.0,
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
            "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trail-cons-1", "TESTUSDT", "LONG", 100.0, 95.0, 110.0, 1.0, 2, 5.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()
    # One wild candle hits BOTH +1.2R high (106) and initial SL low (94) -> conservative SL hit must win
    bars = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100.0, "high": 106.0, "low": 94.0, "close": 103.0},
    ])
    engine._monitor_trade(trade, bars, trailing_stop_enabled=True)
    saved = store.paper_trades(5)[0]
    assert saved["status"] == "CLOSED_SL"
    assert abs(saved["exit_price"] - 95.0) < 1e-9


def test_supabase_rest_sync_auto_backup_and_restore_after_reboot(tmp_path):
    from futures.db_backend import SupabaseStateSync

    remote_db = {}

    def fake_transport(req, timeout=8):
        url = req.full_url
        method = req.get_method()
        if method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            remote_db[body["namespace"]] = body
            return 201, b"[]"
        if method == "GET":
            for ns, row in remote_db.items():
                if ns in url:
                    return 200, json.dumps([row]).encode("utf-8")
            return 200, b"[]"
        return 404, b""

    sync1 = SupabaseStateSync(
        supabase_url="https://example.supabase.co",
        supabase_key="service-role-secret",
        transport=fake_transport,
        writer_id="simple-reversal-v7",
        enforce_single_writer=False,
    )
    m15, m5 = build_long_frames()
    market = FakeFuturesMarket(m15, m5)
    store1 = SimpleStore(tmp_path / "instance1.sqlite3", secret_getter=lambda _k: "", supabase_sync=sync1)
    assert store1.storage_mode == "SUPABASE_REST_SYNC"
    engine1 = SimpleTradingEngine(market=market, store=store1, root=tmp_path, secret_getter=lambda _k: "")
    res = engine1.scan(ScanSettings(symbols=10, initial_equity=1500.0, send_telegram=False))
    assert len(res["signals"]) == 1
    assert SimpleStore.SUPABASE_REST_NAMESPACE in remote_db

    # Simulate Streamlit Cloud reboot: brand-new empty SQLite path, same Supabase remote backup
    sync2 = SupabaseStateSync(
        supabase_url="https://example.supabase.co",
        supabase_key="service-role-secret",
        transport=fake_transport,
        writer_id="simple-reversal-v7",
        enforce_single_writer=False,
    )
    store2 = SimpleStore(tmp_path / "fresh_after_reboot.sqlite3", secret_getter=lambda _k: "", supabase_sync=sync2)
    assert store2.restored_from_supabase is True
    assert store2.get_initial_equity() == 1500.0
    assert len(store2.latest_signals(20)) == 1
    assert len(store2.active_trades()) == 1
    assert store2.active_trades()[0]["symbol"] == "TESTUSDT"

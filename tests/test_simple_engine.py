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

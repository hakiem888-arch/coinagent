import json
import threading
import time as _t

import numpy as np
import pandas as pd

from simple_engine import (
    SIZING_MARGIN_PCT,
    SIZING_MARGIN_USD,
    SIZING_RISK_PCT,
    ScanSettings,
    SimpleStore,
    SimpleTradingEngine,
    bollinger_bands,
    calculate_position_sizing,
    evaluate_reversal,
    rsi_wilder,
    trade_financials,
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


def build_long_frames():
    m15 = make_frame([100.0] * 24 + [95.0], 0, 900_000)
    m15.loc[m15.index[-1], "low"] = 90.0
    m15.loc[m15.index[-1], "close_time"] = 25 * 900_000 - 1
    m5 = make_frame(list(np.linspace(100.0, 70.0, 35)) + [75.0], 25 * 900_000, 300_000)
    m5.loc[m5.index[-2], ["open", "high", "low", "close"]] = [71.0, 72.0, 68.0, 70.0]
    m5.loc[m5.index[-1], ["open", "high", "low", "close"]] = [71.0, 77.0, 69.0, 75.0]
    return m15, m5


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

    def closed_candles(self, symbol, timeframe, server_ms, limit=50):
        return self.frames[timeframe].copy()


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


def test_long_and_short_signals():
    m15, m5 = build_long_frames()
    out = evaluate_reversal(m15, m5, "TESTUSDT")
    assert out is not None and out["direction"] == "LONG"
    assert out["entry"] == 75.0 and out["stop"] == 68.0 and out["target"] == 89.0

    m15_s = make_frame([100.0] * 24 + [105.0], 0, 900_000)
    m15_s.loc[m15_s.index[-1], "high"] = 110.0
    m15_s.loc[m15_s.index[-1], "close_time"] = 25 * 900_000 - 1
    m5_s = make_frame(list(np.linspace(100.0, 130.0, 35)) + [125.0], 25 * 900_000, 300_000)
    m5_s.loc[m5_s.index[-2], ["open", "high", "low", "close"]] = [129.0, 132.0, 128.0, 130.0]
    m5_s.loc[m5_s.index[-1], ["open", "high", "low", "close"]] = [129.0, 131.0, 123.0, 125.0]
    out_s = evaluate_reversal(m15_s, m5_s, "TESTUSDT")
    assert out_s is not None and out_s["direction"] == "SHORT"
    assert out_s["entry"] == 125.0 and out_s["stop"] == 132.0 and out_s["target"] == 111.0


def test_perpetual_margin_sizing_scales_directly_with_leverage():
    # Entry 100, SL 99 (1% SL) on 1000 USDT equity
    s_2x = ScanSettings(sizing_mode=SIZING_MARGIN_USD, margin_usd=50.0, leverage=2)
    s_20x = ScanSettings(sizing_mode=SIZING_MARGIN_USD, margin_usd=50.0, leverage=20)
    r_2x = calculate_position_sizing(100.0, 99.0, 1000.0, s_2x)
    r_20x = calculate_position_sizing(100.0, 99.0, 1000.0, s_20x)
    assert abs(r_2x["margin_usdt"] - 50.0) < 1e-9
    assert abs(r_2x["notional_usdt"] - 100.0) < 1e-9
    assert abs(r_20x["margin_usdt"] - 50.0) < 1e-9
    assert abs(r_20x["notional_usdt"] - 1000.0) < 1e-9
    # 20x must produce 10x larger quantity & 10x larger PnL than 2x!
    assert abs(r_20x["qty"] / r_2x["qty"] - 10.0) < 1e-9

    # Percent of equity mode: 10% of 1000 = 100 USDT margin * 20x = 2000 USDT notional
    s_pct = ScanSettings(sizing_mode=SIZING_MARGIN_PCT, margin_pct=10.0, leverage=20)
    r_pct = calculate_position_sizing(100.0, 99.0, 1000.0, s_pct)
    assert abs(r_pct["margin_usdt"] - 100.0) < 1e-9
    assert abs(r_pct["notional_usdt"] - 2000.0) < 1e-9
    assert abs(r_pct["qty"] - 20.0) < 1e-9

    # Verify ROE% calculation on +2% price move at 20x leverage -> +40% ROE
    fin = trade_financials({
        "entry": 100.0, "quantity": 20.0, "leverage": 20, "direction": "LONG",
        "status": "CLOSED_TP", "mark_price": 102.0, "pnl_usdt": 40.0,
    })
    assert abs(fin["margin_usdt"] - 100.0) < 1e-9
    assert abs(fin["roe_pct"] - 40.0) < 1e-9


def test_simple_scanner_records_paper_signal_and_winrate_stats(tmp_path):
    m15, m5 = build_long_frames()
    market = FakeFuturesMarket(m15, m5)
    store = SimpleStore(tmp_path / "simple_test.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=market, store=store, root=tmp_path, secret_getter=lambda _key: "")
    settings = ScanSettings(symbols=10, sizing_mode=SIZING_MARGIN_USD, margin_usd=50.0, leverage=10, send_telegram=False)
    result = engine.scan(settings)
    assert result["status"] == "COMPLETED"
    assert len(result["signals"]) == 1
    assert abs(result["signals"][0]["margin_usdt"] - 50.0) < 1e-9
    assert abs(result["signals"][0]["notional_usdt"] - 500.0) < 1e-9
    assert len(store.active_trades()) == 1

    # Duplicate scan must not double-record
    second = engine.scan(settings)
    assert second["signals"] == []
    assert store.get_initial_equity() == 1000.0

    # Close trade in profit and verify Win Rate = 100%
    trade = store.active_trades()[0]
    tp_bar = pd.DataFrame([
        {"open_time": trade["opened_ms"] + 1, "close_time": trade["opened_ms"] + 300_000,
         "open": 75.0, "high": 90.0, "low": 74.5, "close": 89.5},
    ])
    engine._monitor_trade(trade, tp_bar, trailing_stop_enabled=True)
    snap = store.dashboard_snapshot(1000.0)
    assert snap["summary"]["closed"] == 1
    assert snap["summary"]["wins"] == 1
    assert snap["summary"]["win_rate"] == 100.0
    assert snap["summary"]["profit_factor"] == float("inf")
    assert snap["summary"]["realized"] > 0


def test_step_trailing_stop_long_and_isolated_liquidation(tmp_path):
    store = SimpleStore(tmp_path / "trail_liq.sqlite3", secret_getter=lambda _key: "")
    engine = SimpleTradingEngine(market=object(), store=store, root=tmp_path, secret_getter=lambda _key: "")

    # Trade 1: Step R-Multiple Trailing Stop (+1.0R -> BE, +1.5R -> +0.5R, then exits +0.5R)
    trade = {
        "signal_id": "trail-1", "symbol": "TESTUSDT", "direction": "LONG",
        "entry": 100.0, "stop": 95.0, "target": 115.0, "quantity": 2.0, "leverage": 2, "risk_usdt": 10.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0, "mark_price": 100.0,
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("trail-1", "TESTUSDT", "LONG", 100.0, 95.0, 115.0, 2.0, 2, 10.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()
    bars = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100.0, "high": 105.2, "low": 99.0, "close": 104.5},
        {"open_time": 600_000, "close_time": 899_999, "open": 104.5, "high": 108.0, "low": 103.0, "close": 106.0},
        {"open_time": 900_000, "close_time": 1_199_999, "open": 106.0, "high": 106.5, "low": 102.0, "close": 103.0},
    ])
    engine._monitor_trade(trade, bars, trailing_stop_enabled=True)
    saved = store.paper_trades(5)[0]
    assert saved["status"] == "CLOSED_TRAIL_TP"
    assert abs(saved["exit_price"] - 102.5) < 1e-9
    assert abs(saved["pnl_usdt"] - 5.0) < 1e-9

    # Trade 2: High leverage 20x where SL is wider than liquidation (-8% SL vs ~-5% liq) -> capped at -margin (CLOSED_LIQ)
    trade_liq = {
        "signal_id": "liq-1", "symbol": "LIQUSDT", "direction": "LONG",
        "entry": 100.0, "stop": 92.0, "target": 116.0, "quantity": 10.0, "leverage": 20, "risk_usdt": 80.0,
        "status": "OPEN", "opened_ms": 299_999, "updated_ms": 299_999,
        "closed_ms": None, "exit_price": None, "pnl_usdt": 0.0, "mark_price": 100.0,
    }
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO simple_paper_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("liq-1", "LIQUSDT", "LONG", 100.0, 92.0, 116.0, 10.0, 20, 80.0,
             "OPEN", 299_999, 299_999, None, None, 0.0, 100.0, "USD_M_PERPETUAL"),
        )
        conn.commit()
    crash_bar = pd.DataFrame([
        {"open_time": 300_000, "close_time": 599_999, "open": 100.0, "high": 100.5, "low": 91.0, "close": 93.0},
    ])
    engine._monitor_trade(trade_liq, crash_bar, trailing_stop_enabled=True)
    saved_liq = [t for t in store.paper_trades(10) if t["signal_id"] == "liq-1"][0]
    assert saved_liq["status"] == "CLOSED_LIQ"
    # Margin = (100 * 10) / 20 = 50 USDT -> max loss capped at -50 USDT
    assert abs(saved_liq["pnl_usdt"] - (-50.0)) < 1e-9


def test_fast_scan_skips_m5_when_no_m15_touch_and_runs_in_parallel(tmp_path):
    m15_hit, m5_hit = build_long_frames()
    m15_flat = make_frame([100.0 + 0.05 * np.sin(i / 2) for i in range(25)], 0, 900_000)
    m15_flat["high"] = m15_flat["close"] + 0.02
    m15_flat["low"] = m15_flat["close"] - 0.02

    class CountingMarket:
        instrument_kind = "USD_M_PERPETUAL"
        market_label = "Mock Fast Futures"
        futures_available = True

        def __init__(self):
            self.lock = threading.Lock()
            self.m15_calls = 0
            self.m5_calls = 0

        def universe(self):
            syms = [f"COIN{i}USDT" for i in range(12)]
            return {
                "now_ms": 40_000_000,
                "info": {"symbols": [{"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"} for s in syms]},
                "tickers": [{"symbol": s, "quoteVolume": str(1000000 - i)} for i, s in enumerate(syms)],
            }

        def closed_candles(self, symbol, timeframe, server_ms, limit=50):
            _t.sleep(0.02)
            with self.lock:
                if timeframe == "15m":
                    self.m15_calls += 1
                else:
                    self.m5_calls += 1
            if timeframe == "15m":
                return m15_hit.copy() if symbol == "COIN0USDT" else m15_flat.copy()
            return m5_hit.copy()

    market = CountingMarket()
    store = SimpleStore(tmp_path / "fast_perf.sqlite3", secret_getter=lambda _k: "")
    engine = SimpleTradingEngine(market=market, store=store, root=tmp_path, secret_getter=lambda _k: "")
    rep = engine.scan(ScanSettings(symbols=12, send_telegram=False))
    assert rep["status"] == "COMPLETED"
    assert rep["scanned"] == 12
    assert market.m15_calls == 12
    assert market.m5_calls == 1
    assert rep["m5_fetched"] == 1
    assert rep["m5_skipped"] == 11
    assert rep["duration_seconds"] < 0.5


def test_supabase_rest_sync_auto_backup_and_restore(tmp_path):
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
    )
    m15, m5 = build_long_frames()
    store1 = SimpleStore(tmp_path / "inst1.sqlite3", secret_getter=lambda _k: "", supabase_sync=sync1)
    engine1 = SimpleTradingEngine(market=FakeFuturesMarket(m15, m5), store=store1, root=tmp_path, secret_getter=lambda _k: "")
    res = engine1.scan(ScanSettings(symbols=10, initial_equity=1500.0, send_telegram=False))
    assert len(res["signals"]) == 1

    sync2 = SupabaseStateSync(
        supabase_url="https://example.supabase.co",
        supabase_key="service-role-secret",
        transport=fake_transport,
    )
    store2 = SimpleStore(tmp_path / "inst2.sqlite3", secret_getter=lambda _k: "", supabase_sync=sync2)
    assert store2.restored_from_supabase is True
    assert store2.get_initial_equity() == 1500.0
    assert len(store2.active_trades()) == 1

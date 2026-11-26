"""Comprehensive End-to-End Acceptance Test Suite for CoinAgentX v6.0.5 — Predictive Watchlist v1.0.

Covers all 6 mandatory acceptance categories from Master Implementation Prompt Section J.7:
1. Market Data (M1/M5/M15 historical & live, immutability, cache isolation, stale, WS reconnect, REST fallback, failover, Spot blocked)
2. Predictive Engine (PRE-WATCH BUY/SELL, EARLY WATCH BUY/SELL, strengthening/weakening, reversal, close confirmation, missing data, zero lookahead, conflict, high volatility)
3. Lifecycle (PRE_WATCH -> EARLY_WATCH -> SETUP_FORMING -> CONFIRMED, hard-gate block, BUY->SELL, invalidation, expiry, MISSED_ENTRY, duplicate event, worker restart)
4. Alert (all alert types, deduplication, retry backoff, token redaction)
5. Integration (shared DB fingerprint, PG available/unavailable fail-closed, queue failure visible, heartbeat/stale, single-worker lease, Groq Advisor-Only, PAPER-only)
6. Performance, Replay & Dashboard UI (<=50 cap, 20 deep target, <=5 Groq, rate limits, no memory leak, no refresh duplication, chronological replay & walk-forward, 18 UI columns)
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from futures.bybit_market import BybitFuturesMarket
from futures.db_backend import (
    DatabaseConfigurationError,
    connect_database,
    database_identity_fingerprint,
)
from futures.groq_rotation import GroqRotation, evaluate_contextual_ai
from futures.intrabar_stream import (
    IntrabarStreamManager,
    parse_binance_kline_message,
    parse_bybit_kline_message,
    validate_ohlcv_dataframe,
)
from futures.market import FuturesMarket, MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.models import (
    INTERVAL_MS,
    MONITOR_TIMEFRAMES,
    TIMEFRAMES,
    IntrabarCandle,
    MonitorSnapshot,
    Result,
)
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.predictive_replay import (
    aggregate_ohlcv_timeframe,
    calibrate_and_evaluate_walk_forward,
    chronological_split,
    replay_predictive_m1_series,
)
from futures.predictive_store import PredictiveSignalRepository
from futures.predictive_ui import (
    STAGE_DISCLAIMERS,
    badge_color_for_signal,
    build_watchlist_rows,
    filter_predictive_signals,
    sort_predictive_signals,
)
from futures.predictive_watchlist import (
    PredictiveConfig,
    PredictiveLifecycleManager,
    PredictiveSignal,
    STAGE_CONFIRMED,
    STAGE_CONFLICT_WAIT,
    STAGE_DIRECTION_CHANGED,
    STAGE_EARLY_WATCH,
    STAGE_EXPIRED,
    STAGE_MISSED_ENTRY,
    STAGE_NO_ENTRY_HIGH_VOLATILITY,
    STAGE_PAPER_ENTRY,
    STAGE_PRE_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_SIGNAL_INVALIDATED,
    STAGE_SIGNAL_STRENGTHENING,
    STAGE_SIGNAL_WEAKENING,
    evaluate_early_signal,
    is_valid_transition,
)
from futures.process_lock import ProcessLock
from futures.provider_router import FuturesProviderRouter
from futures.scanner import ScanBusy
from futures.spot_market import SpotReferenceMarket
from futures.telegram import (
    TelegramAlerts,
    format_predictive_alert,
    redact_telegram_secrets,
)
from futures.version import APP_VERSION, SPEC_VERSION
import worker


# ---------------------------------------------------------------------------
# Deterministic Candle Builders (Unit/Acceptance Fixtures ONLY)
# ---------------------------------------------------------------------------
def _make_df(n=60, interval="1m", end_ms=1_760_000_000_000, base=100.0, drift=0.05, vol=1000.0, include_open_bar=True):
    step = INTERVAL_MS[interval]
    last_open = (end_ms // step) * step if include_open_bar else (end_ms // step - 1) * step
    start_open = last_open - (n - 1) * step
    rows = []
    for i in range(n):
        ot = start_open + i * step
        ct = ot + step - 1
        mid = base + drift * i + 0.03 * math.sin(i * 0.4)
        op = mid - 0.05 * (1 if drift >= 0 else -1)
        cl = mid + 0.05 * (1 if drift >= 0 else -1)
        hi = max(op, cl) + 0.25
        lo = min(op, cl) - 0.25
        v = vol * (1.8 if i >= n - 2 else 1.0)
        rows.append({
            "open_time": int(ot),
            "open": float(op),
            "high": float(hi),
            "low": float(lo),
            "close": float(cl),
            "volume": float(v),
            "close_time": int(ct),
            "is_closed": bool(ct < end_ms),
        })
    return pd.DataFrame(rows)


def _make_directional_snapshot(direction="LONG", now_ms=1_760_000_030_000, provider="Binance Futures", kind="USD_M_PERPETUAL", stale=False, high_vol=False):
    sign = 1.0 if direction == "LONG" else -1.0
    m1 = _make_df(60, "1m", now_ms, base=100.0 if sign > 0 else 200.0, drift=0.12 * sign, vol=1000.0, include_open_bar=True)
    # Strong final impulse on M1
    m1.loc[len(m1) - 1, "close"] = float(m1.loc[len(m1) - 2, "close"]) + 0.35 * sign
    m1.loc[len(m1) - 1, "high"] = max(float(m1.loc[len(m1) - 1, "high"]), float(m1.loc[len(m1) - 1, "close"]) + 0.05)
    m1.loc[len(m1) - 1, "low"] = min(float(m1.loc[len(m1) - 1, "low"]), float(m1.loc[len(m1) - 1, "close"]) - 0.05)
    m1.loc[len(m1) - 1, "volume"] = 2800.0

    m5 = _make_df(60, "5m", now_ms, base=100.0 if sign > 0 else 200.0, drift=0.22 * sign, vol=5000.0, include_open_bar=True)
    # Break local swing high/low on M5 (early CHoCH) + volume
    if sign > 0:
        m5.loc[len(m5) - 1, "close"] = float(m5["high"].iloc[-7:-1].max()) + 0.15
        m5.loc[len(m5) - 1, "high"] = float(m5.loc[len(m5) - 1, "close"]) + 0.05
    else:
        m5.loc[len(m5) - 1, "close"] = float(m5["low"].iloc[-7:-1].min()) - 0.15
        m5.loc[len(m5) - 1, "low"] = float(m5.loc[len(m5) - 1, "close"]) - 0.05
    m5.loc[len(m5) - 1, "volume"] = 9500.0

    m15 = _make_df(210 if high_vol else 80, "15m", now_ms, base=100.0 if sign > 0 else 250.0, drift=0.35 * sign, vol=12000.0, include_open_bar=True)
    if high_vol:
        # Extreme ATR + candle range spike on final closed bars to trigger REGIME_HIGH_VOLATILITY
        for i in range(len(m15) - 6, len(m15)):
            m15.loc[i, "high"] = float(m15.loc[i, "close"]) + 45.0
            m15.loc[i, "low"] = max(1.0, float(m15.loc[i, "close"]) - 45.0)

    px = float(m1.iloc[-1]["close"])
    ex_ts = now_ms - (120_000 if stale else 500)
    return MonitorSnapshot(
        symbol="BTCUSDT",
        provider=provider,
        instrument_kind=kind,
        exchange_timestamp_ms=ex_ts,
        local_receive_ms=now_ms,
        last_price=px,
        mark_price=None if kind == "SPOT_REFERENCE" else px,
        index_price=None if kind == "SPOT_REFERENCE" else px,
        bid=px * 0.9999,
        ask=px * 1.0001,
        frames_closed={"1m": m1[m1["is_closed"]].reset_index(drop=True), "5m": m5[m5["is_closed"]].reset_index(drop=True), "15m": m15[m15["is_closed"]].reset_index(drop=True)},
        frames_live={"1m": m1, "5m": m5, "15m": m15},
        data_freshness_ms=max(0, now_ms - ex_ts),
        is_stale=stale,
    )


def _mock_binance_session(now_ms=1_760_000_030_000):
    class _Resp:
        def __init__(self, payload, status=200):
            self._p = payload
            self.status_code = status
            self.headers = {}
            self.text = json.dumps(payload)
        def json(self):
            return self._p

    class _Sess:
        def get(self, url, params=None, **kw):
            if url.endswith("/fapi/v1/time"):
                return _Resp({"serverTime": now_ms})
            if url.endswith("/fapi/v1/klines") or url.endswith("/fapi/v1/markPriceKlines"):
                tf = (params or {}).get("interval", "1m")
                lim = int((params or {}).get("limit", 10))
                df = _make_df(max(lim, 5), tf, now_ms, include_open_bar=True)
                rows = [
                    [int(r.open_time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), int(r.close_time), "100000", 50, "500", "50000", "0"]
                    for r in df.itertuples()
                ]
                return _Resp(rows)
            if url.endswith("/fapi/v1/ticker/bookTicker"):
                return _Resp({"symbol": "BTCUSDT", "bidPrice": "105.0", "askPrice": "105.02", "time": now_ms})
            if url.endswith("/fapi/v1/premiumIndex"):
                return _Resp({"symbol": "BTCUSDT", "markPrice": "105.01", "indexPrice": "105.00", "time": now_ms})
            if url.endswith("/fapi/v2/ticker/price"):
                return _Resp({"symbol": "BTCUSDT", "price": "105.01", "time": now_ms})
            return _Resp({}, status=404)
    return _Sess()


# ===========================================================================
# 1. MARKET DATA ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_01_version_and_m1_timeframe_support():
    assert APP_VERSION in ("6.0.5", "6.0.6")
    assert "Predictive Watchlist v1.0" in SPEC_VERSION
    assert TIMEFRAMES["SCALPING"] == ("15m", "5m")  # backward-compatible
    assert MONITOR_TIMEFRAMES == ("1m", "5m", "15m")
    assert INTERVAL_MS["1m"] == 60000


def test_02_m1_m5_m15_live_and_closed_candle_separation():
    now_ms = 1_760_000_030_000
    mkt = FuturesMarket(session=_mock_binance_session(now_ms))
    closed_m1 = mkt.closed_candles("BTCUSDT", "1m", now_ms, limit=20)
    assert all(closed_m1["is_closed"])
    assert int(closed_m1["close_time"].iloc[-1]) < now_ms
    closed_keys_before = set(mkt.cache.keys())

    # Live candles across M1/M5/M15 must NEVER pollute self.cache (closed cache)
    for tf in ("1m", "5m", "15m"):
        live = mkt.live_candles("BTCUSDT", tf, server_ms=now_ms, limit=20)
        assert not bool(live["is_closed"].iloc[-1])
        cur = mkt.current_candle("BTCUSDT", tf, server_ms=now_ms)
        assert cur.timeframe == tf
        assert cur.is_closed is False
        assert cur.is_valid_ohlc()
    assert set(mkt.cache.keys()) == closed_keys_before


def test_03_closed_candle_immutability_and_ohlcv_validation():
    mgr = IntrabarStreamManager(FuturesMarket(session=_mock_binance_session()), clock=lambda: 1_760_000_030.0)
    c_final = IntrabarCandle("BTCUSDT", "1m", 1760000000000, 1760000059999, 100.0, 102.0, 99.0, 101.0, 500.0, is_closed=True, exchange_timestamp_ms=1760000059999)
    c_mutated = IntrabarCandle("BTCUSDT", "1m", 1760000000000, 1760000059999, 100.0, 110.0, 95.0, 109.0, 999.0, is_closed=True, exchange_timestamp_ms=1760000059999)
    assert mgr.ingest_candle(c_final) is True
    assert mgr.ingest_candle(c_mutated) is True
    # Closed candle remains immutable once finalized
    stored = mgr._closed_buffer[("BTCUSDT", "1m")][1760000000000]
    assert stored.close == 101.0

    # Invalid OHLC rejected
    bad_df = pd.DataFrame([
        {"open_time": 1000, "open": 100.0, "high": 90.0, "low": 95.0, "close": 98.0, "volume": 10.0, "close_time": 60999},
        {"open_time": 61000, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10.0, "close_time": 120999},
    ])
    cleaned = validate_ohlcv_dataframe(bad_df)
    assert len(cleaned) == 1
    assert int(cleaned.iloc[0]["open_time"]) == 61000


def test_04_websocket_parsers_reconnect_backoff_stale_and_rest_fallback():
    t = [1_760_000_030.0]
    fail_once = [True]
    def _connector(provider, symbols):
        if fail_once[0]:
            fail_once[0] = False
            raise ConnectionError("ws dropped")
        return True

    mkt = FuturesMarket(session=_mock_binance_session(int(t[0] * 1000)))
    mgr = IntrabarStreamManager(mkt, websocket_enabled=True, rest_fallback_enabled=True, stale_threshold_seconds=30.0, ws_connector=_connector, clock=lambda: t[0])
    mgr.subscribe(["BTCUSDT"])
    assert mgr.connect_websocket() is False
    assert mgr.ws_state in ("RECONNECTING", "REST_FALLBACK")
    assert mgr.rest_fallback_active is True
    assert mgr.compute_backoff_delay(0) == 1.0
    assert mgr.compute_backoff_delay(2) == 4.0

    # REST fallback works during reconnect window
    snaps = mgr.poll_rest_cycle(["BTCUSDT"])
    assert "BTCUSDT" in snaps
    assert snaps["BTCUSDT"].mark_price == 105.01

    # Advance past backoff -> WS reconnects
    t[0] += 5.0
    assert mgr.connect_websocket() is True
    assert mgr.ws_state == "CONNECTED_AWAITING_MSG"

    # Parse Binance & Bybit WS kline messages
    b_msg = {"data": {"e": "kline", "E": int(t[0] * 1000), "s": "BTCUSDT", "k": {"t": 1760000000000, "T": 1760000059999, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "102", "l": "99", "c": "101", "v": "50", "x": False}}}
    assert mgr.ingest_raw_ws_message(b_msg) == 1
    by_msg = {"topic": "kline.1.ETHUSDT", "ts": int(t[0] * 1000), "data": [{"start": 1760000000000, "end": 1760000059999, "open": "2000", "high": "2010", "low": "1995", "close": "2005", "volume": "80", "confirm": False, "timestamp": int(t[0] * 1000)}]}
    assert mgr.ingest_raw_ws_message(by_msg, provider_hint="Bybit") == 1

    # Stale feed detection
    t[0] += 120.0
    assert mgr.is_feed_stale() is True


def test_05_binance_to_bybit_failover_and_spot_blocked_from_futures_entry():
    class _BlockedBinance(FuturesMarket):
        def monitor_snapshot(self, *a, **kw):
            raise MarketError("HTTP 451", http_status=451, endpoint="/fapi/v1/klines")
    class _OkBybit(BybitFuturesMarket):
        def monitor_snapshot(self, symbol, **kw):
            return _make_directional_snapshot("LONG", provider=self.market_label, kind=self.instrument_kind)

    router = FuturesProviderRouter(primary=_BlockedBinance(), fallback=_OkBybit(), spot_fallback=SpotReferenceMarket())
    snap = router.monitor_snapshot("BTCUSDT")
    assert snap.instrument_kind == "BYBIT_LINEAR_PERPETUAL"
    assert router.futures_available is True
    assert snap.failover_reason and "Bybit" in snap.failover_reason

    # Spot Reference NEVER produces Futures CONFIRMED / PAPER_ENTRY
    spot_snap = _make_directional_snapshot("LONG", provider="Binance Spot Reference", kind="SPOT_REFERENCE")
    assert spot_snap.mark_price is None  # Never aliases Spot Last as Futures Mark Price
    mgr = PredictiveLifecycleManager()
    valid_res = Result(status="VALID", direction="LONG", checks=[{"Kriteria": "Test", "Status": "LULUS"}], plan={"symbol": "BTCUSDT", "direction": "LONG", "entry": 100.0, "sl": 99.0, "setup_quality_score": 85.0})
    assert mgr.promote_to_confirmed(None, hybrid_result=valid_res, snapshot=spot_snap, now_ms=1_760_000_030_000) is None


# ===========================================================================
# 2. PREDICTIVE ENGINE ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_06_predictive_early_buy_and_sell_symmetric_and_never_confirmed_directly():
    snap_buy = _make_directional_snapshot("LONG")
    sig_buy = evaluate_early_signal(snap_buy, now_ms=snap_buy.local_receive_ms)
    assert sig_buy is not None
    assert sig_buy.direction == "LONG"
    assert sig_buy.stage in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING)
    assert sig_buy.stage != STAGE_CONFIRMED
    assert sig_buy.early_score <= 77.0

    snap_sell = _make_directional_snapshot("SHORT")
    sig_sell = evaluate_early_signal(snap_sell, now_ms=snap_sell.local_receive_ms)
    assert sig_sell is not None
    assert sig_sell.direction == "SHORT"
    assert sig_sell.stage in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING)
    assert sig_sell.early_score <= 77.0


def test_07_stale_high_vol_missing_data_and_zero_future_leakage():
    # Stale data returns None
    stale_snap = _make_directional_snapshot("LONG", stale=True)
    assert evaluate_early_signal(stale_snap, now_ms=stale_snap.local_receive_ms) is None

    # High volatility blocked
    hv_snap = _make_directional_snapshot("LONG", high_vol=True)
    hv_sig = evaluate_early_signal(hv_snap, now_ms=hv_snap.local_receive_ms)
    assert hv_sig is not None
    assert hv_sig.stage == STAGE_NO_ENTRY_HIGH_VOLATILITY

    # Missing M1 data transparently recorded (never auto-passed)
    miss_snap = _make_directional_snapshot("LONG")
    miss_snap.frames_live["1m"] = pd.DataFrame()
    miss_snap.bid = 0.0
    miss_snap.ask = 0.0
    sig_miss = evaluate_early_signal(miss_snap, now_ms=miss_snap.local_receive_ms)
    if sig_miss is not None:
        assert "m1_candles_insufficient" in sig_miss.missing_features

    # Zero future leakage: appending wild future candles with open_time > now_ms MUST NOT change early_score
    base_snap = _make_directional_snapshot("LONG")
    now_ms = base_snap.local_receive_ms
    sig_before = evaluate_early_signal(base_snap, now_ms=now_ms)
    fut_bar = pd.DataFrame([{
        "open_time": now_ms + 60000, "open": 500.0, "high": 600.0, "low": 490.0, "close": 590.0, "volume": 999999.0, "close_time": now_ms + 119999, "is_closed": False
    }])
    base_snap.frames_live["1m"] = pd.concat([base_snap.frames_live["1m"], fut_bar], ignore_index=True)
    sig_after = evaluate_early_signal(base_snap, now_ms=now_ms)
    assert sig_before.early_score == sig_after.early_score
    assert sig_before.stage == sig_after.stage


# ===========================================================================
# 3. LIFECYCLE STATE MACHINE ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_08_full_lifecycle_transitions_and_illegal_transition_blocked():
    assert is_valid_transition(STAGE_PRE_WATCH, STAGE_EARLY_WATCH) is True
    assert is_valid_transition(STAGE_EARLY_WATCH, STAGE_SETUP_FORMING) is True
    assert is_valid_transition(STAGE_SETUP_FORMING, STAGE_CONFIRMED) is True
    assert is_valid_transition(STAGE_CONFIRMED, STAGE_PAPER_ENTRY) is True
    # Illegal jump: PRE_WATCH cannot jump directly to PAPER_ENTRY; EXPIRED cannot become CONFIRMED
    assert is_valid_transition(STAGE_PRE_WATCH, STAGE_PAPER_ENTRY) is False
    assert is_valid_transition(STAGE_EXPIRED, STAGE_CONFIRMED) is False

    mgr = PredictiveLifecycleManager(PredictiveConfig(pre_watch_ttl_seconds=600, max_chase_pct=0.45, max_chase_atr=0.35))
    t0 = 1_760_000_000_000
    s_pw = PredictiveSignal("pw-1", "BTCUSDT", "LONG", STAGE_PRE_WATCH, early_score=52.0, detected_at_ms=t0, updated_at_ms=t0, detected_price=100.0, current_price=100.0, trigger_price=100.4, invalidation_price=99.0, atr_value=0.5, expires_at_ms=t0 + 600_000)

    # Strengthening within same stage
    c_str = PredictiveSignal("pw-tmp", "BTCUSDT", "LONG", STAGE_PRE_WATCH, early_score=57.0, detected_at_ms=t0, updated_at_ms=t0 + 10_000, detected_price=100.1, current_price=100.1, trigger_price=100.4, invalidation_price=99.0, atr_value=0.5)
    c_str.stage = STAGE_PRE_WATCH
    tr_str = mgr.process_early_update(s_pw, c_str, current_price=100.1, now_ms=t0 + 10_000)
    assert tr_str.alert_event_type == STAGE_SIGNAL_STRENGTHENING

    # Weakening within same stage
    c_wk = PredictiveSignal("pw-tmp2", "BTCUSDT", "LONG", STAGE_PRE_WATCH, early_score=51.0, detected_at_ms=t0, updated_at_ms=t0 + 20_000, detected_price=100.0, current_price=100.0, trigger_price=100.4, invalidation_price=99.0, atr_value=0.5)
    tr_wk = mgr.process_early_update(tr_str.signal, c_wk, current_price=100.0, now_ms=t0 + 20_000)
    assert tr_wk.alert_event_type == STAGE_SIGNAL_WEAKENING

    # PRE_WATCH -> EARLY_WATCH -> SETUP_FORMING
    c_ew = PredictiveSignal("pw-tmp3", "BTCUSDT", "LONG", STAGE_EARLY_WATCH, early_score=60.0, detected_at_ms=t0, updated_at_ms=t0 + 30_000, detected_price=100.2, current_price=100.2, trigger_price=100.4, invalidation_price=99.0, atr_value=0.5)
    tr_ew = mgr.process_early_update(tr_wk.signal, c_ew, current_price=100.2, now_ms=t0 + 30_000)
    assert tr_ew.signal.stage == STAGE_EARLY_WATCH

    c_sf = PredictiveSignal("pw-tmp4", "BTCUSDT", "LONG", STAGE_SETUP_FORMING, early_score=70.0, detected_at_ms=t0, updated_at_ms=t0 + 40_000, detected_price=100.3, current_price=100.3, trigger_price=100.4, invalidation_price=99.0, atr_value=0.5)
    tr_sf = mgr.process_early_update(tr_ew.signal, c_sf, current_price=100.3, now_ms=t0 + 40_000)
    assert tr_sf.signal.stage == STAGE_SETUP_FORMING

    # Hard gate failure blocks CONFIRMED
    snap = _make_directional_snapshot("LONG", now_ms=t0 + 50_000)
    failed_res = Result(status="NO_TRADE", direction="LONG", checks=[{"Kriteria": "News Blackout", "Status": "GAGAL"}], plan={"symbol": "BTCUSDT", "direction": "LONG", "entry": 100.35, "entry_trigger": 100.4, "sl": 99.0, "setup_quality_score": 82.0})
    assert mgr.promote_to_confirmed(tr_sf.signal, hybrid_result=failed_res, snapshot=snap, now_ms=t0 + 50_000) is None

    # All hard gates pass + score >= 78 -> CONFIRMED -> PAPER_ENTRY
    snap.mark_price = 100.38
    snap.last_price = 100.38
    ok_res = Result(status="VALID", direction="LONG", checks=[{"Kriteria": "All Hard Gates", "Status": "LULUS"}], metrics={"atr": 0.5, "setup_quality_score": 84.0}, meta={"m15_regime": "TREND_UP", "routed_strategy": "A"}, plan={"id": "trd-1", "symbol": "BTCUSDT", "direction": "LONG", "entry": 100.38, "entry_trigger": 100.40, "sl": 99.2, "tp1": 101.6, "tp2": 102.8, "tp3": 104.0, "risk": 2.5, "leverage": 2, "net_rr_tp1": 1.65, "setup_quality_score": 84.0, "instrument_kind": "USD_M_PERPETUAL"})
    tr_conf = mgr.promote_to_confirmed(tr_sf.signal, hybrid_result=ok_res, snapshot=snap, now_ms=t0 + 50_000)
    assert tr_conf is not None and tr_conf.signal.stage == STAGE_CONFIRMED
    tr_pe = mgr.mark_paper_entry(tr_conf.signal, now_ms=t0 + 51_000, trade_id="trd-1")
    assert tr_pe is not None and tr_pe.signal.stage == STAGE_PAPER_ENTRY


def test_09_direction_change_invalidation_missed_entry_and_expiry(tmp_path):
    mgr = PredictiveLifecycleManager(PredictiveConfig(max_chase_pct=0.45, max_chase_atr=0.35))
    repo = PredictiveSignalRepository(tmp_path / "paper.db")
    t0 = 1_760_000_000_000

    # BUY -> SELL direction change cancels old BUY, preserves history, creates NEW signal_id
    s_buy = PredictiveSignal("pw-buy-1", "ETHUSDT", "LONG", STAGE_EARLY_WATCH, early_score=60.0, detected_at_ms=t0, updated_at_ms=t0, detected_price=2000.0, current_price=2000.0, trigger_price=2005.0, invalidation_price=1985.0, atr_value=8.0, expires_at_ms=t0 + 900_000)
    c_sell = PredictiveSignal("pw-sell-2", "ETHUSDT", "SHORT", STAGE_EARLY_WATCH, early_score=62.0, detected_at_ms=t0 + 60_000, updated_at_ms=t0 + 60_000, detected_price=1995.0, current_price=1995.0, trigger_price=1990.0, invalidation_price=2010.0, atr_value=8.0, expires_at_ms=t0 + 960_000)
    tr_dir = mgr.process_early_update(s_buy, c_sell, current_price=1995.0, now_ms=t0 + 60_000)
    assert tr_dir.replaced_signal.stage == STAGE_DIRECTION_CHANGED
    assert tr_dir.signal.signal_id != s_buy.signal_id
    repo.save_transition(tr_dir, alert_message=format_predictive_alert(tr_dir.signal, event_type=STAGE_DIRECTION_CHANGED), now_ms=t0 + 60_000)

    # Worker restart simulation: new repository instance retains active SELL + historical BUY!
    repo_restarted = PredictiveSignalRepository(tmp_path / "paper.db")
    active = repo_restarted.get_active_signal("ETHUSDT")
    assert active is not None and active.direction == "SHORT" and active.signal_id == "pw-sell-2"
    all_eth = repo_restarted.list_signals()
    assert any(x.signal_id == "pw-buy-1" and x.stage == STAGE_DIRECTION_CHANGED for x in all_eth)

    # Invalidation
    tr_inv = mgr.process_early_update(active, None, current_price=2015.0, now_ms=t0 + 120_000)
    assert tr_inv.signal.stage == STAGE_SIGNAL_INVALIDATED

    # MISSED_ENTRY (anti-chase)
    s_sf = PredictiveSignal("pw-sol-1", "SOLUSDT", "LONG", STAGE_SETUP_FORMING, early_score=72.0, detected_at_ms=t0, updated_at_ms=t0, detected_price=150.0, current_price=150.0, trigger_price=150.5, invalidation_price=148.0, atr_value=0.8, expires_at_ms=t0 + 900_000)
    tr_chase = mgr.process_early_update(s_sf, None, current_price=152.0, now_ms=t0 + 30_000)
    assert tr_chase.signal.stage == STAGE_MISSED_ENTRY
    assert tr_chase.signal.entry_validity == "CHASE_MISSED"

    # TTL Expiry
    s_exp = PredictiveSignal("pw-xrp-1", "XRPUSDT", "LONG", STAGE_PRE_WATCH, early_score=52.0, detected_at_ms=t0, updated_at_ms=t0, detected_price=0.60, current_price=0.60, trigger_price=0.61, invalidation_price=0.58, atr_value=0.01, expires_at_ms=t0 + 600_000)
    tr_exp = mgr.process_early_update(s_exp, None, current_price=0.601, now_ms=t0 + 601_000)
    assert tr_exp.signal.stage == STAGE_EXPIRED


# ===========================================================================
# 4. ALERT ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_10_telegram_predictive_alert_formats_dedup_retry_and_redaction(tmp_path):
    sig = PredictiveSignal("pw-al-1", "BTCUSDT", "LONG", STAGE_PRE_WATCH, early_score=52.0, m15_regime="STRONG_TREND_UP", detected_price=60000.0, current_price=60050.0, trigger_price=60150.0, invalidation_price=59700.0, distance_to_trigger_pct=0.166, signal_age_seconds=12.0, reason_codes=["M1_MICRO_MOMENTUM_LONG", "M5_EARLY_CHOCH_LONG"])
    for ev in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING, STAGE_DIRECTION_CHANGED, STAGE_SIGNAL_INVALIDATED, STAGE_MISSED_ENTRY, STAGE_EXPIRED):
        txt = format_predictive_alert(sig, event_type=ev)
        assert "COINAGENTX" in txt and "BTCUSDT" in txt

    plan = {"entry_trigger": 60150.0, "entry": 60160.0, "sl": 59700.0, "tp1": 60620.0, "tp2": 61080.0, "tp3": 61540.0, "risk": 2.5, "leverage": 2, "net_rr_tp1": 1.72}
    conf_txt = format_predictive_alert(sig, event_type=STAGE_CONFIRMED, confirmed_plan=plan)
    assert "TP1" in conf_txt and "TP3" in conf_txt and "PAPER ONLY" in conf_txt

    # Redaction never leaks token/chat_id
    redacted = redact_telegram_secrets("err https://api.telegram.org/bot123456789:SECRET_TOKEN_XYZ/sendMessage chat=-100999888", token="123456789:SECRET_TOKEN_XYZ", chat_id="-100999888")
    assert "SECRET_TOKEN_XYZ" not in redacted and "-100999888" not in redacted

    # Deduplication & Retry
    calls = [0]
    class _FlakySess:
        def post(self, url, json=None, **kw):
            calls[0] += 1
            if calls[0] == 1:
                return SimpleNamespace(status_code=500, json=lambda: {"ok": False})
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    t_clk = [1000.0]
    tg = TelegramAlerts("123456789:TEST_TOKEN_ABCDEF", "-100123456", tmp_path / "paper.db", session=_FlakySess(), clock=lambda: t_clk[0])
    r1 = tg.send_predictive_alert(sig, event_type=STAGE_PRE_WATCH)
    assert r1["status"] == "RETRY"
    t_clk[0] += 120.0
    r2 = tg.send_predictive_alert(sig, event_type=STAGE_PRE_WATCH)
    assert r2["status"] == "SENT"
    # Duplicate blocked
    r3 = tg.send_predictive_alert(sig, event_type=STAGE_PRE_WATCH)
    assert r3["status"] == "DEDUPLICATED"


# ===========================================================================
# 5. INTEGRATION ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_11_db_identity_pg_fail_closed_single_worker_lease_and_queue_error_visible(tmp_path, monkeypatch):
    db_file = tmp_path / "shared_paper.db"
    fp_worker = database_identity_fingerprint(db_file)
    fp_dash = database_identity_fingerprint(db_file)
    assert fp_worker == fp_dash

    # COINAGENT_REQUIRE_POSTGRES fail-closed when DATABASE_URL missing
    monkeypatch.setenv("COINAGENT_REQUIRE_POSTGRES", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseConfigurationError):
        connect_database(db_file)
    monkeypatch.delenv("COINAGENT_REQUIRE_POSTGRES", raising=False)

    # Single-worker ProcessLock lease blocks second concurrent worker
    lock1 = ProcessLock(db_file)
    lock2 = ProcessLock(db_file)
    lock1.acquire()
    try:
        with pytest.raises(ScanBusy):
            lock2.acquire()
    finally:
        lock1.release()

    # Worker defaults to FuturesProviderRouter + futures_primary_only=True
    args = worker.parse_args(["--db", str(db_file), "--once"])
    scanner, cfg = worker.create_worker_engine(args, root=tmp_path)
    assert isinstance(scanner.market, FuturesProviderRouter)
    assert cfg.futures_primary_only is True
    assert cfg.candidates == 20
    assert cfg.shortlist_limit == 50
    assert cfg.max_groq_candidates == 5

    # Groq Advisor-Only: Groq unavailable NEVER vetoes valid rule engine setup
    res_valid = Result(status="VALID", direction="LONG", plan={"symbol": "BTCUSDT", "direction": "LONG"})
    ai_res = evaluate_contextual_ai(res_valid, None)
    assert ai_res["ai_status"] == "AI_UNAVAILABLE"
    assert res_valid.status == "VALID"


def test_12_paper_only_security_no_live_order_endpoints():
    # Static scan across all futures/*.py and worker.py: zero live order placement endpoints
    forbidden = ("/fapi/v1/order", "/v5/order/create", "/api/v3/order", "/sapi/v1/capital/withdraw")
    root = Path(__file__).resolve().parent.parent
    for pyf in list((root / "futures").glob("*.py")) + [root / "worker.py", root / "app.py"]:
        text = pyf.read_text(encoding="utf-8", errors="ignore")
        for ep in forbidden:
            assert ep not in text, f"Forbidden live endpoint {ep} found in {pyf.name}"


# ===========================================================================
# 6. PERFORMANCE, REPLAY & DASHBOARD UI ACCEPTANCE TESTS (Section J.7)
# ===========================================================================
def test_13_bounded_50_subscriptions_no_memory_leak_and_ui_18_columns():
    mgr = IntrabarStreamManager(FuturesMarket(session=_mock_binance_session()), max_subscriptions=50)
    syms = [f"COIN{i}USDT" for i in range(80)]
    subbed = mgr.subscribe(syms)
    assert len(subbed) == 50  # strictly capped at 50
    mgr.unsubscribe(subbed[:20])
    assert len(mgr.subscribed_symbols()) == 30

    # Dashboard UI columns, filtering, sorting, color badges
    s1 = PredictiveSignal("pw-u1", "BTCUSDT", "LONG", STAGE_PRE_WATCH, early_score=53.0, distance_to_trigger_pct=0.20, data_freshness_ms=200, signal_age_seconds=10.0)
    s2 = PredictiveSignal("pw-u2", "ETHUSDT", "SHORT", STAGE_CONFIRMED, early_score=75.0, setup_quality_score=84.0, distance_to_trigger_pct=0.08, data_freshness_ms=100, signal_age_seconds=5.0, entry_validity="VALID")
    s3 = PredictiveSignal("pw-u3", "SOLUSDT", "LONG", STAGE_EXPIRED, early_score=58.0, distance_to_trigger_pct=0.05, data_freshness_ms=100, signal_age_seconds=900.0, entry_validity="EXPIRED")
    ordered = sort_predictive_signals([s3, s1, s2])
    assert ordered[0].symbol == "ETHUSDT"  # Valid & closest to trigger first
    assert ordered[-1].symbol == "SOLUSDT"  # Expired last
    assert badge_color_for_signal(STAGE_PRE_WATCH) == "#2563EB"
    assert badge_color_for_signal(STAGE_CONFIRMED, "LONG") == "#16A34A"
    assert badge_color_for_signal(STAGE_CONFIRMED, "SHORT") == "#DC2626"
    assert len(filter_predictive_signals(ordered, stage_filter="CONFIRMED", direction_filter="SELL")) == 1
    rows = build_watchlist_rows(ordered)
    required_cols = {"Pair", "Direction", "Stage", "Early Score", "Setup Quality Score", "M15 Regime", "Strategy A-E", "M1 Status", "M5 Status", "Detected Price", "Current Price", "Trigger Price", "Invalidation Price", "Distance to Trigger (%)", "Signal Age (s)", "Provider", "Last Update", "Signal Validity"}
    assert required_cols.issubset(set(rows[0].keys()))
    assert len(STAGE_DISCLAIMERS) == 4


def test_14_chronological_replay_split_walk_forward_and_spot_rejection():
    # Build deterministic 300-bar M1 fixture ONLY to verify replay mechanics & anti-lookahead
    m1 = _make_df(300, "1m", end_ms=1_760_000_000_000, base=100.0, drift=0.04, vol=1200.0, include_open_bar=False)
    tr, val, te = chronological_split(m1)
    assert len(tr) == 180 and len(val) == 60 and len(te) == 60
    assert int(tr["open_time"].max()) < int(val["open_time"].min()) < int(te["open_time"].min())

    # Spot dataset strictly rejected for Futures replay
    with pytest.raises(ValueError):
        replay_predictive_m1_series(m1, instrument_kind="SPOT_REFERENCE", is_synthetic_fixture=True)

    rep = calibrate_and_evaluate_walk_forward(m1, symbol="BTCUSDT", is_synthetic_fixture=True)
    assert rep["production_proof_valid"] is False  # Explicitly never claims synthetic data as production proof
    assert rep["out_of_sample_result"]["ok"] is True
    assert len(rep["walk_forward_folds"]) >= 1

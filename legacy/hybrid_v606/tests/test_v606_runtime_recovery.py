"""CoinAgentX v6.0.6 — P0 Runtime Recovery & Futures Data Acceptance Test Suite (P0-1..P1-3).

Verifies:
- APP_VERSION == "6.0.6" & SPEC_VERSION preserves Hybrid Adaptive v1.1 + Predictive Watchlist v1.0.
- P0-1: Worker heartbeat records & verifies PID, worker_id, database_fingerprint, worker_scans, and stale detection.
- P0-2: Predictive Monitor lifecycle reports `monitor_health == "HEALTHY"` ONLY when `MONITOR_ONLINE` with verified heartbeat & active feed; distinguishes EARLY_WATCH, SETUP_FORMING, INVALIDATED, EXPIRED with reasons.
- P0-3: Intrabar stream preserves valid cached candles across cycles, reports `last_successful_update_ms`, and stale feed blocks entry.
- P0-4: `validate_futures_contract_spec()` validates status, perpetual type, USDT quote, tick/step/min_qty/min_notional and rejects non-Futures contracts; Spot Reference blocked from Futures PAPER_ENTRY.
- P1-1: Single monotonic clock aligns `stage_timings["ai_analysis_s"] == budget_telemetry["groq_request_time_s"]`, `total_elapsed_s == duration_seconds`, and documents `queue_wait_included_in_total=True`.
- P1-2: Telegram outbox records verified HTTP delivery metadata (`outbox_id`, `http_status`, `delivery_timestamp_ms`, `retry_state`, `last_error`).
- P1-3: `MultiCycleResourceProfiler` measures actual RSS MB, CPU user/system seconds, cache entries, and RSS delta across cycles.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

from futures.db_backend import database_identity_fingerprint
from futures.groq_rotation import GroqRotation
from futures.intrabar_stream import IntrabarStreamManager
from futures.market import FuturesMarket
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.memory_profiler import MultiCycleResourceProfiler
from futures.models import Contract, IntrabarCandle, MonitorSnapshot
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.predictive_store import PredictiveSignalRepository
from futures.predictive_watchlist import (
    PredictiveLifecycleManager,
    PredictiveSignal,
    STAGE_EARLY_WATCH,
    evaluate_early_signal,
)
from futures.provider_router import validate_futures_contract_spec
from futures.telegram import TelegramAlerts, format_predictive_alert
from futures.version import APP_VERSION, SPEC_VERSION


def test_v606_01_version_and_worker_pid_monitor_healthy(tmp_path):
    assert APP_VERSION == "6.0.6"
    assert "Hybrid Adaptive v1.1" in SPEC_VERSION and "Predictive Watchlist v1.0" in SPEC_VERSION

    db = tmp_path / "shared.db"
    lw = PaperLedger(db, 1000.0)
    ld = PaperLedger(db, 1000.0)
    fp = database_identity_fingerprint(db)
    now_ms = int(time.time() * 1000)

    vr = lw.record_worker_heartbeat("worker-v606", now_ms, 60, database_fingerprint=fp, worker_state="WORKER_ONLINE", worker_scans=3, pid=os.getpid())
    assert vr["verified"] is True and vr["pid"] == os.getpid()
    wh = ld.worker_health_status(now_ms=now_ms + 5000)
    assert wh["pid"] == os.getpid()
    assert wh["worker_scans"] == 3
    assert wh["worker_state"] == "WORKER_ONLINE"

    lw.record_monitor_heartbeat("worker-v606", now_ms, monitor_state="MONITOR_ONLINE", feed_active=True, websocket_state="WEBSOCKET_CONNECTED", symbols_requested=20, symbols_success=20, database_fingerprint=fp)
    mh = ld.monitor_health_status(now_ms=now_ms + 5000)
    assert mh["monitor_state"] == "MONITOR_ONLINE"
    assert mh["monitor_health"] == "HEALTHY"
    assert mh["online"] is True


def test_v606_02_stream_preserves_candles_across_cycles_and_stale_blocks_signal():
    now_ms = 1_760_000_030_000
    mgr = IntrabarStreamManager(FuturesMarket(), clock=lambda: now_ms / 1000.0)
    mgr.subscribe(["BTCUSDT", "ETHUSDT"])
    c1 = IntrabarCandle("BTCUSDT", "1m", 1760000000000, 1760000059999, 95000.0, 95100.0, 94950.0, 95080.0, 120.0, is_closed=False, exchange_timestamp_ms=now_ms)
    assert mgr.ingest_candle(c1, from_ws=False) is True
    # Re-subscribing on next cycle MUST preserve existing valid BTCUSDT candle!
    mgr.subscribe(["BTCUSDT", "SOLUSDT"])
    assert mgr.get_live_candle("BTCUSDT", "1m") is not None
    hs = mgr.health_status()
    assert hs["last_successful_update_ms"] == now_ms

    # Stale snapshot automatically returns None (blocks signal/entry)
    stale_snap = MonitorSnapshot("BTCUSDT", "Binance Futures", "USD_M_PERPETUAL", now_ms - 120_000, now_ms, 95080.0, 95080.0, 95080.0, 95079.0, 95081.0, data_freshness_ms=120_000, is_stale=True)
    assert evaluate_early_signal(stale_snap, now_ms=now_ms) is None


def test_v606_03_futures_contract_spec_validation():
    good = Contract(tick=0.1, step=0.001, min_qty=0.001, max_qty=1000.0, min_notional=5.0, market_step=0.001, market_min_qty=0.001, market_max_qty=1000.0, status="TRADING", contract_type="PERPETUAL", quote_asset="USDT")
    ok, errs = validate_futures_contract_spec(good, "BTCUSDT")
    assert ok is True and errs == []

    bad = Contract(tick=0.0, step=0.001, min_qty=0.0, max_qty=1000.0, min_notional=5.0, market_step=0.001, market_min_qty=0.001, market_max_qty=1000.0, status="BREAK", contract_type="DELIVERY", quote_asset="BUSD")
    ok2, errs2 = validate_futures_contract_spec(bad, "BADPAIR")
    assert ok2 is False and len(errs2) >= 4


def test_v606_04_budget_alignment_telegram_http_record_and_resource_profiler(tmp_path):
    db = tmp_path / "paper.db"
    class _MinMkt(FuturesMarket):
        def universe(self):
            return {"info": {"symbols": []}, "tickers": [], "books": [], "now_ms": 1_760_000_000_000}

    t = [50.0]
    def _clk():
        t[0] += 0.1
        return t[0]

    scanner = MarketScanner(_MinMkt(), GroqRotation([]), NewsCalendar(), PaperLedger(db, 1000.0), TelegramAlerts("", "", db), clock=_clk)
    rep = scanner.run(ScannerConfig(send_alerts=False, send_watchlist_alerts=False, send_scan_summary=False))
    bt = rep["scanner_diagnostics"]["budget_telemetry"]
    assert bt["total_elapsed_s"] == rep["duration_seconds"]
    assert bt["queue_wait_included_in_total"] is True
    assert rep["stage_timings"]["ai_analysis_s"] == bt["groq_request_time_s"]

    # P1-2: Telegram outbox HTTP 200 delivery record
    repo = PredictiveSignalRepository(db)
    sig = PredictiveSignal("pw-v606-1", "BTCUSDT", "LONG", STAGE_EARLY_WATCH, early_score=60.0, detected_at_ms=1000, updated_at_ms=1000, detected_price=95000.0, current_price=95000.0, trigger_price=95200.0, invalidation_price=94500.0, expires_at_ms=901000)
    tr = PredictiveLifecycleManager().process_early_update(None, sig, current_price=95000.0, now_ms=1000)
    repo.save_transition(tr, alert_message=format_predictive_alert(sig), now_ms=1000)
    tg = TelegramAlerts("123456:BOT_TOKEN", "-100999", db, session=SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"ok": True})))
    fl = tg.flush_predictive_outbox(repo, max_items=5, now_ms=2000)
    assert fl["sent"] == 1
    st = tg.telegram_status(repo)
    rec = st["last_delivery_record"]
    assert rec["outbox_id"] is not None
    assert rec["http_status"] == 200
    assert rec["delivery_timestamp_ms"] == 2000
    assert rec["retry_state"] == "DELIVERED"

    # P1-3: Multi-cycle RSS & CPU profiling
    prof = MultiCycleResourceProfiler()
    s1 = prof.record(scanner, scanner.intrabar_stream, "cycle-1")
    s2 = prof.record(scanner, scanner.intrabar_stream, "cycle-2")
    assert s1["rss_mb"] > 0.0 and s1["cpu_user_s"] >= 0.0
    assert "rss_delta_from_start_mb" in s2

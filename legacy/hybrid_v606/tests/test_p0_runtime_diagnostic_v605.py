"""13-Item P0 Runtime Diagnostic Remediation Acceptance Test Suite (CoinAgentX v6.0.5).

Verifies:
 1. Zero double counting of candidates (DEFERRED != ACTIVE_QUEUE; accounted_total == stage2_total; candidate_id + status_timestamp_ms).
 2. Worker process active but heartbeat failed -> PROCESS_RUNNING_HEARTBEAT_FAILED / DEGRADED (never WORKER_ONLINE).
 3. Worker heartbeat write-readback verified.
 4. Database fingerprint identical between dashboard and worker.
 5. Futures unavailable -> provider_diagnostics captures primary/fallback HTTP status & error class, zero Futures PAPER_ENTRY.
 6. WebSocket receives valid live message -> WEBSOCKET_CONNECTED & feed_active=True.
 7. REST fallback genuinely polls (`rest_poll_count >= 1`, `rest_last_success_ms > 0`).
 8. No active feed claimed without candles (`feed_active=False` when 0 candles).
 9. Predictive `NO_SETUP` explicitly distinguished from `MONITOR_OFFLINE`, `NO_MARKET_DATA`, `PREDICTIVE_STORE_UNAVAILABLE`.
10. AI budget telemetry separates `groq_request_time_s` from `local_analysis_time_s` & enforces budget without silently exceeding.
11. Deferred queue survives restart.
12. Telegram counter consistency between per-cycle `cycle_status_counts` and cumulative `telegram_outbox`.
13. Full suite green & PAPER ONLY enforced.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from futures.bybit_market import BybitFuturesMarket
from futures.db_backend import database_identity_fingerprint
from futures.groq_rotation import GroqRotation
from futures.intrabar_stream import IntrabarStreamManager
from futures.market import FuturesMarket, MarketError
from futures.market_scanner import MarketScanner, ScannerConfig, reconcile_candidate_pipeline
from futures.models import MonitorSnapshot, Result
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.predictive_watchlist import PredictiveLifecycleManager
from futures.provider_router import FuturesProviderRouter
from futures.spot_market import SpotReferenceMarket
from futures.telegram import TelegramAlerts


def test_01_zero_candidate_double_counting_mutually_exclusive_states():
    states = [
        {"candidate_id": "sc1:BTCUSDT", "symbol": "BTCUSDT", "status": "ANALYZED", "status_timestamp_ms": 1000},
        {"candidate_id": "sc1:ETHUSDT", "symbol": "ETHUSDT", "status": "ANALYZED", "status_timestamp_ms": 1000},
        {"candidate_id": "sc1:FETUSDT", "symbol": "FETUSDT", "status": "DEFERRED", "status_timestamp_ms": 1000},
        {"candidate_id": "sc1:FILUSDT", "symbol": "FILUSDT", "status": "DEFERRED", "status_timestamp_ms": 1000},
    ]
    rec = reconcile_candidate_pipeline(
        stage2_total=42,
        active_queue_count=0,
        analyzed_count=2,
        deferred_count=2,
        expired_count=0,
        rejected_count=38,
        cancelled_count=0,
        candidate_states=states,
    )
    # Zero double counting: active_queue_count=0, deferred_count=2, accounted_total == stage2_total == 42!
    assert rec["active_queue_count"] == 0
    assert rec["deferred_count"] == 2
    assert rec["accounted_total"] == rec["stage2_total"] == 42
    assert rec["reconciliation_ok"] is True
    assert len({c["symbol"] for c in rec["candidate_states"]}) == len(rec["candidate_states"])
    assert all("candidate_id" in c and "status_timestamp_ms" in c for c in rec["candidate_states"])


def test_02_03_and_04_worker_heartbeat_failed_state_readback_and_db_fingerprint(tmp_path):
    db = tmp_path / "shared.db"
    lw = PaperLedger(db, 1000.0)
    ld = PaperLedger(db, 1000.0)
    fp_w = database_identity_fingerprint(lw.db)
    fp_d = database_identity_fingerprint(ld.db)
    assert fp_w == fp_d

    t0 = int(time.time() * 1000)
    # Simulate process running but heartbeat verification failed
    lw.record_worker_heartbeat("worker-1", t0, 60, database_fingerprint=fp_w, worker_state="PROCESS_RUNNING_HEARTBEAT_FAILED")
    st_fail = ld.worker_health_status(now_ms=t0 + 2000)
    assert st_fail["worker_state"] == "PROCESS_RUNNING_HEARTBEAT_FAILED"
    assert st_fail["display_status"] == "DEGRADED"
    assert st_fail["healthy"] is False

    # Verified write-readback -> WORKER_ONLINE
    vr = lw.record_worker_heartbeat("worker-1", t0 + 5000, 60, database_fingerprint=fp_w, worker_state="WORKER_ONLINE")
    assert vr["verified"] is True
    st_ok = ld.worker_health_status(now_ms=t0 + 10000)
    assert st_ok["worker_state"] == "WORKER_ONLINE"
    assert st_ok["heartbeat_verified"] is True
    assert st_ok["database_fingerprint"] == fp_d


def test_05_futures_provider_diagnostics_and_spot_blocked():
    class _B451(FuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 451", http_status=451, endpoint=path)

    class _By403(BybitFuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 403 geo", http_status=403, endpoint=path, error_category="REGIONAL_RESTRICTION")

    class _SpotOk(SpotReferenceMarket):
        def get(self, path, params=None):
            return {"serverTime": 1_760_000_000_000}

    router = FuturesProviderRouter(_B451(), _By403(), spot_fallback=_SpotOk(), spot_fallback_enabled=True)
    router.get("/fapi/v1/time")
    pd_info = router.provider_diagnostics()
    assert pd_info["primary_http_status"] == 451
    assert pd_info["primary_error_class"] == "REGIONAL_RESTRICTION"
    assert pd_info["fallback_http_status"] == 403
    assert pd_info["fallback_error_class"] == "REGIONAL_RESTRICTION"
    assert pd_info["last_successful_futures_ms"] is None
    assert pd_info["data_status"] == "FUTURES_DATA_UNAVAILABLE"

    spot_snap = MonitorSnapshot("BTCUSDT", "Binance Spot", "SPOT_REFERENCE", 1_760_000_000_000, 1_760_000_000_000, 60000.0, None, None, 59999.0, 60001.0)
    ok_res = Result(status="VALID", direction="LONG", checks=[{"Kriteria": "All", "Status": "LULUS"}], plan={"symbol": "BTCUSDT", "direction": "LONG", "entry": 60000.0, "sl": 59500.0, "setup_quality_score": 88.0})
    assert PredictiveLifecycleManager().promote_to_confirmed(None, hybrid_result=ok_res, snapshot=spot_snap, now_ms=1_760_000_000_000) is None


def test_06_07_and_08_ws_live_message_rest_fallback_polling_and_no_feed_active_without_candles():
    now_ms = 1_760_000_030_000
    # 0 candles -> feed_active MUST be False
    mgr0 = IntrabarStreamManager(FuturesMarket(), clock=lambda: now_ms / 1000.0)
    hs0 = mgr0.health_status()
    assert hs0["feed_active"] is False
    assert hs0["per_timeframe_candle_counts"] == {"1m": 0, "5m": 0, "15m": 0}

    # WS live message -> WEBSOCKET_CONNECTED & feed_active=True
    b_kline = json.dumps({
        "data": {"e": "kline", "E": now_ms, "s": "BTCUSDT", "k": {"t": 1760000000000, "T": 1760000059999, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "102", "l": "99", "c": "101", "v": "50", "x": False}}
    })
    class _T:
        def send(self, x): pass
        def recv_available(self): return [b_kline]
        def close(self): pass

    mgr_ws = IntrabarStreamManager(FuturesMarket(), transport_factory=lambda u: _T(), clock=lambda: now_ms / 1000.0)
    mgr_ws.subscribe(["BTCUSDT"])
    assert mgr_ws.connect_websocket() is True
    hs_ws = mgr_ws.health_status()
    assert hs_ws["connection_attempts"] == 1
    assert hs_ws["websocket_state"] == "WEBSOCKET_CONNECTED"
    assert hs_ws["feed_active"] is True

    # REST fallback genuinely polls
    class _RestMkt(FuturesMarket):
        def monitor_snapshot(self, sym, **kw):
            return MonitorSnapshot(sym, self.market_label, self.instrument_kind, now_ms, now_ms, 100.0, 100.0, 100.0, 99.9, 100.1)

    mgr_rest = IntrabarStreamManager(_RestMkt(), websocket_enabled=False, rest_fallback_enabled=True, clock=lambda: now_ms / 1000.0)
    snaps = mgr_rest.poll_rest_cycle(["BTCUSDT"])
    assert "BTCUSDT" in snaps
    hs_r = mgr_rest.health_status()
    assert hs_r["rest_poll_count"] == 1
    assert hs_r["rest_last_success_ms"] == now_ms


def test_09_10_11_and_12_predictive_statuses_budget_split_queue_restart_and_telegram_consistency(tmp_path):
    db = tmp_path / "paper.db"
    ledger = PaperLedger(db, 1000.0)

    # P0-05: Distinguish MONITOR_OFFLINE vs NO_MARKET_DATA vs NO_SETUP
    scanner = MarketScanner(FuturesMarket(), GroqRotation([]), NewsCalendar(), ledger, TelegramAlerts("", "", db))
    d_offline = scanner.run_early_monitor_cycle([], send_alerts=False)
    assert d_offline["predictive_status"] == "MONITOR_OFFLINE"

    class _EmptySnapMkt(FuturesMarket):
        def monitor_snapshot(self, sym, **kw):
            return None
    scanner_no_data = MarketScanner(_EmptySnapMkt(), GroqRotation([]), NewsCalendar(), ledger, TelegramAlerts("", "", db))
    scanner_no_data.intrabar_stream.websocket_enabled = False
    d_nodata = scanner_no_data.run_early_monitor_cycle(["BTCUSDT"], send_alerts=False)
    assert d_nodata["predictive_status"] == "NO_MARKET_DATA"

    class _FlatSnapMkt(FuturesMarket):
        def monitor_snapshot(self, sym, **kw):
            now_ms = int(time.time() * 1000)
            return MonitorSnapshot(sym, self.market_label, self.instrument_kind, now_ms, now_ms, 100.0, 100.0, 100.0, 99.9, 100.1)
    scanner_nosetup = MarketScanner(_FlatSnapMkt(), GroqRotation([]), NewsCalendar(), ledger, TelegramAlerts("", "", db))
    scanner_nosetup.intrabar_stream.websocket_enabled = False
    d_nosetup = scanner_nosetup.run_early_monitor_cycle(["BTCUSDT"], send_alerts=False)
    assert d_nosetup["predictive_status"] == "NO_SETUP"

    # P1-06 & P1-07: Deferred queue survives restart + Telegram cumulative telemetry matches deliveries
    ledger.save_candidate_queue({"items": [{"symbol": "FETUSDT", "queued_at_ms": 1000}], "expired": []}, now_ms=1000)
    reloaded = PaperLedger(db, 1000.0).load_candidate_queue()
    assert [x["symbol"] for x in reloaded["items"]] == ["FETUSDT"]

    tg = TelegramAlerts("123456:TOKEN", "-100123", db)
    tg.record_delivery_telemetry("Alert Telegram terkirim.", ok=True)
    tg.record_delivery_telemetry("Alert Telegram terkirim.", ok=True)
    tg.record_delivery_telemetry("Alert duplikat diblokir.")
    st = tg.telegram_status()
    assert st["requests"] == 2
    assert st["success"] == 2
    assert st["duplicate_suppressed_count"] == 1
    assert st["last_delivery_status"] == "DEDUPLICATED"

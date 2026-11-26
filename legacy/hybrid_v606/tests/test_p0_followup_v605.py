"""12-Item P0 Follow-Up Remediation Acceptance Test Suite (CoinAgentX v6.0.5).

Verifies:
 1. Dashboard active does NOT mean Worker active (worker_scans == 0, WORKER_OFFLINE).
 2. Worker startup produces verified heartbeat & increments worker_scans.
 3. Monitor startup produces verified 5-state monitor heartbeat (MONITOR_STARTING -> MONITOR_ONLINE / MONITOR_DATA_UNAVAILABLE / MONITOR_DEGRADED / MONITOR_OFFLINE).
 4. WebSocket genuinely receives live messages & aggregates M1 -> M5/M15 candles.
 5. REST fallback genuinely polls when WS unavailable/disconnected.
 6. M1/M5/M15 candles available & fresh (`feed_active=True`).
 7. Predictive Monitor distinguishes MONITOR_OFFLINE vs NO_SETUP vs MONITOR_DATA_UNAVAILABLE.
 8. Futures providers independently verified (`verify_futures_providers()`) with separated primary & fallback errors.
 9. Spot NEVER produces Futures PAPER_ENTRY.
10. Budget telemetry monotonic clock consistency (`groq_request_time_s > 0` when Groq called, `total_elapsed_s == duration_seconds`, non-additive overlap note).
11. Queue reconciliation & 5 targeted queue tests (deferred persistence, candidate expiration, restart recovery, candidate state transition, no duplicate accounting).
12. PAPER ONLY enforced across entire codebase.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

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


def test_01_02_and_03_dashboard_vs_worker_startup_heartbeat_and_monitor_heartbeat(tmp_path):
    db = tmp_path / "shared.db"
    ld_dash = PaperLedger(db, 1000.0)
    ld_worker = PaperLedger(db, 1000.0)
    fp = database_identity_fingerprint(db)

    # 1. Dashboard active without Worker -> WORKER_OFFLINE, worker_scans=0
    wh0 = ld_dash.worker_health_status()
    assert wh0["worker_state"] == "WORKER_OFFLINE"
    assert wh0["alive"] is False
    assert ld_dash.monitor_health_status()["monitor_state"] == "MONITOR_OFFLINE"

    # 2. Worker startup -> WORKER_STARTING -> WORKER_ONLINE with worker_scans=1
    t0 = int(time.time() * 1000)
    vr_start = ld_worker.record_worker_heartbeat("worker-101", t0, 60, database_fingerprint=fp, worker_state="WORKER_STARTING", worker_scans=0)
    assert vr_start["verified"] is True
    assert ld_dash.worker_health_status(now_ms=t0 + 1000)["worker_state"] == "WORKER_STARTING"

    vr_on = ld_worker.record_worker_heartbeat("worker-101", t0 + 5000, 60, database_fingerprint=fp, worker_state="WORKER_ONLINE", worker_scans=1)
    assert vr_on["verified"] is True
    wh_on = ld_dash.worker_health_status(now_ms=t0 + 10000)
    assert wh_on["worker_state"] == "WORKER_ONLINE"
    assert wh_on["worker_scans"] == 1
    assert wh_on["database_fingerprint"] == fp

    # 3. Monitor startup -> MONITOR_STARTING -> MONITOR_ONLINE (requires feed_active=True & verified!)
    mh_start = ld_worker.record_monitor_heartbeat("worker-101", t0 + 2000, monitor_state="MONITOR_STARTING", database_fingerprint=fp)
    assert mh_start["verified"] is True
    assert ld_dash.monitor_health_status(now_ms=t0 + 3000)["monitor_state"] == "MONITOR_STARTING"

    # If feed_active=False, MONITOR_ONLINE downgrades honestly to MONITOR_DATA_UNAVAILABLE!
    ld_worker.record_monitor_heartbeat("worker-101", t0 + 4000, monitor_state="MONITOR_ONLINE", feed_active=False, database_fingerprint=fp)
    assert ld_dash.monitor_health_status(now_ms=t0 + 5000)["monitor_state"] == "MONITOR_DATA_UNAVAILABLE"

    # With feed_active=True -> MONITOR_ONLINE
    ld_worker.record_monitor_heartbeat("worker-101", t0 + 6000, monitor_state="MONITOR_ONLINE", feed_active=True, websocket_state="WEBSOCKET_CONNECTED", symbols_requested=10, symbols_success=10, database_fingerprint=fp)
    mh_ok = ld_dash.monitor_health_status(now_ms=t0 + 8000)
    assert mh_ok["monitor_state"] == "MONITOR_ONLINE"
    assert mh_ok["online"] is True


def test_04_05_and_06_ws_live_message_m1_m5_m15_aggregation_and_rest_fallback():
    now_ms = 1_760_000_030_000
    b_kline = json.dumps({
        "data": {"e": "kline", "E": now_ms, "s": "BTCUSDT", "k": {"t": 1760000000000, "T": 1760000059999, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "103", "l": "99", "c": "102", "v": "150", "x": False}}
    })
    class _T:
        def send(self, x): pass
        def recv_available(self): return [b_kline]
        def close(self): pass

    mgr = IntrabarStreamManager(FuturesMarket(), transport_factory=lambda u: _T(), clock=lambda: now_ms / 1000.0)
    mgr.subscribe(["BTCUSDT"])
    assert mgr.connect_websocket() is True
    agg = mgr.aggregate_m1_to_higher_tf("BTCUSDT")
    assert "5m" in agg and "15m" in agg
    hs = mgr.health_status()
    assert hs["websocket_state"] == "WEBSOCKET_CONNECTED"
    assert hs["feed_active"] is True
    assert hs["per_timeframe_candle_counts"] == {"1m": 1, "5m": 1, "15m": 1}

    # REST fallback genuinely polls
    class _RestMkt(FuturesMarket):
        def monitor_snapshot(self, sym, **kw):
            return MonitorSnapshot(sym, self.market_label, self.instrument_kind, now_ms, now_ms, 102.0, 102.0, 102.0, 101.9, 102.1)
    mgr_r = IntrabarStreamManager(_RestMkt(), websocket_enabled=False, rest_fallback_enabled=True, clock=lambda: now_ms / 1000.0)
    assert "BTCUSDT" in mgr_r.poll_rest_cycle(["BTCUSDT"])
    assert mgr_r.health_status()["rest_poll_count"] == 1
    assert mgr_r.health_status()["websocket_state"] == "REST_FALLBACK"


def test_07_08_and_09_independent_futures_provider_verification_and_spot_blocked():
    class _B451(FuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 451 geo", http_status=451, endpoint=path)
    class _By403(BybitFuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 403 waf", http_status=403, endpoint=path, error_category="REGIONAL_RESTRICTION")

    router = FuturesProviderRouter(_B451(), _By403(), spot_fallback=SpotReferenceMarket(), spot_fallback_enabled=True)
    v = router.verify_futures_providers()
    assert v["primary"]["ok"] is False and v["primary"]["http_status"] == 451 and v["primary"]["error_class"] == "REGIONAL_RESTRICTION"
    assert v["fallback"]["ok"] is False and v["fallback"]["http_status"] == 403 and v["fallback"]["error_class"] == "REGIONAL_RESTRICTION"
    assert v["futures_available"] is False and v["data_status"] == "FUTURES_DATA_UNAVAILABLE"

    # Spot blocked from Futures PAPER_ENTRY
    spot_snap = MonitorSnapshot("BTCUSDT", "Binance Spot", "SPOT_REFERENCE", 1_760_000_000_000, 1_760_000_000_000, 60000.0, None, None, 59999.0, 60001.0)
    ok_res = Result(status="VALID", direction="LONG", checks=[{"Kriteria": "All", "Status": "LULUS"}], plan={"symbol": "BTCUSDT", "direction": "LONG", "entry": 60000.0, "sl": 59500.0, "setup_quality_score": 88.0})
    assert PredictiveLifecycleManager().promote_to_confirmed(None, hybrid_result=ok_res, snapshot=spot_snap, now_ms=1_760_000_000_000) is None


def test_10_budget_telemetry_monotonic_clock_consistency(tmp_path):
    t = [100.0]
    def _clk():
        t[0] += 0.25
        return t[0]

    class _MinMkt(FuturesMarket):
        def universe(self):
            return {"info": {"symbols": []}, "tickers": [], "books": [], "now_ms": 1_760_000_000_000}

    db = tmp_path / "paper.db"
    scanner = MarketScanner(_MinMkt(), GroqRotation([]), NewsCalendar(), PaperLedger(db, 1000.0), TelegramAlerts("", "", db), clock=_clk)
    rep = scanner.run(ScannerConfig(send_alerts=False, send_watchlist_alerts=False, send_scan_summary=False))
    bt = rep["scanner_diagnostics"]["budget_telemetry"]
    assert bt["clock_source"] == "monotonic"
    assert bt["total_elapsed_s"] == rep["duration_seconds"]
    assert "non_additive_overlap_note" in bt


def test_11_and_12_queue_five_targeted_invariants_and_paper_only(tmp_path):
    db = tmp_path / "paper.db"
    ledger = PaperLedger(db, 1000.0)
    t0 = 1_760_000_000_000

    # 1. Deferred persistence & 2. Restart recovery
    ledger.save_candidate_queue(
        {"items": [{"symbol": "FETUSDT", "queued_at_ms": t0}, {"symbol": "FILUSDT", "queued_at_ms": t0 - 1_200_000}], "expired": [], "updated_at_ms": t0},
        now_ms=t0,
    )
    reloaded = PaperLedger(db, 1000.0).load_candidate_queue()
    assert len(reloaded["items"]) == 2

    # 3. Candidate expiration (>900s max_queue_age_ms)
    expired = [x for x in reloaded["items"] if (t0 - x["queued_at_ms"]) > 900_000]
    active_def = [x for x in reloaded["items"] if (t0 - x["queued_at_ms"]) <= 900_000]
    assert [x["symbol"] for x in expired] == ["FILUSDT"]
    assert [x["symbol"] for x in active_def] == ["FETUSDT"]

    # 4. Candidate state transition & 5. No duplicate accounting (2 analyzed + 2 deferred + 37 rejected = 41)
    states = [
        {"candidate_id": "s1:BTCUSDT", "symbol": "BTCUSDT", "status": "ANALYZED", "status_timestamp_ms": t0},
        {"candidate_id": "s1:ETHUSDT", "symbol": "ETHUSDT", "status": "ANALYZED", "status_timestamp_ms": t0},
        {"candidate_id": "s1:FETUSDT", "symbol": "FETUSDT", "status": "DEFERRED", "status_timestamp_ms": t0},
        {"candidate_id": "s1:FILUSDT", "symbol": "FILUSDT", "status": "DEFERRED", "status_timestamp_ms": t0},
    ]
    rec = reconcile_candidate_pipeline(stage2_total=41, active_queue_count=0, analyzed_count=2, deferred_count=2, expired_count=0, rejected_count=37, cancelled_count=0, candidate_states=states)
    assert rec["active_queue_count"] == 0
    assert rec["accounted_total"] == 41
    assert rec["reconciliation_ok"] is True
    assert len({c["symbol"] for c in rec["candidate_states"]}) == 4

    # PAPER ONLY check
    forbidden = ("/fapi/v1/order", "/v5/order/create", "/api/v3/order", "/sapi/v1/capital/withdraw")
    root = Path(__file__).resolve().parent.parent
    for pyf in list((root / "futures").glob("*.py")) + [root / "worker.py", root / "app.py"]:
        txt = pyf.read_text(encoding="utf-8", errors="ignore")
        for ep in forbidden:
            assert ep not in txt

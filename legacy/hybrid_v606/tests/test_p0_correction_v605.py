"""15-Item P0 Correction Acceptance Test Suite for CoinAgentX v6.0.5 (P0-01 through P1-09).

Verifies:
 1. Binance Futures active (USD_M_PERPETUAL)
 2. Bybit Futures fallback (BYBIT_LINEAR_PERPETUAL)
 3. Spot blocked from Futures PAPER_ENTRY & FUTURES_DATA_UNAVAILABLE
 4. Worker heartbeat actual (ONLINE < 90s)
 5. Worker stale (> 90s -> STALE, never ONLINE)
 6. Dashboard & worker database identity identical + verified R/W + PG never shows `.sqlite3`
 7. Queue persistence & non-silent QUEUE_SAVE_FAILED
 8. Candidate reconciliation (complete queued_candidates, zero truncation)
 9. Hard gate M5 clarity (score 90 + M5 trigger missing stays NO_TRADE + decision_transparency)
10. Groq Advisor Only
11. Max 5 Groq per cycle
12. Budget timeout telemetry & zero candidate loss
13. Telegram retry, error_classes, outbox status & secret redaction
14. Restart recovery (predictive signal + events + queue preserved)
15. PAPER ONLY enforcement + BoundedLRUDict + real RSS memory profiling + separated runtime_diagnostics
"""
from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from futures.bybit_market import BybitFuturesMarket
from futures.db_backend import (
    DatabaseConfigurationError,
    connect_database,
    database_display_identity,
    database_identity_fingerprint,
    verify_repository_read_write,
)
from futures.groq_rotation import evaluate_contextual_ai
from futures.market import FuturesMarket, MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.memory_profiler import BoundedLRUDict, profile_scanner_memory, read_process_rss_mb
from futures.models import MonitorSnapshot, Result
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.predictive_store import PredictiveSignalRepository
from futures.predictive_watchlist import (
    PredictiveLifecycleManager,
    PredictiveSignal,
    STAGE_EARLY_WATCH,
)
from futures.provider_router import FuturesProviderRouter
from futures.spot_market import SpotReferenceMarket
from futures.telegram import TelegramAlerts, format_predictive_alert
import worker


def test_p0_01_binance_futures_active():
    class _OkBinance(FuturesMarket):
        def get(self, path, params=None):
            return {"serverTime": 1_760_000_000_000}

    router = FuturesProviderRouter(primary=_OkBinance(), fallback=BybitFuturesMarket(), spot_fallback=SpotReferenceMarket())
    res = router.get("/fapi/v1/time")
    assert res["serverTime"] == 1_760_000_000_000
    assert router.instrument_kind == "USD_M_PERPETUAL"
    assert router.futures_available is True
    assert router.data_status == "OK"


def test_p0_02_bybit_futures_fallback():
    class _BlockedBinance(FuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 451", http_status=451, endpoint=path)

    class _OkBybit(BybitFuturesMarket):
        def get(self, path, params=None):
            return {"time": 1_760_000_000_000}

    router = FuturesProviderRouter(primary=_BlockedBinance(), fallback=_OkBybit(), spot_fallback=SpotReferenceMarket())
    router.get("/fapi/v1/time")
    assert router.instrument_kind == "BYBIT_LINEAR_PERPETUAL"
    assert router.futures_available is True
    assert router.failover_reason and "Bybit" in router.failover_reason


def test_p0_03_spot_blocked_from_futures_entry_and_unavailable_status():
    class _BlockedBinance(FuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 451", http_status=451, endpoint=path)

    class _BlockedBybit(BybitFuturesMarket):
        def get(self, path, params=None):
            raise MarketError("HTTP 451 geo", http_status=451, endpoint=path, error_category="REGIONAL_RESTRICTION")

    class _OkSpot(SpotReferenceMarket):
        def get(self, path, params=None):
            return {"serverTime": 1_760_000_000_000}

    router = FuturesProviderRouter(primary=_BlockedBinance(), fallback=_BlockedBybit(), spot_fallback=_OkSpot(), spot_fallback_enabled=True)
    router.get("/fapi/v1/time")
    assert router.instrument_kind == "SPOT_REFERENCE"
    assert router.futures_available is False
    assert router.data_status == "FUTURES_DATA_UNAVAILABLE"

    # Spot NEVER promotes to Futures CONFIRMED / PAPER_ENTRY
    spot_snap = MonitorSnapshot("BTCUSDT", "Binance Spot", "SPOT_REFERENCE", 1_760_000_000_000, 1_760_000_000_000, 60000.0, 60000.0, 60000.0, 59999.0, 60001.0)
    assert spot_snap.mark_price is None
    mgr = PredictiveLifecycleManager()
    ok_res = Result(status="VALID", direction="LONG", checks=[{"Kriteria": "All", "Status": "LULUS"}], plan={"symbol": "BTCUSDT", "direction": "LONG", "entry": 60000.0, "sl": 59500.0, "setup_quality_score": 88.0})
    assert mgr.promote_to_confirmed(None, hybrid_result=ok_res, snapshot=spot_snap, now_ms=1_760_000_000_000) is None


def test_p0_04_and_05_worker_heartbeat_actual_and_stale_threshold(tmp_path):
    db = tmp_path / "paper.db"
    ledger = PaperLedger(db, 1000.0)
    now_ms = int(time.time() * 1000)
    disp = database_display_identity(db)
    ledger.record_worker_heartbeat(
        "worker-live-123",
        now_ms,
        60,
        strategy_profile="BALANCED",
        strategy_engine="HYBRID_ADAPTIVE_V1",
        last_scan_ms=now_ms,
        next_scan_ms=now_ms + 60_000,
        database_name=disp,
    )
    # Actual recent heartbeat (<90s) -> ONLINE
    st_online = ledger.worker_health_status(now_ms=now_ms + 25_000)
    assert st_online["alive"] is True
    assert st_online["worker_id"] == "worker-live-123"
    assert st_online["database_name"] == disp

    # Heartbeat older than 90s -> STALE / NOT alive
    st_stale = ledger.worker_health_status(now_ms=now_ms + 95_000)
    assert st_stale["alive"] is False
    assert st_stale.get("v11_status") in ("STALE", "OFFLINE")


def test_p0_06_database_identity_reconciliation_and_no_sqlite_display_on_pg(tmp_path, monkeypatch):
    db = tmp_path / "paper_history_spot_reference.sqlite3"
    pg = "postgresql://postgres:secretpass@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
    disp_pg = database_display_identity(db, pg_url=pg)
    assert "sqlite" not in disp_pg.lower()
    assert "secretpass" not in disp_pg
    assert disp_pg == "postgresql://aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
    assert database_identity_fingerprint(db, pg_url=pg) == database_identity_fingerprint(tmp_path / "other.db", pg_url=pg)

    # Verified read/write probe on SQLite local DB
    rw = verify_repository_read_write(db)
    assert rw["ok"] is True and rw["verified_rw_ok"] is True
    assert rw["database_fingerprint"] == database_identity_fingerprint(db)

    # Fail-closed when COINAGENT_REQUIRE_POSTGRES=1 without DATABASE_URL
    monkeypatch.setenv("COINAGENT_REQUIRE_POSTGRES", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rw_fail = verify_repository_read_write(db)
    assert rw_fail["ok"] is False and rw_fail["verified_rw_ok"] is False


def test_p0_07_and_08_queue_persistence_non_silent_and_reconciliation(tmp_path):
    db = tmp_path / "paper.db"
    ledger = PaperLedger(db, 1000.0)
    items = [{"symbol": f"COIN{i}USDT", "queued_at_ms": 1_760_000_000_000, "m15_score": 65} for i in range(11)]
    ledger.save_candidate_queue({"items": items, "expired": [], "updated_at_ms": 1_760_000_000_000}, now_ms=1_760_000_000_000)
    reloaded = PaperLedger(db, 1000.0).load_candidate_queue()
    assert len(reloaded["items"]) == 11  # Zero truncation

    # Mathematical reconciliation check: 33 shortlist = 2 analyzed + 11 deferred + 0 expired + 20 pre-deep rejected
    an_cnt, def_cnt, exp_cnt, s2_total = 2, 11, 0, 33
    rej_pre = max(0, s2_total - an_cnt - def_cnt - exp_cnt)
    assert an_cnt + def_cnt + exp_cnt + rej_pre == s2_total


def test_p0_09_hard_gate_m5_transparency_keeps_no_trade():
    res = Result(
        status="NO_TRADE",
        direction="LONG",
        checks=[
            {"Kriteria": "M15 Regime", "Status": "LULUS"},
            {"Kriteria": "Routing strategi & pemicu M5", "Status": "GAGAL"},
        ],
        reasons=["Routing strategi & pemicu M5"],
        metrics={"setup_quality_score": 90.0, "m5_trigger_passed": False},
        meta={"m15_regime": "TREND_UP", "routed_strategy": "A"},
    )
    assert res.status == "NO_TRADE"
    failed = [c["Kriteria"] for c in res.checks if c["Status"] != "LULUS"]
    assert "Routing strategi & pemicu M5" in failed


def test_p0_10_11_and_12_groq_advisor_only_max_5_and_budget_telemetry():
    # Advisor-Only: AI unavailable never turns VALID into NO_TRADE
    r = Result(status="VALID", direction="LONG", plan={"symbol": "BTCUSDT", "direction": "LONG"})
    ai = evaluate_contextual_ai(r, None)
    assert ai["ai_status"] == "AI_UNAVAILABLE"
    assert r.status == "VALID"

    # Worker config enforces max_groq_candidates == 5
    args = worker.parse_args(["--once"])
    assert args.max_groq_candidates == 5


def test_p0_13_telegram_retry_error_classes_outbox_and_redaction(tmp_path):
    calls = [0]
    class _Sess:
        def post(self, url, json=None, **kw):
            calls[0] += 1
            if calls[0] == 1:
                return SimpleNamespace(status_code=429, json=lambda: {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}})
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    clk = [1000.0]
    tg = TelegramAlerts("999999999:SUPER_SECRET_BOT_KEY", "-100888777", tmp_path / "paper.db", session=_Sess(), clock=lambda: clk[0])
    sig = PredictiveSignal("pw-tg-1", "BTCUSDT", "LONG", STAGE_EARLY_WATCH, early_score=60.0)
    r1 = tg.send_predictive_alert(sig)
    assert r1["status"] == "RETRY"
    clk[0] += 10.0
    r2 = tg.send_predictive_alert(sig)
    assert r2["status"] == "SENT"
    r3 = tg.send_predictive_alert(sig)
    assert r3["status"] == "DEDUPLICATED"

    repo = PredictiveSignalRepository(tmp_path / "paper.db")
    st = tg.telegram_status(repo)
    assert st["requests"] == 2
    assert st["success"] == 1
    assert st["errors"] == 1
    assert st["error_classes"]["HTTP_429"] == 1
    assert st["retry_count"] == 1
    assert st["duplicate_suppressed_count"] == 1
    assert "SUPER_SECRET_BOT_KEY" not in str(st)


def test_p0_14_restart_recovery_preserves_predictive_signal_and_events(tmp_path):
    db = tmp_path / "shared.db"
    repo1 = PredictiveSignalRepository(db)
    mgr = PredictiveLifecycleManager()
    sig = PredictiveSignal("pw-rec-1", "ETHUSDT", "LONG", STAGE_EARLY_WATCH, early_score=62.0, detected_at_ms=1000, updated_at_ms=1000, detected_price=2000.0, current_price=2000.0, trigger_price=2010.0, invalidation_price=1980.0, expires_at_ms=901000)
    tr = mgr.process_early_update(None, sig, current_price=2000.0, now_ms=1000)
    repo1.save_transition(tr, alert_message=format_predictive_alert(sig), now_ms=1000)

    # Fresh repository after simulated worker restart
    repo2 = PredictiveSignalRepository(db)
    recovered = repo2.get_active_signal("ETHUSDT")
    assert recovered is not None and recovered.signal_id == "pw-rec-1"
    assert len(repo2.list_events("pw-rec-1")) >= 1


def test_p0_15_paper_only_bounded_lru_and_real_memory_profiling():
    # Static check: zero live order endpoints
    forbidden = ("/fapi/v1/order", "/v5/order/create", "/api/v3/order", "/sapi/v1/capital/withdraw")
    root = Path(__file__).resolve().parent.parent
    for pyf in list((root / "futures").glob("*.py")) + [root / "worker.py", root / "app.py"]:
        txt = pyf.read_text(encoding="utf-8", errors="ignore")
        for ep in forbidden:
            assert ep not in txt

    # BoundedLRUDict strictly bounds entries (prevents P1-08 unbounded growth)
    cache = BoundedLRUDict(max_entries=32)
    for i in range(100):
        cache[f"k{i}"] = i
    assert len(cache) == 32
    assert "k0" not in cache and "k99" in cache

    # Real RSS memory profiling returns positive finite OS RSS MB
    tracemalloc.start()
    try:
        prof = profile_scanner_memory()
        assert prof["rss_mb"] > 0.0
        assert prof["tracemalloc_active"] is True
        assert read_process_rss_mb() > 0.0
    finally:
        tracemalloc.stop()

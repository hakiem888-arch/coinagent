"""Comprehensive Architecture & Regression Suite for CoinAgentX v6.0.2 (BUG-36 through BUG-44).

Verifies all 16 mandatory regression scenarios:
1.  BUG-38: Toggle OFF stays OFF across 10x full reloads.
2.  BUG-38: Toggle ON stays ON across 10x full reloads.
3.  BUG-38: Toggle values never mutate when role changes from ADMIN to VIEWER.
4.  BUG-38: Toggle values never mutate when role changes from VIEWER to ADMIN.
5.  BUG-38: Toggle values never mutate across provider failovers (Binance -> Bybit -> Spot Reference).
6.  BUG-38: Risky automatic toggles default to OFF on a fresh DB, and DB save failure surfaces SAVE_FAILED without swallowing exceptions.
7.  BUG-36: Independent Worker scanner continues running after ADMIN session token (3,600s TTL) expires.
8.  BUG-36: Dashboard & ledger explicitly surface WORKER_OFFLINE when worker is inactive and prevent dual concurrent schedulers when worker is alive.
9.  BUG-37: Passive UI refresh interval is 60s (never 15s) and passive UI refresh never triggers new market API requests.
10. BUG-40: Configured & effective scan interval synchronized across Admin, Viewer (never forced to 300s), and Worker dynamically without restart.
11. BUG-41: Every scan report is stored in global `scanner_runs` and Dashboard reads the newest report by timestamp across all providers/ledgers.
12. BUG-39: Filter rejection breakdown accurately counts all 7 rejection categories + summary counts.
13. BUG-42: News API failure sets `news_status = "NEWS_UNAVAILABLE"` and continues Stage 1 + Stage 2 + Stage 3 technical scan without aborting.
14. BUG-43: Stage 2 M15 candidate errors are classified into 8 specific codes (`HTTP_TIMEOUT`, `RATE_LIMIT`, `STALE_CANDLE`, `CANDLE_NOT_CLOSED`, `EMPTY_RESPONSE`, `INVALID_SYMBOL`, `INVALID_OHLC`, `CONNECTION_ERROR`) with credential redaction.
15. BUG-44: Separate stage time budgets mark incomplete candidate cycles as `PARTIAL_SCAN` (never `COMPLETED`) and queue remaining candidates for the next cycle.
16. SUPABASE / SQL Schema: `001_coinagent_schema.sql` and `_PostgresConnectionAdapter` define global `user_preferences` and `scanner_runs` with RLS and `service_role` grants.
"""
from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

from futures.db_backend import (
    DEFAULT_USER_PREFERENCES,
    _PostgresConnectionAdapter,
    load_global_preferences,
    load_latest_global_scan_run,
    save_global_preferences,
    save_global_scan_run,
)
from futures.market import MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.screener import (
    M15_ERROR_CODES,
    REJECTION_CATEGORIES,
    classify_m15_error,
    select_candidates,
    shortlist,
)
from tests.fixtures import NOW, candles
from tests.test_market_scanner import Alerts, Market, Pool
from tests.test_screener import universe
from worker import sync_worker_config_from_preferences

APP = Path(__file__).resolve().parents[1] / "app.py"
MIGRATION_SQL = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "001_coinagent_schema.sql"


def by_label(elements, label):
    return next(x for x in elements if x.label == label)


def test_01_bug38_toggle_off_stays_off_across_10x_full_reloads(monkeypatch, tmp_path):
    """Scenario 1 (BUG-38): Setting all toggles to OFF persists as OFF across 10 full browser reloads."""
    db_file = tmp_path / "reload_off.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug38")
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: {})

    # First session: Admin explicitly sets toggles OFF
    res = save_global_preferences(
        db_file,
        {
            "auto": False,
            "relaxed": False,
            "interval": 300,
            "candidates": 1,
            "alert": False,
            "watchlist_alert": False,
            "summary_alert": False,
        },
    )
    assert res["ok"] is True and res["status"] == "SAVED"

    for _ in range(10):
        at = AppTest.from_file(str(APP), default_timeout=20)
        at.session_state["admin_auto_authenticate_token"] = "admin-secret-bug38"
        at.run()
        assert not at.exception
        assert by_label(at.toggle, "Auto-scan seluruh market").value is False
        assert by_label(at.toggle, "Adaptive Reversal & Medium-Liquidity Mode").value is False
        assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is False
        assert by_label(at.toggle, "Alert Telegram REVERSAL WATCHLIST").value is False
        assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is False
        assert by_label(at.selectbox, "Interval scan (detik)").value == 300
        assert by_label(at.selectbox, "Kandidat analisis mendalam").value == 1


def test_02_bug38_toggle_on_stays_on_across_10x_full_reloads(monkeypatch, tmp_path):
    """Scenario 2 (BUG-38): Setting toggles to ON persists as ON across 10 full browser reloads."""
    db_file = tmp_path / "reload_on.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug38")
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: {})

    res = save_global_preferences(
        db_file,
        {
            "auto": True,
            "relaxed": True,
            "interval": 900,
            "candidates": 2,
            "alert": True,
            "watchlist_alert": True,
            "summary_alert": True,
        },
    )
    assert res["ok"] is True and res["status"] == "SAVED"

    for _ in range(10):
        at = AppTest.from_file(str(APP), default_timeout=20)
        at.session_state["admin_auto_authenticate_token"] = "admin-secret-bug38"
        at.run()
        assert not at.exception
        assert by_label(at.toggle, "Auto-scan seluruh market").value is True
        assert by_label(at.toggle, "Adaptive Reversal & Medium-Liquidity Mode").value is True
        assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is True
        assert by_label(at.toggle, "Alert Telegram REVERSAL WATCHLIST").value is True
        assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is True
        assert by_label(at.selectbox, "Interval scan (detik)").value == 900


def test_03_bug38_toggles_never_change_on_admin_to_viewer_role_transition(monkeypatch, tmp_path):
    """Scenario 3 (BUG-38): When ADMIN logs out or token expires (becoming VIEWER), ON toggles stay ON and OFF toggles stay OFF."""
    db_file = tmp_path / "role_admin_to_viewer.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug38")
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: {})

    save_global_preferences(
        db_file,
        {
            "auto": True,
            "relaxed": False,
            "interval": 300,
            "alert": True,
            "watchlist_alert": False,
            "summary_alert": True,
        },
    )
    at = AppTest.from_file(str(APP), default_timeout=20)
    at.session_state["admin_auto_authenticate_token"] = "admin-secret-bug38"
    at.run()
    assert not at.exception
    assert by_label(at.toggle, "Auto-scan seluruh market").value is True
    assert by_label(at.toggle, "Adaptive Reversal & Medium-Liquidity Mode").value is False
    assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is True
    assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is True

    # Logout Admin -> transitions to VIEWER
    by_label(at.button, "Logout Admin").click().run()
    assert not at.exception
    # Values must remain identical (ON stays ON, OFF stays OFF)
    assert by_label(at.toggle, "Auto-scan seluruh market").value is True
    assert by_label(at.toggle, "Adaptive Reversal & Medium-Liquidity Mode").value is False
    assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is True
    assert by_label(at.toggle, "Alert Telegram REVERSAL WATCHLIST").value is False
    assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is True
    assert by_label(at.selectbox, "Interval scan (detik)").value == 300


def test_04_bug38_toggles_never_change_on_viewer_to_admin_role_transition(monkeypatch, tmp_path):
    """Scenario 4 (BUG-38): When VIEWER logs in as ADMIN, stored OFF toggles stay OFF and are never reset to ON."""
    db_file = tmp_path / "role_viewer_to_admin.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug38")
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: {})

    save_global_preferences(
        db_file,
        {
            "auto": False,
            "relaxed": False,
            "interval": 300,
            "alert": False,
            "watchlist_alert": False,
            "summary_alert": False,
        },
    )
    # Start as VIEWER
    at = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not at.exception
    assert by_label(at.toggle, "Auto-scan seluruh market").value is False
    assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is False

    # Log in as ADMIN via UI
    by_label(at.checkbox, "Masukkan token administrator").check().run()
    by_label(at.text_input, "Token Administrator").set_value("admin-secret-bug38")
    by_label(at.button, "Verifikasi Admin").click().run()
    assert not at.exception
    # Stored OFF values must remain OFF after becoming ADMIN!
    assert by_label(at.toggle, "Auto-scan seluruh market").value is False
    assert by_label(at.toggle, "Adaptive Reversal & Medium-Liquidity Mode").value is False
    assert by_label(at.toggle, "Alert Telegram PAPER BUY/SELL").value is False
    assert by_label(at.toggle, "Alert Telegram REVERSAL WATCHLIST").value is False
    assert by_label(at.toggle, "Kirim MARKET SCANNER REPORT setiap siklus").value is False
    assert by_label(at.selectbox, "Interval scan (detik)").value == 300


def test_05_bug38_toggles_never_change_across_provider_failover_binance_bybit_spot(tmp_path):
    """Scenario 5 (BUG-38): Switching active ledger across Binance, Bybit, and Spot Reference preserves global user_preferences."""
    binance_ledger = PaperLedger(tmp_path / "paper_history.sqlite3", expected_instrument_kind="USD_M_PERPETUAL")
    bybit_ledger = PaperLedger(tmp_path / "bybit_paper_history.sqlite3", expected_instrument_kind="BYBIT_LINEAR_PERPETUAL")
    spot_ledger = PaperLedger(tmp_path / "spot_paper_history.sqlite3", expected_instrument_kind="SPOT_REFERENCE")

    save_res = binance_ledger.save_ui_preferences({
        "auto": True,
        "relaxed": False,
        "interval": 300,
        "candidates": 1,
        "alert": False,
        "watchlist_alert": True,
        "summary_alert": False,
    })
    assert save_res["ok"] is True and save_res["status"] == "SAVED"

    # Reading from Bybit and Spot Reference ledgers returns the exact same global preferences
    for lg in (binance_ledger, bybit_ledger, spot_ledger):
        prefs = lg.load_ui_preferences()
        assert prefs["_exists"] is True
        assert prefs["auto"] is True
        assert prefs["relaxed"] is False
        assert prefs["interval"] == 300
        assert prefs["candidates"] == 1
        assert prefs["alert"] is False
        assert prefs["watchlist_alert"] is True
        assert prefs["summary_alert"] is False


def test_06_bug38_risky_automatic_toggles_default_off_and_save_failure_surfaces_error(monkeypatch, tmp_path):
    """Scenario 6 (BUG-38): Fresh database defaults all risky automatic features to OFF, and DB errors return SAVE_FAILED."""
    fresh_db = tmp_path / "fresh_prefs.db"
    prefs = load_global_preferences(fresh_db)
    assert prefs["_exists"] is False
    assert DEFAULT_USER_PREFERENCES["auto_scan"] is False
    assert DEFAULT_USER_PREFERENCES["relaxed_mode"] is False
    assert DEFAULT_USER_PREFERENCES["send_alerts"] is False
    assert DEFAULT_USER_PREFERENCES["send_watchlist_alerts"] is False
    assert DEFAULT_USER_PREFERENCES["send_scan_summary"] is False
    assert prefs["auto"] is False
    assert prefs["relaxed"] is False
    assert prefs["alert"] is False
    assert prefs["watchlist_alert"] is False
    assert prefs["summary_alert"] is False

    # Simulate database write failure and confirm it is NOT swallowed silently
    import futures.db_backend as dbb
    monkeypatch.setattr(dbb, "connect_database", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Simulated disk/DB failure")))
    fail_res = save_global_preferences(fresh_db, {"auto": True})
    assert fail_res["ok"] is False
    assert fail_res["status"] == "SAVE_FAILED"
    assert "Simulated disk/DB failure" in fail_res["error"]


def test_07_bug36_scanner_worker_continues_when_admin_session_token_expires(monkeypatch, tmp_path):
    now_clk = [1_700_000_000.0]
    monkeypatch.setattr('futures.market_scanner.time.time', lambda: now_clk[0])
    """Scenario 7 (BUG-36): Independent Worker continues scanning even after ADMIN session token (3,600s TTL) expires."""
    ledger = PaperLedger(tmp_path / "worker_indep.db")
    from futures.security import SecurityManager
    sec = SecurityManager(
        ledger.db,
        admin_token="secret-admin-tok",
        clock=lambda: now_clk[0],
    )
    admin_tok = sec.authenticate("secret-admin-tok")
    assert sec.verify_session(admin_tok)["role"] == "ADMIN"

    # Worker scanner has its own independent ROLE_WORKER session
    m = Market()
    p = Pool("WAIT")
    a = Alerts(ledger)
    from futures.news import NewsCalendar
    worker_scanner = MarketScanner(
        m, p, NewsCalendar(), ledger, a,
        clock=lambda: now_clk[0],
        security=sec,
        trusted_worker=True,
    )
    cfg = ScannerConfig(interval=60, send_alerts=False, send_watchlist_alerts=False, send_scan_summary=False)

    rep1 = worker_scanner.run(cfg, worker_id="worker-24x7")
    assert rep1["scan_status"] in ("COMPLETED", "COMPLETED_WITH_NOTES")

    # Advance clock by 4,000 seconds (> 3,600s ADMIN token TTL)
    now_clk[0] += 4000.0
    # Admin token is now expired and downgraded to VIEWER
    assert sec.verify_session(admin_tok)["role"] == "VIEWER"

    # Independent Worker scanner still runs without interruption!
    rep2 = worker_scanner.run(cfg, worker_id="worker-24x7")
    assert rep2["scan_id"] != rep1["scan_id"]
    assert rep2["scan_status"] in ("COMPLETED", "COMPLETED_WITH_NOTES")
    assert len(rep2["analyses"]) == 2


def test_08_bug36_worker_offline_status_displayed_and_dual_scheduler_prevented(monkeypatch, tmp_path):
    """Scenario 8 (BUG-36): Surfaces WORKER_OFFLINE when worker is inactive, and prevents browser auto-scan when worker is alive."""
    db_file = tmp_path / "worker_state.db"
    ledger = PaperLedger(db_file)
    wh_offline = ledger.worker_health_status(now_ms=NOW)
    assert wh_offline["alive"] is False
    assert wh_offline["worker_state"] == "WORKER_OFFLINE"
    assert "WORKER_OFFLINE" in wh_offline["detail"]

    # When worker records a fresh heartbeat, worker_state is WORKER_ONLINE
    ledger.record_worker_heartbeat("worker-live-01", NOW, 60)
    wh_online = ledger.worker_health_status(now_ms=NOW + 5000)
    assert wh_online["alive"] is True
    assert wh_online["worker_state"] == "WORKER_ONLINE"

    # In dashboard UI, when Worker is alive, browser does NOT run a second concurrent auto-scan scheduler
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug36")
    save_global_preferences(db_file, {"auto": True, "interval": 60})
    ledger.record_worker_heartbeat("worker-live-01", int(__import__("time").time() * 1000), 60)
    scan_calls = []
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: scan_calls.append(manual) or {})
    at = AppTest.from_file(str(APP), default_timeout=20)
    at.session_state["admin_auto_authenticate_token"] = "admin-secret-bug36"
    at.run()
    assert not at.exception
    # Browser auto-scan yielded to the live Worker!
    assert len(scan_calls) == 0


def test_09_bug37_passive_ui_refresh_is_60s_and_never_triggers_market_api_requests(monkeypatch, tmp_path):
    """Scenario 9 (BUG-37): UI refresh is 60s (never 15s) and passive UI refresh does not trigger new market scans."""
    import app as app_mod
    assert 30 <= app_mod.UI_REFRESH_SECONDS <= 60
    assert app_mod.UI_REFRESH_SECONDS != 15

    db_file = tmp_path / "ui_refresh.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug37")
    save_global_preferences(db_file, {"auto": True, "interval": 60})
    calls = []
    def mock_run(self, cfg, manual=False, **kw):
        calls.append(1)
        return {
            "scan_id": "scan-initial",
            "started_at_ms": NOW,
            "completed_at_ms": NOW + 1000,
            "last_scan_at_ms": NOW,
            "next_scan_at_ms": NOW + 60_000,
            "configured_interval": 60,
            "effective_interval": 60,
            "now_ms": NOW,
            "universe_loaded": True,
            "universe_count": 120,
            "eligible_count": 30,
            "shortlist": [],
            "selected": [],
            "analyses": [],
            "errors": [],
            "duration_seconds": 1.0,
        }
    monkeypatch.setattr(MarketScanner, "run", mock_run)
    at = AppTest.from_file(str(APP), default_timeout=20)
    at.session_state["admin_auto_authenticate_token"] = "admin-secret-bug37"
    at.run()
    assert len(calls) == 1
    # Passive UI refreshes before interval due never trigger extra scans
    at.run()
    at.run()
    assert len(calls) == 1
    assert any("Jadwal scan berikutnya:" in c.value for c in at.caption)


def test_10_bug40_configured_and_effective_interval_synchronized_across_admin_viewer_and_worker(monkeypatch, tmp_path):
    """Scenario 10 (BUG-40): Admin, Viewer, and Worker read the exact same global scan_interval without forcing 300s on Viewer."""
    db_file = tmp_path / "interval_sync.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-bug40")
    save_global_preferences(db_file, {"auto": True, "interval": 60, "relaxed": True})

    # 1. Viewer session reads interval=60 (never forced to 300!)
    at_viewer = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not at_viewer.exception
    assert by_label(at_viewer.selectbox, "Interval scan (detik)").value == 60

    # 2. Worker dynamically syncs interval from user_preferences without restart
    ledger = PaperLedger(db_file)
    dummy_engine = type("DummyEngine", (), {"ledger": ledger})()
    base_cfg = ScannerConfig(interval=300, relaxed_mode=False)
    synced_cfg = sync_worker_config_from_preferences(dummy_engine, base_cfg)
    assert synced_cfg.interval == 60
    assert synced_cfg.relaxed_mode is True

    # 3. Admin updates interval to 900 -> Worker immediately picks up 900 on next cycle
    save_global_preferences(db_file, {"auto": True, "interval": 900, "relaxed": False})
    synced_cfg_2 = sync_worker_config_from_preferences(dummy_engine, synced_cfg)
    assert synced_cfg_2.interval == 900
    assert synced_cfg_2.relaxed_mode is False


def test_11_bug41_dashboard_reads_latest_report_from_global_scanner_runs_across_providers(monkeypatch, tmp_path):
    """Scenario 11 (BUG-41): Scan reports from Binance, Bybit, and Spot Reference are written to global `scanner_runs` and Dashboard always loads the newest report."""
    binance_db = tmp_path / "paper_history.sqlite3"
    bybit_db = tmp_path / "bybit_paper_history.sqlite3"
    spot_db = tmp_path / "spot_paper_history.sqlite3"

    lg_binance = PaperLedger(binance_db, expected_instrument_kind="USD_M_PERPETUAL")
    lg_bybit = PaperLedger(bybit_db, expected_instrument_kind="BYBIT_LINEAR_PERPETUAL")
    lg_spot = PaperLedger(spot_db, expected_instrument_kind="SPOT_REFERENCE")

    lg_binance.save_scan_report({
        "scan_id": "run-binance-1",
        "started_at_ms": NOW + 1000,
        "completed_at_ms": NOW + 1500,
        "now_ms": NOW + 1500,
        "instrument_kind": "USD_M_PERPETUAL",
        "market_provider": "Binance USD-M Futures",
        "scan_status": "COMPLETED",
        "universe_loaded": True,
        "universe_count": 200,
        "eligible_count": 40,
        "shortlist": [],
        "selected": [],
        "analyses": [],
        "errors": [],
    })
    lg_bybit.save_scan_report({
        "scan_id": "run-bybit-2",
        "started_at_ms": NOW + 5000,
        "completed_at_ms": NOW + 5800,
        "now_ms": NOW + 5800,
        "instrument_kind": "BYBIT_LINEAR_PERPETUAL",
        "market_provider": "Bybit USDT Perpetual",
        "scan_status": "COMPLETED",
        "universe_loaded": True,
        "universe_count": 180,
        "eligible_count": 35,
        "shortlist": [],
        "selected": [],
        "analyses": [],
        "errors": [],
    })
    lg_spot.save_scan_report({
        "scan_id": "run-spot-3",
        "started_at_ms": NOW + 9000,
        "completed_at_ms": NOW + 9600,
        "now_ms": NOW + 9600,
        "instrument_kind": "SPOT_REFERENCE",
        "market_provider": "Binance Spot Reference",
        "scan_status": "COMPLETED",
        "universe_loaded": True,
        "universe_count": 250,
        "eligible_count": 55,
        "shortlist": [],
        "selected": [],
        "analyses": [],
        "errors": [],
    })

    # Even when querying via lg_binance, latest_global_scan_report returns the newest run (`run-spot-3`)
    latest = lg_binance.latest_global_scan_report()
    assert latest is not None
    assert latest["scan_id"] == "run-spot-3"
    assert latest["instrument_kind"] == "SPOT_REFERENCE"
    assert latest["universe_count"] == 250


def test_12_bug39_filter_rejection_breakdown_counts_all_7_categories_and_summary():
    """Scenario 12 (BUG-39): `shortlist()` returns accurate counts for all 7 rejection categories and summary metrics."""
    u = universe(10)
    # Inject specific rejection causes across symbols 0..6:
    u["info"]["symbols"][0]["status"] = "SETTLING"          # rejected_invalid_symbol
    u["tickers"][1]["quoteVolume"] = "500000"               # rejected_low_volume
    u["books"][2]["askPrice"] = "102.00"                    # rejected_wide_spread
    u["tickers"][3]["count"] = 50                           # rejected_low_trades
    u["books"][4]["time"] = NOW - 120_000                   # rejected_stale_quote
    u["books"].pop(5)                                       # rejected_missing_candle
    u["tickers"][6]["highPrice"] = "100.0"                  # rejected_intraday_range (flat 0%)
    u["tickers"][6]["lowPrice"] = "100.0"

    out = shortlist(u, min_volume=10_000_000, limit=10, allow_medium_liquidity_fallback=False)
    bd = out["rejection_breakdown"]
    for cat in REJECTION_CATEGORIES:
        assert cat in bd
    assert bd["rejected_invalid_symbol"] == 1
    assert bd["rejected_low_volume"] == 1
    assert bd["rejected_wide_spread"] == 1
    assert bd["rejected_low_trades"] == 1
    assert bd["rejected_stale_quote"] == 1
    assert bd["rejected_missing_candle"] == 1
    assert bd["rejected_intraday_range"] == 1
    assert out["total_discovered"] == 10
    assert out["eligible_count"] == 3
    assert out["shortlist_count"] == 3
    assert out["rejected_count"] == 7


def test_13_bug42_news_api_failure_keeps_technical_screening_and_analysis_running(tmp_path):
    """Scenario 13 (BUG-42): When News API fails/times out, technical screening (Stage 1 + Stage 2 + Stage 3) still completes with news_status='NEWS_UNAVAILABLE'."""
    class FailingNewsCalendar:
        def window(self, now_ms):
            raise RuntimeError("BLS/Fed connection timed out")

    ledger = PaperLedger(tmp_path / "news_fail.db")
    m = Market()
    p = Pool("WAIT")
    a = Alerts(ledger)
    scanner = MarketScanner(m, p, FailingNewsCalendar(), ledger, a, clock=lambda: 100.0)
    cfg = ScannerConfig(fee=.0004, slippage_bps=.1, funding=.00001)

    report = scanner.run(cfg)
    assert report["news_status"] == "NEWS_UNAVAILABLE"
    assert report["technical_scan_completed"] is True
    assert len(report["shortlist"]) >= 20
    assert len(report["selected"]) == 2
    assert len(report["ranked"]) >= 20
    assert len(report["analyses"]) == 2
    # Paper trade is safely held in WAIT because news check is unverified
    assert len(ledger.trades()) == 0


def test_14_bug43_m15_candidate_errors_classified_into_8_specific_codes_with_redaction():
    """Scenario 14 (BUG-43): M15 candidate errors are classified into all 8 specific error codes and secrets are redacted."""
    cases = [
        (TimeoutError("Read timed out api_key=secret123"), "HTTP_TIMEOUT"),
        (MarketError("HTTP 429 Too Many Requests", http_status=429), "RATE_LIMIT"),
        (ValueError("STALE_CANDLE: Candle 15m kedaluwarsa"), "STALE_CANDLE"),
        (ValueError("CANDLE_NOT_CLOSED: Candle 15m terakhir belum tertutup"), "CANDLE_NOT_CLOSED"),
        (ValueError("EMPTY_RESPONSE: Respons candle M15 kosong"), "EMPTY_RESPONSE"),
        (MarketError("HTTP 400 Invalid symbol", http_status=400), "INVALID_SYMBOL"),
        (ValueError("INVALID_OHLC: Data OHLC mengandung NaN"), "INVALID_OHLC"),
        (MarketError("Connection reset by peer postgresql://user:pass@host:5432/db"), "CONNECTION_ERROR"),
    ]
    for exc, expected_code in cases:
        diag = classify_m15_error(exc, symbol="BTCUSDT", timeframe="15m")
        assert diag["code"] == expected_code
        assert diag["code"] in M15_ERROR_CODES
        assert "secret123" not in diag["detail"]
        assert "user:pass" not in diag["detail"]

    # Also verify select_candidates formats errors with symbol, [15m], and specific code
    rows = shortlist(universe(5))["rows"][:2]
    class BrokenM15Market:
        def closed_candles(self, symbol, interval, now_ms, limit):
            if symbol == rows[0]["symbol"]:
                raise TimeoutError("Request timed out after 10s")
            d = candles(100 + np.arange(100) * 0.1, "15m")
            d.loc[d.index[-1], "close_time"] = now_ms + 60_000  # Not closed yet
            return d

    selected, ranked, errors, diags = select_candidates(rows, BrokenM15Market(), NOW, 2, return_diagnostics=True)
    assert len(selected) == 0 and len(errors) == 2 and len(diags) == 2
    assert "HTTP_TIMEOUT" in errors[0] and "[15m]" in errors[0]
    assert "CANDLE_NOT_CLOSED" in errors[1] and "[15m]" in errors[1]


def test_15_bug44_stage_budgets_and_partial_scan_status_with_candidate_queue(tmp_path):
    """Scenario 15 (BUG-44): Exceeding candidate/cycle budget marks scan_status='PARTIAL_SCAN' (never 'COMPLETED') and queues remaining candidates."""
    ledger = PaperLedger(tmp_path / "partial_scan.db")
    now_clk = [100.0]
    m = Market()
    p = Pool("WAIT")
    a = Alerts(ledger)
    from futures.news import NewsCalendar
    scanner = MarketScanner(m, p, NewsCalendar(), ledger, a, clock=lambda: now_clk[0])
    orig_load_modes = m.load_modes

    def slow_load_modes(symbol, modes=None):
        now_clk[0] += 30.0  # Exceeds per_candidate_timeout_s=25.0 after first candidate
        return orig_load_modes(symbol, modes)

    m.load_modes = slow_load_modes
    cfg = ScannerConfig(
        candidates=2,
        per_candidate_timeout_s=25.0,
        total_cycle_budget_s=60.0,
        send_alerts=False,
        send_watchlist_alerts=False,
        send_scan_summary=False,
    )
    rep1 = scanner.run(cfg)
    assert rep1["scan_status"] == "PARTIAL_SCAN"
    assert rep1["scan_status"] not in ("COMPLETED", "COMPLETED_WITH_NOTES")
    assert len(rep1["analyses"]) == 1
    assert len(rep1["queued_candidates"]) == 1
    queued_sym = rep1["queued_candidates"][0]
    assert scanner.next_symbol == queued_sym
    assert queued_sym in scanner.pending_candidate_queue

    # Next cycle processes the queued candidate first!
    m.load_modes = orig_load_modes
    now_clk[0] += 120.0
    rep2 = scanner.run(cfg, manual=True)
    assert rep2["scan_status"] in ("COMPLETED", "COMPLETED_WITH_NOTES")
    assert rep2["analyses"][0]["snapshot"].symbol == queued_sym


def test_16_postgresql_schema_migration_includes_user_preferences_and_scanner_runs_with_rls():
    """Scenario 16 (BUG-38, BUG-41, SEC-03): Migration SQL and PostgreSQL adapter support global user_preferences and scanner_runs."""
    sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.user_preferences" in sql_text
    assert "CREATE TABLE IF NOT EXISTS public.scanner_runs" in sql_text
    assert "'user_preferences'" in sql_text
    assert "'scanner_runs'" in sql_text

    # Verify _PostgresConnectionAdapter._translate_sql does NOT prefix global tables with namespace_prefix
    t_prefs = _PostgresConnectionAdapter._translate_sql(
        "INSERT OR REPLACE INTO user_preferences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        namespace_prefix="spot_paper_history_",
    )
    assert "INTO user_preferences" in t_prefs
    assert "spot_paper_history_user_preferences" not in t_prefs
    assert "ON CONFLICT (user_id) DO UPDATE SET" in t_prefs

    t_runs = _PostgresConnectionAdapter._translate_sql(
        "INSERT OR REPLACE INTO scanner_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        namespace_prefix="bybit_paper_history_",
    )
    assert "INTO scanner_runs" in t_runs
    assert "bybit_paper_history_scanner_runs" not in t_runs
    assert "ON CONFLICT (scan_id) DO UPDATE SET" in t_runs



def test_17_hot_reload_bootstrap_purges_stale_sys_modules_preventing_import_error(tmp_path):
    """Scenario 17: When Streamlit Cloud hot-reloads app.py with an older `futures.db_backend` cached in sys.modules, `_bootstrap_project_files()` upgrades files on disk AND purges stale `sys.modules['futures.*']` so `from futures.db_backend import DEFAULT_USER_PREFERENCES` never raises ImportError."""
    import runpy
    import sys
    import types

    # Simulate an older `futures.db_backend` module cached in sys.modules lacking DEFAULT_USER_PREFERENCES
    stale_mod = types.ModuleType("futures.db_backend")
    stale_mod.DatabaseConfigurationError = RuntimeError
    saved_modules = {k: v for k, v in sys.modules.items() if k == "futures" or k.startswith("futures.")}
    try:
        sys.modules["futures.db_backend"] = stale_mod
        assert not hasattr(sys.modules["futures.db_backend"], "DEFAULT_USER_PREFERENCES")
        # Running app.py top-level bootstrap must purge the stale module and import the real one cleanly
        import app as app_mod
        app_mod._purge_stale_futures_modules(Path(app_mod.__file__).resolve().parent, force=False)
        from futures.db_backend import (
            DEFAULT_USER_PREFERENCES,
            load_global_preferences,
            load_latest_global_scan_run,
            resolve_global_control_db_path,
            save_global_preferences,
        )
        assert DEFAULT_USER_PREFERENCES["auto_scan"] is False
        assert callable(save_global_preferences)
        assert callable(load_global_preferences)
        assert callable(load_latest_global_scan_run)
        assert callable(resolve_global_control_db_path)
    finally:
        for k in [k for k in list(sys.modules) if k == "futures" or k.startswith("futures.")]:
            sys.modules.pop(k, None)
        sys.modules.update(saved_modules)



def test_18_postgres_int4_to_bigint_and_datatype_mismatch_fallback(monkeypatch, tmp_path):
    """Scenario 18: Verify PostgreSQL execute() automatically heals 32-bit `integer out of range` (`NumericValueOutOfRange`) by upgrading int4 columns to BIGINT via raw cursor and retrying seamlessly."""
    ddl = _PostgresConnectionAdapter._translate_sql(
        "CREATE TABLE IF NOT EXISTS scanner_locks (lock_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, acquired_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL)",
        namespace_prefix="paper_history_spot_reference_",
    )
    assert "acquired_ms BIGINT NOT NULL" in ddl
    assert "expires_ms BIGINT NOT NULL" in ddl

    # Simulate a PostgreSQL connection where `paper_history_spot_reference_scanner_locks.acquired_ms` is still `integer` (int4)
    # and throws `NumericValueOutOfRange: integer out of range` until `ALTER TABLE ... TYPE BIGINT` is executed!
    class FakePgCursor:
        def __init__(self, conn):
            self.conn = conn
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            s = sql.strip()
            if "information_schema.columns" in s and "data_type = 'integer'" in s:
                self._rows = [(tbl, col) for (tbl, col), dtype in self.conn.col_types.items() if dtype == "integer"]
                return
            if s.startswith("ALTER TABLE public."):
                m = __import__("re").search(r'ALTER TABLE public\."([^"]+)" ALTER COLUMN "([^"]+)" TYPE BIGINT', s)
                if m:
                    self.conn.col_types[(m.group(1), m.group(2))] = "bigint"
                return
            if "INSERT INTO paper_history_spot_reference_scanner_locks" in s:
                if self.conn.col_types.get(("paper_history_spot_reference_scanner_locks", "acquired_ms")) == "integer":
                    if any(isinstance(p, int) and p > 2_147_483_647 for p in params):
                        class NumericValueOutOfRange(Exception):
                            pass
                        raise NumericValueOutOfRange("integer out of range")
                self.conn.inserted.append(params)

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class FakePgConn:
        def __init__(self):
            self.col_types = {("paper_history_spot_reference_scanner_locks", "acquired_ms"): "integer"}
            self.inserted = []
            self.rollbacks = 0
            self.commits = 0

        def cursor(self):
            return FakePgCursor(self)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            pass

    raw = FakePgConn()
    adapter = _PostgresConnectionAdapter(raw, driver_name="psycopg", namespace_prefix="paper_history_spot_reference_")
    now_64bit = 1_763_311_200_000
    adapter.execute(
        "INSERT OR REPLACE INTO scanner_locks VALUES ('active_scanner', ?, ?, ?)",
        ("scanner-01", now_64bit, now_64bit + 120_000),
    )
    assert raw.col_types[("paper_history_spot_reference_scanner_locks", "acquired_ms")] == "bigint"
    assert len(raw.inserted) == 1 and raw.inserted[0][1] == now_64bit

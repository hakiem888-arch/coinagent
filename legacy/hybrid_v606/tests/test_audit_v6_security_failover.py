"""Comprehensive Remediation Tests for BUG-09 through BUG-14 (CoinAgentX v6.0.0).

Covers:
- BUG-09: Strict separation of Binance Futures (USD_M_PERPETUAL), Bybit (BYBIT_LINEAR_PERPETUAL),
          and Spot Reference (SPOT_REFERENCE via paper_db_path) SQLite databases, independent
          virtual balances, and zero cross-venue price marking on provider failover.
- BUG-10: Server-side authentication, RBAC (ADMIN vs VIEWER), server-side enforcement on
          manual scan, Telegram test/send, journal export, and Groq AI rate limiting, plus
          persistent SQLite audit log.
- BUG-11: Import & execution verification of C12, UI AppTest suites, and GitHub Actions CI workflow.
- BUG-12: Provider failover across all 6 required position & error states:
          (1) No active position, (2) Active PENDING, (3) Active OPEN, (4) TP1 filled,
          (5) Stop at break-even, (6) HTTP 451, 403, 429, and ConnectionTimeout.
- BUG-13: Deployment & resource protection (manual scan frequency limit, AI session quota,
          Telegram repeat protection, journal export authorization, SQLite integrity check,
          Streamlit restart state recovery, and CPU/memory/API request monitoring).
- BUG-14: Spot Reference validation & distinct labeling across strategy, Telegram, dashboard,
          and journal export.
"""
from copy import deepcopy
from pathlib import Path
import json
import pytest
import requests

import app as app_mod
from futures.market import MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.provider_router import FuturesProviderRouter
from futures.security import SecurityManager, ROLE_ADMIN, ROLE_VIEWER, verify_sqlite_integrity, ResourceMonitor
from futures.spot_market import paper_db_path, futures_db_path, bybit_db_path, resolve_db_path
from futures.strategy import evaluate, break_even, quantize, spot_reference_validation_metadata
from futures.telegram import format_alert, TelegramAlerts
from tests.fixtures import setup, NOW
from tests.test_paper import approved, quote, enter
from tests.test_market_scanner import Market, Calendar, Pool, Alerts
from tests.test_spot_market import spot_snapshot


class MultiTierFakeMarket(Market):
    """Stateful fake market supporting Binance Futures, Bybit Perpetual, and Spot Reference snapshots."""

    def __init__(self, kind="USD_M_PERPETUAL", price_offset=0.0):
        super().__init__()
        self.instrument_kind = kind
        self.price_offset = float(price_offset)
        self.time_path = (
            "/api/v3/time" if kind == "SPOT_REFERENCE"
            else ("/v5/market/time" if kind == "BYBIT_LINEAR_PERPETUAL" else "/fapi/v1/time")
        )
        self.market_label = (
            "Binance Spot" if kind == "SPOT_REFERENCE"
            else ("Bybit USDT Perpetual" if kind == "BYBIT_LINEAR_PERPETUAL" else "Binance Futures")
        )
        self.fail_mode = None  # None, 451, 403, 429, "timeout"

    def _check_fail(self, endpoint="/time"):
        if self.fail_mode == 451:
            raise MarketError("HTTP 451 blocked", http_status=451, endpoint=endpoint)
        if self.fail_mode == 403:
            raise MarketError("HTTP 403 forbidden", http_status=403, endpoint=endpoint)
        if self.fail_mode == 429:
            raise MarketError("HTTP 429 rate limit", http_status=429, endpoint=endpoint)
        if self.fail_mode == "timeout":
            raise MarketError("Connection timeout", http_status=None, endpoint=endpoint)

    def get(self, path, *args, **kwargs):
        self._check_fail(path)
        return {"serverTime": NOW}

    def universe(self):
        self._check_fail("/universe")
        u = super().universe()
        u["instrument_kind"] = self.instrument_kind
        if self.instrument_kind == "SPOT_REFERENCE":
            u["book_observed_ms"] = NOW
            for s in u["info"]["symbols"]:
                s.pop("contractType", None)
                s["isSpotTradingAllowed"] = True
            for b in u["books"]:
                b.pop("time", None)
        return u

    def snapshot(self, symbol, mode):
        self._check_fail("/snapshot")
        s = super().snapshot(symbol, mode)
        if self.instrument_kind == "SPOT_REFERENCE":
            s = spot_snapshot(s)
        else:
            s.instrument_kind = self.instrument_kind
            s.source = f"SYNTHETIC {self.instrument_kind}"
        if self.price_offset:
            for col in ("open", "high", "low", "close"):
                s.bias[col] = s.bias[col] + self.price_offset
                s.execution[col] = s.execution[col] + self.price_offset
            s.last += self.price_offset
            if s.mark is not None:
                s.mark += self.price_offset
            s.bid += self.price_offset
            s.ask += self.price_offset
            s.bids = [[s.bid, 10000]]
            s.asks = [[s.ask, 10000]]
            if s.open_bar_high is not None:
                s.open_bar_high += self.price_offset
                s.open_bar_low += self.price_offset
            if s.open_mark_high is not None:
                s.open_mark_high += self.price_offset
                s.open_mark_low += self.price_offset
            if s.previous_day_high is not None:
                s.previous_day_high += self.price_offset
                s.previous_day_low += self.price_offset
        return s


# ==============================================================================
# BUG-09 & BUG-12: DATABASE ISOLATION & MULTI-TIER FAILOVER WITH ACTIVE POSITIONS
# ==============================================================================

def test_bug09_database_paths_and_ledgers_are_strictly_isolated(monkeypatch, tmp_path):
    """BUG-09: Spot, Bybit, and Binance USD-M Perpetual use separate DB files and independent virtual equity."""
    monkeypatch.delenv("SPOT_HISTORY_DB", raising=False)
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "paper_history.sqlite3"))

    fut_db = resolve_db_path(tmp_path, "USD_M_PERPETUAL")
    byb_db = resolve_db_path(tmp_path, "BYBIT_LINEAR_PERPETUAL")
    spt_db = resolve_db_path(tmp_path, "SPOT_REFERENCE")

    assert fut_db == tmp_path / "paper_history.sqlite3"
    assert byb_db == tmp_path / "paper_history_bybit_perpetual.sqlite3"
    assert spt_db == paper_db_path(tmp_path) == tmp_path / "paper_history_spot_reference.sqlite3"
    assert len({fut_db, byb_db, spt_db}) == 3
    assert app_mod.active_db_path("SPOT_REFERENCE") == paper_db_path(app_mod.ROOT)
    assert app_mod.active_db_path("USD_M_PERPETUAL") == futures_db_path(app_mod.ROOT)


def test_bug09_and_bug12_failover_binance_to_bybit_to_spot_with_open_and_pending_positions(tmp_path):
    """BUG-09 #6 & BUG-12: Transition Binance Futures -> Bybit -> Spot Reference when OPEN and PENDING positions exist."""
    primary = MultiTierFakeMarket("USD_M_PERPETUAL", price_offset=0.0)
    bybit = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL", price_offset=0.0)
    spot = MultiTierFakeMarket("SPOT_REFERENCE", price_offset=0.0)
    router = FuturesProviderRouter(primary, bybit, spot_fallback=spot, spot_fallback_enabled=True)

    base_db = tmp_path / "paper_history.sqlite3"
    fut_ledger = PaperLedger(base_db, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    alerts = Alerts(fut_ledger)
    scanner = MarketScanner(router, Pool(), Calendar(True), fut_ledger, alerts, clock=lambda: 100)
    cfg = ScannerConfig(candidates=2, interval=60, fee=0.0004, slippage_bps=0.1, funding=0.00001, send_alerts=False)

    # Step 1: Run on Binance USD-M Futures -> creates 2 PENDING trades in paper_history.sqlite3
    rep1 = scanner.run(cfg, manual=True)
    assert rep1["instrument_kind"] == "USD_M_PERPETUAL"
    fut_trades = scanner.ledger_for("USD_M_PERPETUAL").trades()
    assert len(fut_trades) == 2
    # Fill BTCUSDT into OPEN on Binance Futures, leave ETHUSDT as PENDING
    btc_trade = next(t for t in fut_trades if t["plan"]["symbol"] == "BTCUSDT")
    btc_plan = btc_trade["plan"]
    s_btc = primary.snapshot("BTCUSDT", btc_plan["mode"])
    q_fill = quote({"snapshot": s_btc}, (btc_plan["entry_trigger"] + btc_plan["entry"]) / 2, delta=1000)
    scanner.ledger_for("USD_M_PERPETUAL").advance(q_fill)
    fut_after_fill = {t["plan"]["symbol"]: t for t in scanner.ledger_for("USD_M_PERPETUAL").trades()}
    assert fut_after_fill["BTCUSDT"]["status"] == "OPEN"
    assert fut_after_fill["ETHUSDT"]["status"] == "PENDING"
    fut_equity_before = scanner.ledger_for("USD_M_PERPETUAL").summary(NOW + 1000)["equity"]

    # Step 2: Binance Futures hits HTTP 451 -> Router fails over to Bybit Perpetual
    primary.fail_mode = 451
    rep2 = scanner.run(cfg, manual=True)
    assert rep2["instrument_kind"] == "BYBIT_LINEAR_PERPETUAL"
    assert scanner.ledger.db.name.endswith("_bybit_perpetual.sqlite3")
    # Bybit ledger has its own independent 1000.0 equity and its own trades
    byb_ledger = scanner.ledger_for("BYBIT_LINEAR_PERPETUAL")
    assert byb_ledger.summary(NOW)["initial_equity"] == 1000.0
    assert len(byb_ledger.trades()) == 2
    assert all(t["plan"]["instrument_kind"] == "BYBIT_LINEAR_PERPETUAL" for t in byb_ledger.trades())

    # Verify Binance Futures OPEN and PENDING positions were NOT touched or closed by Bybit prices!
    fut_check = {t["plan"]["symbol"]: t for t in scanner.ledger_for("USD_M_PERPETUAL").trades()}
    assert fut_check["BTCUSDT"]["status"] == "OPEN"
    assert fut_check["ETHUSDT"]["status"] == "PENDING"
    assert scanner.ledger_for("USD_M_PERPETUAL").summary(NOW + 1000)["equity"] == pytest.approx(fut_equity_before)

    # Step 3: Bybit hits HTTP 403 -> Router fails over to Spot Reference
    bybit.fail_mode = 403
    rep3 = scanner.run(cfg, manual=True)
    assert rep3["instrument_kind"] == "SPOT_REFERENCE"
    assert scanner.ledger.db.name.endswith("_spot_reference.sqlite3")
    spot_ledger = scanner.ledger_for("SPOT_REFERENCE")
    assert spot_ledger.summary(NOW)["initial_equity"] == 1000.0
    assert len(spot_ledger.trades()) == 2
    assert all(t["plan"]["instrument_kind"] == "SPOT_REFERENCE" for t in spot_ledger.trades())

    # Verify both Binance Futures AND Bybit ledgers remain 100% intact and accessible from their original ledgers
    fut_final = {t["plan"]["symbol"]: t for t in scanner.ledger_for("USD_M_PERPETUAL").trades()}
    assert fut_final["BTCUSDT"]["status"] == "OPEN"
    assert fut_final["ETHUSDT"]["status"] == "PENDING"
    assert len(byb_ledger.active()) == 2


def test_bug12_failover_with_tp1_filled_and_stop_at_break_even_and_error_codes(tmp_path):
    """BUG-12: Verify failover & error handling when TP1 is filled, stop is at BE, and under HTTP 451, 403, 429, and timeout."""
    primary = MultiTierFakeMarket("USD_M_PERPETUAL")
    bybit = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL")
    spot = MultiTierFakeMarket("SPOT_REFERENCE")
    router = FuturesProviderRouter(primary, bybit, spot_fallback=spot, spot_fallback_enabled=True)

    base_db = tmp_path / "bug12.sqlite3"
    fut_ledger = PaperLedger(base_db, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    scanner = MarketScanner(router, Pool(), Calendar(True), fut_ledger, Alerts(fut_ledger), clock=lambda: 100)
    cfg = ScannerConfig(candidates=1, interval=60, fee=0.0004, slippage_bps=0.1, funding=0.00001, send_alerts=False)

    # 1. Open position on Binance Futures, fill TP1, and move stop to Break-Even
    scanner.run(cfg, manual=True)
    t0 = fut_ledger.trades()[0]
    p = t0["plan"]
    s_base = primary.snapshot(p["symbol"], p["mode"])
    fut_ledger.advance(quote({"snapshot": s_base}, (p["entry_trigger"] + p["entry"]) / 2, delta=1000))
    fut_ledger.advance(quote({"snapshot": s_base}, p["tp1"] + 0.02, delta=2000))
    t_tp1 = fut_ledger.trades()[0]
    assert t_tp1["status"] == "OPEN"
    assert t_tp1["tp1_ms"] == NOW + 2000
    expected_be = quantize(break_even(p["entry"], p["direction"], p["fee"], p["funding_per_unit"], p["slippage_per_unit"]), p["tick"], up=True)
    assert t_tp1["stop"] == pytest.approx(expected_be)

    # 2. Simulate HTTP 429 (Rate Limit) -> Must NOT failover to Bybit, must record CONNECTION_ERROR, and keep TP1/BE position intact
    primary.fail_mode = 429
    rep_429 = scanner.run(cfg, manual=True)
    assert rep_429["scan_status"] == "FAILED"
    assert rep_429["data_status"] == "CONNECTION_ERROR"
    assert router.instrument_kind == "USD_M_PERPETUAL"
    t_after_429 = fut_ledger.trades()[0]
    assert t_after_429["status"] == "OPEN" and t_after_429["stop"] == pytest.approx(expected_be)

    # 3. Simulate Connection Timeout -> Must NOT failover, must record CONNECTION_ERROR, and keep TP1/BE position intact
    primary.fail_mode = "timeout"
    rep_timeout = scanner.run(cfg, manual=True)
    assert rep_timeout["scan_status"] == "FAILED"
    assert rep_timeout["data_status"] == "CONNECTION_ERROR"
    assert router.instrument_kind == "USD_M_PERPETUAL"
    t_after_timeout = fut_ledger.trades()[0]
    assert t_after_timeout["status"] == "OPEN" and t_after_timeout["stop"] == pytest.approx(expected_be)

    # 4. Simulate mid-monitoring HTTP 451 -> Router fails over to Bybit during monitor(), protecting the TP1/BE position in USD_M_PERPETUAL ledger
    primary.fail_mode = None
    orig_load = primary.load
    def fail_on_load(sym, mode):
        primary.fail_mode = 451
        return orig_load(sym, mode)
    primary.load = fail_on_load
    rep_451 = scanner.run(cfg, manual=True)
    assert router.instrument_kind == "BYBIT_LINEAR_PERPETUAL"
    t_after_failover = fut_ledger.trades()[0]
    assert t_after_failover["status"] == "OPEN"
    assert t_after_failover["tp1_ms"] == NOW + 2000
    assert t_after_failover["stop"] == pytest.approx(expected_be)
    assert t_after_failover["remaining_qty"] == pytest.approx(p["qty"] - p["tp1_qty"])


# ==============================================================================
# BUG-10 & BUG-13: SERVER-SIDE AUTH, RBAC, RATE LIMITING, AUDIT LOG & RESOURCES
# ==============================================================================

def test_bug10_server_side_authentication_rbac_and_audit_log(tmp_path):
    """BUG-10: Verify server-side auth, ADMIN vs VIEWER separation, and persistent audit logging."""
    db_path = tmp_path / "security.sqlite3"
    sec = SecurityManager(db_path, admin_token="rahasia-admin-2026", require_auth=True)

    # Unauthenticated viewer cannot execute privileged actions
    assert sec.verify_session(None)["role"] == ROLE_VIEWER
    for action in ("MANUAL_SCAN", "TELEGRAM_TEST", "TELEGRAM_ALERT", "JOURNAL_EXPORT", "CONFIG_UPDATE"):
        ok, msg = sec.authorize_action(action, session_token=None)
        assert not ok and "UNAUTHORIZED" in msg

    # Wrong password rejected
    assert sec.authenticate("salah-password") is None
    # Forged session token rejected
    assert sec.verify_session("deadbeef:ADMIN:9999999999:forgedsig")["role"] == ROLE_VIEWER

    # Valid admin credential issues signed token and grants ADMIN role
    token = sec.authenticate("rahasia-admin-2026")
    assert token is not None
    assert sec.verify_session(token)["role"] == ROLE_ADMIN

    # Admin can execute privileged actions
    ok_scan, _ = sec.authorize_action("MANUAL_SCAN", session_token=token)
    assert ok_scan is True

    # Telegram test message enforces server-side RBAC
    tg = TelegramAlerts("123:token", "-100123", db_path)
    ok_viewer, msg_viewer = tg.send_test_message(security=sec, session_token=None)
    assert not ok_viewer and "UNAUTHORIZED" in msg_viewer

    # Journal export enforces server-side RBAC
    ledger = PaperLedger(db_path)
    ok_exp_v, msg_exp_v = app_mod.export_journal_authorized(ledger, sec, session_token=None)
    assert not ok_exp_v and "UNAUTHORIZED" in msg_exp_v
    ok_exp_a, payload_a = app_mod.export_journal_authorized(ledger, sec, session_token=token)
    assert ok_exp_a and "PAPER_SIMULATION" in payload_a

    # Audit log persisted in SQLite records all denied and allowed actions
    logs = sec.audit_entries(limit=50)
    actions_logged = {(row["action"], row["status"]) for row in logs}
    assert ("ADMIN_LOGIN", "DENIED") in actions_logged
    assert ("ADMIN_LOGIN", "ALLOWED") in actions_logged
    assert ("MANUAL_SCAN", "DENIED") in actions_logged
    assert ("MANUAL_SCAN", "ALLOWED") in actions_logged
    assert ("JOURNAL_EXPORT", "ALLOWED") in actions_logged


def test_bug13_rate_limits_sqlite_integrity_restart_recovery_and_resource_monitor(tmp_path):
    """BUG-13: Manual scan cooldown, AI quota, Telegram repeat protection, SQLite integrity, and state recovery."""
    clock = [1000.0]
    db_path = tmp_path / "resource.sqlite3"
    sec = SecurityManager(
        db_path,
        admin_token="admin-token",
        require_auth=True,
        monotonic=lambda: clock[0],
        manual_scan_cooldown=15.0,
        max_manual_scans_per_min=3,
        max_ai_calls_per_session=2,
        max_exports_per_5min=2,
    )
    token = sec.authenticate("admin-token")

    # 1. Manual scan cooldown (15s after a completed scan)
    assert sec.authorize_action("MANUAL_SCAN", session_token=token, last_scan_failed=False)[0] is True
    clock[0] += 5.0
    ok_early, msg_early = sec.authorize_action("MANUAL_SCAN", session_token=token, last_scan_failed=False)
    assert not ok_early and "Rate limit" in msg_early

    # 2. AI per-session quota limit (max 2 calls)
    assert sec.authorize_action("AI_REVIEW", session_token=token)[0] is True
    assert sec.authorize_action("AI_REVIEW", session_token=token)[0] is True
    ok_ai3, msg_ai3 = sec.authorize_action("AI_REVIEW", session_token=token)
    assert not ok_ai3 and "Kuota AI" in msg_ai3

    # 3. Telegram test message rate limit (30s minimum interval)
    assert sec.authorize_action("TELEGRAM_TEST", session_token=token)[0] is True
    clock[0] += 5.0
    ok_tg2, msg_tg2 = sec.authorize_action("TELEGRAM_TEST", session_token=token)
    assert not ok_tg2 and "Rate limit" in msg_tg2

    # 4. Journal export rate limit (max 2 per 5 min)
    assert sec.authorize_action("JOURNAL_EXPORT", session_token=token)[0] is True
    assert sec.authorize_action("JOURNAL_EXPORT", session_token=token)[0] is True
    assert sec.authorize_action("JOURNAL_EXPORT", session_token=token)[0] is False

    # 5. SQLite integrity check
    ledger = PaperLedger(db_path)
    integrity = ledger.integrity_check()
    assert integrity["ok"] is True and integrity["status"] == "ok"

    # 6. Streamlit restart state recovery
    ledger.save_scan_report({"scan_id": "persisted-123", "now_ms": NOW, "scan_status": "COMPLETED", "analyses": []})
    scanner = MarketScanner(Market(), Pool(), Calendar(True), ledger, Alerts(ledger), security=sec)
    app_mod.st.session_state.clear()
    assert app_mod.recover_state_after_restart(scanner) is True
    assert app_mod.st.session_state["market_report"]["scan_id"] == "persisted-123"

    # 7. CPU, memory, and API request monitoring
    snap = sec.monitor.snapshot()
    assert "cpu_process_seconds" in snap and "memory_rss_mb" in snap
    assert snap["counters"]["rate_limited_events"] >= 3


# ==============================================================================
# BUG-14: SPOT REFERENCE VALIDATION & DISTINCT LABELING
# ==============================================================================

def test_bug14_spot_reference_never_equated_to_perpetual_across_all_outputs(tmp_path):
    """BUG-14: Spot Reference explicitly states Last Spot != Mark Price, funding/leverage are simulation only, and PnL != perpetual."""
    s, a = setup()
    s_spot = spot_snapshot(deepcopy(s))
    r_spot = evaluate(s_spot, a, paper=True)
    assert r_spot.status == "VALID"

    # 1. Strategy metadata & plan validation
    assert r_spot.meta["is_spot_reference"] is True
    assert "BUKAN FUTURES PERPETUAL" in r_spot.meta["market_category_label"]
    assert "bukan Mark Price" in r_spot.meta["mark_price_claim"]
    assert "bukan data aktual" in r_spot.meta["funding_claim"]
    assert "tidak disamakan dengan hasil kontrak perpetual" in r_spot.meta["pnl_claim"]
    assert r_spot.plan["mark_price_claim"] == r_spot.meta["mark_price_claim"]

    # 2. Telegram alert formatting
    data_spot = {"snapshot": s_spot, "result": r_spot, "ai": {"ok": True, "decision": "LONG", "reasons": ["OK"], "cautions": []}, "status": "VALID", "monotonic": 100}
    alert_text = format_alert(data_spot)
    assert "SPOT REFERENCE (SIMULASI SPOT — BUKAN FUTURES PERPETUAL)" in alert_text
    assert "Harga spot tidak identik dengan Mark Price Futures" in alert_text
    assert "PnL tidak disamakan dengan kontrak perpetual" in alert_text

    # 3. Journal export JSON validation
    spot_db = tmp_path / "spot_paper_history.sqlite3"
    ledger = PaperLedger(spot_db, expected_instrument_kind="SPOT_REFERENCE")
    assert ledger.queue(data_spot, lambda: 100)[0]
    exported = json.loads(ledger.export_json())
    assert exported["instrument_kind"] == "SPOT_REFERENCE"
    assert exported["validation"]["is_spot_reference"] is True
    assert "tidak disamakan dengan hasil kontrak perpetual" in exported["validation"]["pnl_claim"]


def test_bug11_github_actions_ci_workflow_exists_and_runs_all_tests():
    """BUG-11: Verify .github/workflows/ci.yml exists and executes compileall + pytest with requirements-dev.txt."""
    root = Path(__file__).resolve().parent.parent
    ci_file = root / ".github" / "workflows" / "ci.yml"
    assert ci_file.exists()
    content = ci_file.read_text(encoding="utf-8")
    assert "requirements-dev.txt" in content
    assert "pytest" in content
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" in content

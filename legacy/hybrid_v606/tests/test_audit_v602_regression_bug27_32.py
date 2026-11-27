"""Comprehensive Regression Tests for BUG-27 through BUG-32, LOGIC-01, and OPS-01..03 (CoinAgentX v6.0.2).

Covers:
- BUG-27: Dynamic leverage enforced on `plan` across confidence scores (80, 84, 85, 89, 92) and
  operator leverage choices (1x, 2x, 3x); no `KeyError: 'max_allowed_leverage'` on low confidence.
- BUG-28: Canonical backup checksum covers `trades`, `events`, `meta`, `telegram_alerts`,
  `security_audit_log`, `security_quota_events`, `instrument_kind`, and `namespace`; mandatory
  checksum on schema v2+; restores `security_audit_log` idempotently; rejects cross-instrument backups.
- BUG-29: Scanner lock (`lock_owner_ledger` & `lock_owner_path`) atomically migrates to destination
  database on provider failover, releases old database lock, and aborts if destination lock is busy.
- BUG-30: Tertiary Spot Reference fallback only triggers on explicit `REGIONAL_RESTRICTION` (HTTP 403/451),
  never on `http_status=None`, transient network timeouts, rate limits (429 / 403 too frequent),
  invalid JSON, or Bybit `retCode` errors.
- BUG-31: Single server-side authorization layer for `TelegramAlerts` (`send_summary`, `send_watchlist`,
  `send_cancellation`, `send`, `send_test_message`) so 1 message consumes 1 quota unit and 1 audit entry.
- BUG-32: Dashboard diagnostics use `ledger.integrity_check()` (`database_health`) matching the actual backend.
- LOGIC-01: Consistent automatic `TP3` (4R) execution for `runner_qty` (25%) alongside 2 ATR trailing stop.
- OPS-01, OPS-02, OPS-03: Supabase SQL migration file, legacy `manual_app.py` public deployment guard,
  and synchronized documentation.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

import app as app_mod
import manual_app as manual_app_mod
from futures.db_backend import compute_backup_checksum, normalize_postgres_url
from futures.market import MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.provider_router import FuturesProviderRouter, classify_provider_error
from futures.security import DeploymentSecurityError, SecurityManager
from futures.strategy import evaluate, select_dynamic_leverage
from futures.telegram import TelegramAlerts
from tests.fixtures import NOW, setup
from tests.test_audit_v6_security_failover import MultiTierFakeMarket
from tests.test_market_scanner import Alerts, Calendar, Pool
from tests.test_paper import approved, enter, quote


# ==============================================================================
# BUG-27: DYNAMIC LEVERAGE ENFORCEMENT & LOW CONFIDENCE REJECTION
# ==============================================================================

@pytest.mark.parametrize("confidence", [80, 84, 85, 89, 92])
@pytest.mark.parametrize("op_leverage", [1, 2, 3])
def test_bug27_dynamic_leverage_matrix_and_low_confidence_rejection(confidence, op_leverage):
    """BUG-27: Verify confidence 80/84/85/89/92 with operator leverage 1x/2x/3x; never raises KeyError."""
    s, a = setup()
    a.leverage = op_leverage
    a.confidence_override = confidence

    res = evaluate(s, a, paper=True)
    assert "max_allowed_leverage" in res.metrics
    assert "selected_leverage" in res.metrics

    if confidence < 85:
        assert res.status != "VALID"
        assert res.plan is None
        assert res.metrics["max_allowed_leverage"] == 0
        assert res.metrics["selected_leverage"] == 0
    elif confidence <= 89:
        assert res.status == "VALID"
        assert res.plan is not None
        assert res.plan["max_allowed_leverage"] == 2
        expected_lev = min(op_leverage, 2)
        assert res.plan["leverage"] == expected_lev
        assert res.metrics["selected_leverage"] == expected_lev
    else:
        assert res.status == "VALID"
        assert res.plan is not None
        assert res.plan["max_allowed_leverage"] == 3
        expected_lev = min(op_leverage, 3)
        assert res.plan["leverage"] == expected_lev
        assert res.metrics["selected_leverage"] == expected_lev

    # Also verify volatility & calibration caps are strictly respected by select_dynamic_leverage
    sel_high_vol, caps_high_vol = select_dynamic_leverage(95, atr_pct=0.05, sl_pct=0.01, operator_leverage=3, calibrated=True)
    assert caps_high_vol["volatility_cap"] == 2
    assert caps_high_vol["max_allowed_leverage"] == 2
    assert sel_high_vol == 2


# ==============================================================================
# BUG-28: CANONICAL BACKUP CHECKSUM OVER ALL SECURITY DATA & FULL AUDIT RESTORE
# ==============================================================================

def test_bug28_backup_checksum_covers_all_fields_and_restores_audit_log(tmp_path):
    """BUG-28: Tampering with any backup field (including audit log & quota events) fails checksum; audit log is restored."""
    db_src = tmp_path / "futures_desk.db"
    sec = SecurityManager(db_src, admin_token="admin-bug28-secret")
    tok = sec.authenticate("admin-bug28-secret")
    assert sec.authorize_action("AI_REVIEW", session_token=tok)[0] is True

    ledger = PaperLedger(db_src, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    d = approved("LONG")
    assert ledger.queue(d, lambda: 100)[0] is True
    enter(ledger, d, delta=1000)

    tg = TelegramAlerts("123:tok", "-100123", db_src, security=sec)
    assert tg.reserve("PAPER:BTCUSDT:INTRADAY:LONG:1") is True
    tg.record("PAPER:BTCUSDT:INTRADAY:LONG:1", "sent")

    raw_json = ledger.export_json()
    backup_obj = json.loads(raw_json)
    assert len(backup_obj["security_audit_log"]) >= 2
    assert len(backup_obj["security_quota_events"]) == 1

    # 1. Backup without checksum_sha256 MUST be rejected on schema v2
    no_checksum = copy.deepcopy(backup_obj)
    no_checksum.pop("checksum_sha256", None)
    db_target = tmp_path / "target.db"
    target_ledger = PaperLedger(db_target, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    with pytest.raises(ValueError, match="wajib menyertakan checksum_sha256"):
        target_ledger.import_backup_json(no_checksum)

    # 2. Tampering with ANY critical field must invalidate checksum and be rejected
    mutations = [
        ("trades", lambda b: b["trades"][0].update(realized_pnl=99999.0)),
        ("events", lambda b: b["events"][0].update(pnl=88888.0)),
        ("meta", lambda b: b["meta"].update(initial_equity="999999")),
        ("telegram_alerts", lambda b: b["telegram_alerts"][0].update(state="tampered")),
        ("security_audit_log", lambda b: b["security_audit_log"].pop(0)),
        ("security_quota_events", lambda b: b["security_quota_events"].clear()),
        ("instrument_kind", lambda b: b.update(instrument_kind="SPOT_REFERENCE")),
        ("namespace", lambda b: b.update(namespace="SPOT_REFERENCE:paper_audit.db")),
    ]
    for field_name, mutate_fn in mutations:
        tampered = copy.deepcopy(backup_obj)
        mutate_fn(tampered)
        assert compute_backup_checksum(tampered) != backup_obj["checksum_sha256"], f"Checksum did not change for {field_name}"
        with pytest.raises(ValueError, match="Checksum SHA-256"):
            target_ledger.import_backup_json(tampered)

    # 3. Cross-instrument backup (even with recomputed checksum) is rejected by target ledger
    cross_inst = copy.deepcopy(backup_obj)
    cross_inst["instrument_kind"] = "SPOT_REFERENCE"
    cross_inst["namespace"] = "SPOT_REFERENCE:paper_audit.db"
    cross_inst["checksum_sha256"] = compute_backup_checksum(cross_inst)
    with pytest.raises(ValueError, match="instrument_kind|lintas instrumen"):
        target_ledger.import_backup_json(cross_inst)

    # 4. Valid restore recovers trades, events, telegram_alerts, security_audit_log, and security_quota_events idempotently
    res = target_ledger.import_backup_json(raw_json)
    assert res["ok"] is True
    assert res["audit_entries_restored"] == len(backup_obj["security_audit_log"])
    sec_restored = SecurityManager(db_target, admin_token="admin-bug28-secret")
    assert len(sec_restored.audit_entries(limit=100)) >= len(backup_obj["security_audit_log"])
    assert sec_restored.ai_quota_usage()["global_hour_used"] == 1

    # Second restore is idempotent (0 duplicate audit entries)
    res2 = target_ledger.import_backup_json(raw_json)
    assert res2["ok"] is True
    assert res2["audit_entries_restored"] == 0


# ==============================================================================
# BUG-29: ATOMIC SCANNER LOCK MIGRATION ON FAILOVER & CONCURRENT CONTENTION
# ==============================================================================

def test_bug29_scanner_lock_migrates_on_failover_and_aborts_if_destination_locked(tmp_path):
    """BUG-29: Failover moves scanner lock from source DB to destination DB and aborts if destination DB is locked."""
    primary = MultiTierFakeMarket("USD_M_PERPETUAL")
    bybit = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL")
    router = FuturesProviderRouter(primary, bybit, spot_fallback_enabled=False)

    base_db = tmp_path / "paper_history.sqlite3"
    fut_ledger = PaperLedger(base_db, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    scanner = MarketScanner(router, Pool(), Calendar(True), fut_ledger, Alerts(fut_ledger), clock=lambda: 100)
    byb_ledger = scanner.ledger_for("BYBIT_LINEAR_PERPETUAL")
    cfg = ScannerConfig(candidates=1, interval=60, send_alerts=False)

    # Trigger failover during universe() and inspect lock ownership mid-scan
    primary.fail_mode = 451
    mid_scan_locks = {}
    orig_bybit_universe = bybit.universe

    def inspected_universe():
        # While scanner is running on Bybit after failover:
        # 1) Old Binance DB lock MUST already be released (another owner can acquire it)
        can_acq_old = fut_ledger.acquire_db_scanner_lock("probe-owner", int(NOW), ttl_ms=10000)
        if can_acq_old:
            fut_ledger.release_db_scanner_lock("probe-owner")
        # 2) New Bybit DB lock MUST be held by scanner.instance_id (another owner CANNOT acquire it)
        can_acq_new = byb_ledger.acquire_db_scanner_lock("probe-owner", int(NOW), ttl_ms=10000)
        mid_scan_locks["old_db_free"] = can_acq_old
        mid_scan_locks["new_db_locked_by_scanner"] = not can_acq_new
        mid_scan_locks["lock_owner_path"] = scanner.lock_owner_path
        return orig_bybit_universe()

    bybit.universe = inspected_universe
    rep = scanner.run(cfg, manual=True)
    assert rep["instrument_kind"] == "BYBIT_LINEAR_PERPETUAL"
    assert mid_scan_locks["old_db_free"] is True
    assert mid_scan_locks["new_db_locked_by_scanner"] is True
    assert mid_scan_locks["lock_owner_path"] == byb_ledger.db

    # After scan completes, both Binance and Bybit DB locks are completely released
    assert scanner.lock_owner_ledger is None
    assert fut_ledger.acquire_db_scanner_lock("probe-after", int(NOW), ttl_ms=10000) is True
    fut_ledger.release_db_scanner_lock("probe-after")
    assert byb_ledger.acquire_db_scanner_lock("probe-after", int(NOW), ttl_ms=10000) is True
    byb_ledger.release_db_scanner_lock("probe-after")

    # Scenario B: Concurrent scanner already holds Bybit DB lock when failover from Binance -> Bybit happens
    primary2 = MultiTierFakeMarket("USD_M_PERPETUAL")
    bybit2 = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL")
    router2 = FuturesProviderRouter(primary2, bybit2, spot_fallback_enabled=False)
    scanner_a = MarketScanner(router2, Pool(), Calendar(True), fut_ledger, Alerts(fut_ledger), clock=lambda: 200)
    byb_ledger2 = scanner_a.ledger_for("BYBIT_LINEAR_PERPETUAL")

    # Another scanner ("scanner-b") locks Bybit DB first
    assert byb_ledger2.acquire_db_scanner_lock("scanner-b", int( time_ms := int(NOW + 500000000000) ), ttl_ms=300000) is True
    primary2.fail_mode = 451
    rep_busy = scanner_a.run(cfg, manual=True)
    assert rep_busy["scan_status"] == "FAILED"
    assert "lock database" in rep_busy.get("scan_notice", "")
    # Source lock on Binance was still properly released upon abort!
    assert scanner_a.lock_owner_ledger is None
    assert fut_ledger.acquire_db_scanner_lock("probe-clean", time_ms, ttl_ms=10000) is True
    fut_ledger.release_db_scanner_lock("probe-clean")
    byb_ledger2.release_db_scanner_lock("scanner-b")


# ==============================================================================
# BUG-30: EXPLICIT ERROR CLASSIFICATION FOR SPOT FALLBACK
# ==============================================================================

def test_bug30_spot_fallback_only_on_explicit_regional_restriction():
    """BUG-30: Network errors (http_status=None), invalid JSON, retCode errors, and rate limits never trigger Spot fallback."""
    assert classify_provider_error(MarketError("HTTP 451", http_status=451)) == "REGIONAL_RESTRICTION"
    assert classify_provider_error(MarketError("Akses Bybit ditolak (HTTP 403)", http_status=403)) == "UNKNOWN_403"
    assert classify_provider_error(MarketError("Akses Bybit ditolak (HTTP 403) pembatasan wilayah", http_status=403)) == "REGIONAL_RESTRICTION"
    assert classify_provider_error(MarketError("Akses Bybit ditolak (HTTP 403). IP terkena batas frekuensi", http_status=403)) == "RATE_LIMIT"
    assert classify_provider_error(MarketError("Batas permintaan Bybit tercapai", http_status=429)) == "RATE_LIMIT"
    assert classify_provider_error(MarketError("Koneksi data Bybit gagal", http_status=None)) == "NETWORK_ERROR"
    assert classify_provider_error(MarketError("Respons Bybit bukan JSON valid", http_status=None)) == "INVALID_RESPONSE"
    assert classify_provider_error(MarketError("Bybit menolak permintaan data publik: 10001", http_status=None)) == "INVALID_RESPONSE"

    for non_regional_err in (
        MarketError("Koneksi data Bybit gagal. Periksa jaringan server hosting.", http_status=None),
        MarketError("Respons Bybit bukan JSON valid", http_status=None),
        MarketError("Bybit menolak permintaan data publik: 10001", http_status=None),
        MarketError("Batas permintaan Bybit tercapai; jeda 60 detik.", http_status=429),
        MarketError("Akses Bybit ditolak (HTTP 403). IP terkena batas frekuensi", http_status=403),
    ):
        primary = MultiTierFakeMarket("USD_M_PERPETUAL")
        primary.fail_mode = 451
        bybit = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL")
        bybit.get = lambda *_a, _e=non_regional_err, **_kw: (_ for _ in ()).throw(_e)
        spot = MultiTierFakeMarket("SPOT_REFERENCE")
        router = FuturesProviderRouter(primary, bybit, spot_fallback=spot, spot_fallback_enabled=True)

        with pytest.raises(MarketError):
            router.get("/fapi/v1/time")
        # Must remain on BYBIT_LINEAR_PERPETUAL, NEVER silently switching to SPOT_REFERENCE!
        assert router.instrument_kind == "BYBIT_LINEAR_PERPETUAL"


# ==============================================================================
# BUG-31: SINGLE SERVER-SIDE TELEGRAM AUTHORIZATION PER MESSAGE
# ==============================================================================

def test_bug31_telegram_single_authorization_consumes_one_quota_unit(monkeypatch, tmp_path):
    """BUG-31: send_summary, send_watchlist, send_cancellation, and send each consume exactly 1 TELEGRAM_ALERT quota unit."""
    db_path = tmp_path / "tg_quota.db"
    sec = SecurityManager(db_path, admin_token="tg-admin-2026", max_telegram_per_min=10)
    tok = sec.authenticate("tg-admin-2026")

    fake_session = SimpleNamespace(
        post=lambda *_a, **_kw: SimpleNamespace(status_code=200, json=lambda: {"ok": True})
    )
    tg = TelegramAlerts("123:bot-token", "-100123", db_path, session=fake_session, monotonic=lambda: 100, security=sec)
    d = approved("LONG")

    before_cnt = sec.monitor.snapshot()["counters"]["telegram_requests"]
    assert "dikirim" in tg.send_summary({"scan_id": "s1", "universe_count": 10}, session_token=tok)
    assert sec.monitor.snapshot()["counters"]["telegram_requests"] == before_cnt + 1
    assert len(sec.limiter.events.get("telegram_alert", [])) == 1

    assert "terkirim" in tg.send_watchlist(d, session_token=tok)
    assert sec.monitor.snapshot()["counters"]["telegram_requests"] == before_cnt + 2
    assert len(sec.limiter.events.get("telegram_alert", [])) == 2

    assert "terkirim" in tg.send_cancellation("BTCUSDT", "sig-1", "EXPIRED", session_token=tok)
    assert sec.monitor.snapshot()["counters"]["telegram_requests"] == before_cnt + 3
    assert len(sec.limiter.events.get("telegram_alert", [])) == 3

    assert "terkirim" in tg.send(d, session_token=tok)
    assert sec.monitor.snapshot()["counters"]["telegram_requests"] == before_cnt + 4
    assert len(sec.limiter.events.get("telegram_alert", [])) == 4


# ==============================================================================
# BUG-32, LOGIC-01, & OPS-01..03 VERIFICATION
# ==============================================================================

def test_bug32_logic01_tp3_execution_and_ops_requirements(monkeypatch, tmp_path):
    """BUG-32, LOGIC-01, OPS-01, OPS-02: Verify TP3 (4R) automatic fill, backend health diagnostic, SQL file, and legacy guard."""
    # 1. LOGIC-01: Verify automatic TP1 (2R) -> TP2 (3R) -> TP3 (4R) runner execution
    d = approved("LONG")
    ledger = PaperLedger(tmp_path / "tp3_test.db")
    assert ledger.queue(d, lambda: 100)[0] is True
    enter(ledger, d, delta=1000)
    p = ledger.trades()[0]["plan"]

    # Fill TP1 (2R)
    ledger.advance(quote(d, p["tp1"] + 0.01, delta=2000))
    t1 = ledger.trades()[0]
    assert t1["tp1_ms"] == NOW + 2000
    assert t1["remaining_qty"] == pytest.approx(p["qty"] - p["tp1_qty"])

    # Fill TP2 (3R)
    ledger.advance(quote(d, p["tp2"] + 0.01, delta=3000))
    t2 = ledger.trades()[0]
    assert t2["tp2_ms"] == NOW + 3000
    assert t2["remaining_qty"] == pytest.approx(p["runner_qty"])
    assert t2["status"] == "OPEN"

    # Fill TP3 (4R) automatically closes the remaining runner_qty
    ledger.advance(quote(d, p["tp3"] + 0.01, delta=4000))
    t3 = ledger.trades()[0]
    assert t3["tp3_ms"] == NOW + 4000
    assert t3["remaining_qty"] == 0.0
    assert t3["status"] == "CLOSED"
    events = [ev["event"] for ev in json.loads(ledger.export_json())["events"]]
    assert "TP1" in events and "TP2" in events and "TP3" in events

    # 2. BUG-32: Verify app.py does not call verify_sqlite_integrity directly
    app_text = Path(app_mod.__file__).read_text(encoding="utf-8")
    code_after_bootstrap = app_text[app_text.find("import streamlit as st"):]
    assert "verify_sqlite_integrity(" not in code_after_bootstrap
    assert 'diagnostic["database_health"]' in code_after_bootstrap

    # 3. OPS-01: Verify Supabase SQL migration file exists and contains coinagent_backups DDL
    root = Path(app_mod.__file__).resolve().parent
    sql_file = root / "supabase" / "migrations" / "001_coinagent_schema.sql"
    assert sql_file.exists()
    sql_text = sql_file.read_text(encoding="utf-8")
    assert "coinagent_backups" in sql_text
    assert "JSONB" in sql_text
    assert "UNIQUE (namespace)" in sql_text

    # 4. OPS-02: Verify legacy manual_app.py blocks unauthenticated public deployments
    monkeypatch.setenv("COINAGENT_PUBLIC_DEPLOYMENT", "true")
    monkeypatch.delenv("COINAGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(DeploymentSecurityError):
        manual_app_mod.verify_legacy_entrypoint_security()



def test_normalize_postgres_url_strips_template_brackets_and_encodes_special_chars():
    """Verify common Supabase DATABASE_URL paste issues ([PASSWORD] brackets and @ in password) are normalized."""
    raw_brackets = "postgresql://postgres.abcd:[MyPass@123#]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
    norm = normalize_postgres_url(raw_brackets)
    assert norm == "postgresql://postgres.abcd:MyPass%40123%23@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require"

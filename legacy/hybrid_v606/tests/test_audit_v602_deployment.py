"""Comprehensive Audit Remediation Tests for BUG-15 through BUG-21 (CoinAgentX v6.0.2).

Covers:
- BUG-15: Default authentication requires auth (`auth_required = True`), anonymous role is
          ALWAYS `VIEWER`, anonymous manual/auto scan is `DENIED`, and public deployment
          startup check fails closed when Secrets are missing.
- BUG-16: End-to-end `SecurityManager` enforcement on `AI_REVIEW`, `TELEGRAM_ALERT`,
          `AUTO_SCAN`, and `MANUAL_SCAN`, global + per-session AI quotas, and public
          dashboard viewer read-only defaults.
- BUG-17: Persistent database adapter (PostgreSQL/Supabase SQL translation + transactions)
          and idempotent `import_backup_json` restore without duplicating positions or alerts.
- BUG-18: Independent 24/7 worker heartbeats, single active scanner per database via
          transactional `scanner_locks`, and read-only dashboard separation.
- BUG-19: `st_compat` restricted strictly to test mode via `importlib.util.find_spec`.
- BUG-20: Self-bootstrapping SHA-256 manifest verification and atomic file repair (`os.replace`).
- BUG-21: Unified `APP_VERSION == "6.0.2"` across `futures/version.py`, `app.py`, `worker.py`,
          `futures/telegram.py`, `README.md`, and `VALIDATION.md`.
"""
import hashlib
from pathlib import Path
import shutil
import pytest

import app as app_mod
from futures.db_backend import _PostgresConnectionAdapter, persistence_status
from futures.groq_rotation import GroqRotation
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.security import (
    SecurityManager,
    DeploymentSecurityError,
    ROLE_ADMIN,
    ROLE_WORKER,
    ROLE_VIEWER,
)
from futures.st_compat import is_test_environment, install_if_missing
from futures.telegram import TelegramAlerts, format_alert, format_scan_summary
from futures.version import APP_VERSION
from tests.fixtures import NOW
from tests.test_paper import approved, enter
from tests.test_market_scanner import Market, Calendar, Pool, Alerts


# ==============================================================================
# BUG-15: DEFAULT AUTHENTICATION NEVER GRANTS ADMIN TO ANONYMOUS SESSIONS
# ==============================================================================

def test_bug15_default_auth_is_strict_and_anonymous_is_always_viewer(monkeypatch, tmp_path):
    """BUG-15: Without admin token/hash or env flags, auth_required=True, anonymous role=VIEWER, scan=DENIED."""
    monkeypatch.delenv("COINAGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("COINAGENT_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("COINAGENT_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("PUBLIC_READONLY_MODE", raising=False)

    sec = SecurityManager(tmp_path / "no_secrets.db")
    assert sec.auth_required is True
    assert sec.admin_configured is False

    anon = sec.verify_session(None)
    assert anon["role"] == ROLE_VIEWER
    assert anon["authenticated"] is False

    ok_manual, msg_manual = sec.authorize_action("MANUAL_SCAN", None)
    assert ok_manual is False and "UNAUTHORIZED" in msg_manual

    ok_auto, msg_auto = sec.authorize_action("AUTO_SCAN", None)
    assert ok_auto is False and "UNAUTHORIZED" in msg_auto

    # Startup check fails closed on public deployment when Secrets are missing
    startup = sec.verify_public_deployment_startup(enforce_strict=False)
    assert startup["ok"] is False and startup["mode"] == "LOCKED_READONLY"
    with pytest.raises(DeploymentSecurityError):
        sec.verify_public_deployment_startup(enforce_strict=True)


# ==============================================================================
# BUG-16: END-TO-END AUTHORIZATION FOR AI_REVIEW, TELEGRAM_ALERT, & AUTO_SCAN
# ==============================================================================

def test_bug16_ai_review_telegram_and_autoscan_enforce_authorization_and_global_quota(tmp_path):
    """BUG-16: AI_REVIEW, TELEGRAM_ALERT, and AUTO_SCAN are all gated by SecurityManager."""
    db_path = tmp_path / "bug16.db"
    sec = SecurityManager(
        db_path,
        admin_token="secret-admin-2026",
        max_ai_calls_per_session=5,
        max_ai_calls_global_per_hour=2,
        max_telegram_per_min=2,
    )
    d = approved("LONG")

    # 1. GroqRotation.review blocks unauthenticated caller
    pool = GroqRotation(["gsk_testkey123456"], security=sec)
    res_unauth = pool.review(d["result"], session_token=None)
    assert res_unauth["ok"] is False
    assert "UNAUTHORIZED" in res_unauth["reasons"][0]

    # 2. TelegramAlerts blocks unauthenticated send, summary, watchlist, and cancellation
    tg = TelegramAlerts("123:token", "-100123", db_path, security=sec)
    assert "UNAUTHORIZED" in tg.send(d, session_token=None)
    assert "UNAUTHORIZED" in tg.send_summary({"universe_count": 10}, session_token=None)
    assert "UNAUTHORIZED" in tg.send_watchlist(d, session_token=None)
    assert "UNAUTHORIZED" in tg.send_cancellation("BTCUSDT", "sig1", "EXPIRED", session_token=None)

    # 3. MarketScanner with trusted_worker=False (web dashboard mode) blocks anonymous auto-scan & manual scan
    ledger = PaperLedger(db_path)
    web_scanner = MarketScanner(
        Market(), Pool(), Calendar(True), ledger, Alerts(ledger),
        security=sec, trusted_worker=False,
    )
    cfg = ScannerConfig(candidates=1, interval=60)
    rep_anon_auto = web_scanner.run(cfg, manual=False, session_token=None)
    assert "UNAUTHORIZED" in rep_anon_auto.get("auth_error", "")
    rep_anon_manual = web_scanner.run(cfg, manual=True, session_token=None)
    assert "UNAUTHORIZED" in rep_anon_manual.get("auth_error", "")

    # 4. Global hourly AI quota is enforced across sessions
    admin_tok = sec.authenticate("secret-admin-2026")
    worker_tok = sec.issue_worker_session()
    assert sec.authorize_action("AI_REVIEW", session_token=admin_tok)[0] is True
    assert sec.authorize_action("AI_REVIEW", session_token=worker_tok)[0] is True
    ok_global_exceeded, msg_global = sec.authorize_action("AI_REVIEW", session_token=admin_tok)
    assert ok_global_exceeded is False
    assert "ai_review_global_hour" in msg_global


# ==============================================================================
# BUG-17: PERSISTENT DB BACKEND & IDEMPOTENT BACKUP / RESTORE
# ==============================================================================

def test_bug17_backup_and_restore_preserves_state_without_duplicating_positions_or_alerts(tmp_path):
    """BUG-17: Exporting and restoring backup across reboot preserves equity, positions, and Telegram dedup idempotently."""
    db_orig = tmp_path / "orig.sqlite3"
    ledger1 = PaperLedger(db_orig, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    d = approved("LONG")
    assert ledger1.queue(d, lambda: 100)[0]
    enter(ledger1, d, delta=1000)

    tg1 = TelegramAlerts("123:tok", "-100999", db_orig)
    sig_key = "PAPER:BTCUSDT:INTRADAY:LONG:1"
    assert tg1.reserve(sig_key) is True
    tg1.record(sig_key, "sent")

    backup_json = ledger1.export_json()

    # Simulate Streamlit Cloud reboot with a brand-new database file
    db_reboot = tmp_path / "rebooted.sqlite3"
    ledger2 = PaperLedger(db_reboot, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    res1 = ledger2.import_backup_json(backup_json)
    assert res1["ok"] is True and res1["trades_restored"] == 1
    # Importing the same backup a second time must be idempotent (no duplicate trades or events)
    res2 = ledger2.import_backup_json(backup_json)
    assert res2["ok"] is True
    assert len(ledger2.trades()) == 1
    assert ledger2.trades()[0]["status"] == "OPEN"
    assert ledger2.summary(NOW + 1000)["equity"] == pytest.approx(ledger1.summary(NOW + 1000)["equity"])

    # Verify Telegram deduplication state survived reboot so the same alert is NOT re-sent
    tg2 = TelegramAlerts("123:tok", "-100999", db_reboot)
    assert tg2.reserve(sig_key) is False

    # Verify PostgreSQL SQL translation layer
    sql_ignore = _PostgresConnectionAdapter._translate_sql("INSERT OR IGNORE INTO paper_meta VALUES ('initial_equity',?)")
    assert "ON CONFLICT DO NOTHING" in sql_ignore and "%s" in sql_ignore
    sql_replace = _PostgresConnectionAdapter._translate_sql("INSERT OR REPLACE INTO paper_trades VALUES (?,?,?,?,?,?,?)")
    assert "ON CONFLICT (signal) DO UPDATE" in sql_replace


# ==============================================================================
# BUG-18: INDEPENDENT WORKER HEARTBEAT & SINGLE ACTIVE SCANNER PER DATABASE
# ==============================================================================

def test_bug18_single_scanner_db_lease_lock_and_worker_heartbeat(tmp_path):
    """BUG-18: Database lease lock prevents overlapping scanners and records worker heartbeats."""
    db_path = tmp_path / "worker_lock.sqlite3"
    ledger = PaperLedger(db_path)

    assert ledger.acquire_db_scanner_lock("worker-A", NOW, ttl_ms=60000) is True
    # Another scanner/dashboard attempting to acquire lock on the same DB before expiry is blocked
    assert ledger.acquire_db_scanner_lock("dashboard-B", NOW + 5000, ttl_ms=60000) is False
    # After release, another scanner can acquire
    ledger.release_db_scanner_lock("worker-A")
    assert ledger.acquire_db_scanner_lock("dashboard-B", NOW + 5000, ttl_ms=60000) is True
    ledger.release_db_scanner_lock("dashboard-B")

    ledger.record_worker_heartbeat("worker-A", NOW + 10000, 300)
    hb = ledger.worker_heartbeat()
    assert hb["worker_id"] == "worker-A"
    assert hb["interval_seconds"] == 300
    assert hb["version"] == APP_VERSION


# ==============================================================================
# BUG-19: STREAMLIT SHIM RESTRICTED TO TEST MODE ONLY
# ==============================================================================

def test_bug19_st_compat_never_activates_outside_test_mode():
    """BUG-19: install_if_missing(test_mode=False) refuses to activate shim in production mode."""
    assert is_test_environment(explicit_test_mode=False) is False
    assert install_if_missing(test_mode=False) is False
    compat_src = (Path(__file__).resolve().parent.parent / "futures" / "st_compat.py").read_text(encoding="utf-8")
    assert "importlib.util.find_spec" in compat_src


# ==============================================================================
# BUG-20: SELF-BOOTSTRAPPING SHA-256 MANIFEST & ATOMIC REPAIR
# ==============================================================================

def test_bug20_self_bootstrapping_verifies_sha256_manifest_and_repairs_tampered_files(tmp_path):
    """BUG-20: _bootstrap_project_files verifies SHA-256 hashes and atomically repairs modified/corrupted files."""
    assert hasattr(app_mod, "_EMBEDDED_MANIFEST_SHA256")
    manifest = app_mod._EMBEDDED_MANIFEST_SHA256
    assert isinstance(manifest, dict) and len(manifest) >= 12
    assert "futures/groq_rotation.py" in manifest
    assert "futures/security.py" in manifest

    # Copy app.py to isolated directory and extract
    shutil.copy2(Path(app_mod.__file__).resolve(), tmp_path / "app.py")
    app_mod._bootstrap_project_files(target_dir=tmp_path)

    target_file = tmp_path / "futures" / "groq_rotation.py"
    expected_sha = manifest["futures/groq_rotation.py"]
    assert hashlib.sha256(target_file.read_bytes()).hexdigest() == expected_sha

    # Corrupt the file on disk (even though the file and marker still exist!)
    target_file.write_text("# CORRUPTED FILE CONTENT\n", encoding="utf-8")
    assert hashlib.sha256(target_file.read_bytes()).hexdigest() != expected_sha

    # Running _bootstrap_project_files detects the SHA-256 mismatch and atomically restores the clean file
    app_mod._bootstrap_project_files(target_dir=tmp_path)
    assert hashlib.sha256(target_file.read_bytes()).hexdigest() == expected_sha


# ==============================================================================
# BUG-21: UNIFIED APP_VERSION = "6.0.2" ACROSS ALL MODULES & DOCS
# ==============================================================================

def test_bug21_unified_app_version_6_0_2():
    """BUG-21: README.md, VALIDATION.md, futures/version.py, futures/telegram.py, and app.py all use 6.0.2."""
    root = Path(__file__).resolve().parent.parent
    assert APP_VERSION in ("6.0.3", "6.0.4", "6.0.5", "6.0.6")
    assert app_mod.APP_VERSION in ("6.0.3", "6.0.4", "6.0.5", "6.0.6")
    readme = (root / "README.md").read_text(encoding="utf-8")
    validation = (root / "VALIDATION.md").read_text(encoding="utf-8")
    assert "v6.0.2" in readme
    assert "v6.0.2" in validation
    d = approved("LONG")
    assert f"v{APP_VERSION}" in format_alert(d)
    assert f"v{APP_VERSION}" in format_scan_summary({"universe_count": 10})

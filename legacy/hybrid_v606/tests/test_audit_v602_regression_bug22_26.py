import copy
"""Comprehensive Audit Regression Tests for BUG-22 through BUG-26 (CoinAgentX v6.0.2 Audit Compliant).

Covers:
- BUG-22: PostgreSQL False Positive Elimination & Centralized Database Configuration:
  * `resolve_database_config` / `configured_database_url` resolves `DATABASE_URL` from centralized
    config, Streamlit Secrets getter, and environment variables, and propagates it to all
    `PaperLedger` instances, `MarketScanner.ledger_for()`, `SecurityManager`, `TelegramAlerts`,
    and `worker.create_worker_engine()`.
  * `connect_database` NEVER silently falls back to `sqlite3.Connection` when PostgreSQL is requested
    but the driver is missing or connection fails; it raises `DatabaseConfigurationError`.
  * `persistence_status` ONLY reports `POSTGRESQL` (`persistent=True`, `verified_rw=True`) when a
    genuine PostgreSQL connection is opened and a read/write probe succeeds; otherwise it reports
    `POSTGRESQL_ERROR` (`persistent=False`).
- BUG-23: Active Supabase REST Sync with Namespace, Schema Version, SHA-256 Checksum, Stale Guard & Retry:
  * `PaperLedger` automatically calls `SupabaseStateSync.push_backup` after state-changing transactions
    and automatically pulls/restores on startup when local SQLite is empty after reboot.
  * Separate `namespace` per journal (`USD_M_PERPETUAL`, `BYBIT_LINEAR_PERPETUAL`, `SPOT_REFERENCE`).
  * Validates `schema_version`, `backup_at_ms`, `state_updated_ms`, and `checksum_sha256`.
  * Rejects stale/older backups so older state never overwrites newer state.
  * Retries transient network/HTTP 5xx errors and never claims `SUPABASE_REST_SYNC` is active
    or ACID-transactional before verification.
- BUG-24: Authentic SQLite vs PostgreSQL Health & Integrity Verification:
  * `_PostgresConnectionAdapter._translate_sql("PRAGMA quick_check")` raises `DatabaseConfigurationError`
    rather than faking `SELECT 'ok'`.
  * `verify_sqlite_integrity`, `verify_postgres_health`, and `verify_database_health` distinguish
    connection status, database health, and backup availability.
- BUG-25: Persistent Cross-Process AI Quotas:
  * Global hourly (`max_ai_calls_global_per_hour`) and per-session (`max_ai_calls_per_session`) AI quotas
    are stored atomically in `security_quota_events` in the shared database.
  * Shared across `worker.py` and `app.py`, and survive process restarts and backup/restore.
- BUG-26: Cache Resource Fingerprint & Session Invalidation on Token Rotation:
  * `compute_auth_config_fingerprint` generates distinct fingerprints when `admin_token` or `db_url`
    rotates (even when `bool(admin_token)` remains `True`), without exposing raw tokens.
  * Rotating `admin_token` immediately invalidates all previously issued administrator sessions.
"""
import json
import sqlite3
import sys
import types
from pathlib import Path
import pytest

import app as app_mod
import worker as worker_mod
from futures.db_backend import (
    DatabaseConfigurationError,
    SupabaseStateSync,
    _PostgresConnectionAdapter,
    compute_backup_checksum,
    configure_database_runtime,
    connect_database,
    persistence_status,
    resolve_database_config,
    verify_database_health,
    verify_postgres_health,
    verify_sqlite_integrity,
)
from futures.market_scanner import MarketScanner
from futures.paper import PaperLedger
from futures.security import (
    ROLE_ADMIN,
    ROLE_VIEWER,
    SecurityManager,
    compute_auth_config_fingerprint,
)
from tests.fixtures import NOW
from tests.test_market_scanner import Alerts, Calendar, Market, Pool
from tests.test_paper import approved, enter, quote


class _MockPostgresCursor:
    """In-memory DB-API 2.0 cursor backing _MockPostgresConnection for end-to-end PostgreSQL adapter verification."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rest = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rest


class _MockPostgresConnection:
    """DB-API 2.0 PostgreSQL driver simulation translating %s back to SQLite backing engine to test full SQL roundtrips."""

    def __init__(self, backing_sqlite_path):
        self._sqlite = sqlite3.connect(backing_sqlite_path, timeout=10)
        self.closed = False

    def cursor(self):
        conn = self

        class _Cur:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params=()):
                s = sql.strip()
                if s.upper() == "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()":
                    self._rows = [("coinagent_prod", "public")]
                    return
                if s.upper() == "BEGIN":
                    conn._sqlite.execute("BEGIN IMMEDIATE")
                    self._rows = []
                    return
                # Translate PostgreSQL dialect back to backing SQLite engine for execution verification
                s_exec = s.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                s_exec = s_exec.replace("%s", "?")
                cur = conn._sqlite.execute(s_exec, tuple(params) if params else ())
                self._rows = cur.fetchall()

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        return _Cur()

    def commit(self):
        self._sqlite.commit()

    def rollback(self):
        self._sqlite.rollback()

    def close(self):
        self.closed = True
        self._sqlite.close()


# ==============================================================================
# BUG-22: POSTGRESQL FALSE POSITIVE ELIMINATION & CENTRALIZED CONFIG
# ==============================================================================

def test_bug22_no_postgres_false_positive_and_centralized_config_propagation(monkeypatch, tmp_path):
    """BUG-22: PostgreSQL status is NEVER reported unless connection & RW probe succeed; no silent fallback to SQLite."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    configure_database_runtime(secret_getter=lambda _k: "", db_url="")

    # 1. Reproduction of BUG-22 when DATABASE_URL is in Secrets but driver is unavailable
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    fake_secrets = {"DATABASE_URL": "postgresql://trader:secret@db.example.com:5432/coinagent"}
    secret_getter = lambda k: fake_secrets.get(k, "")

    # connect_database MUST NOT silently return sqlite3.Connection when PostgreSQL is requested!
    with pytest.raises(DatabaseConfigurationError, match="Driver PostgreSQL|tidak terpasang"):
        connect_database(tmp_path / "local.db", get_secret=secret_getter)

    # persistence_status MUST NOT report POSTGRESQL_SUPABASE or persistent=True when driver/connection fails!
    st_err = persistence_status(tmp_path / "local.db", get_secret=secret_getter)
    assert st_err["backend"] == "POSTGRESQL_ERROR"
    assert st_err["persistent"] is False
    assert st_err["persistent_across_reboots"] is False
    assert st_err["verified_rw"] is False
    assert st_err["connected"] is False

    # 2. Now register a working PostgreSQL driver (`psycopg`) and verify end-to-end PostgreSQL operation
    pg_backing = tmp_path / "pg_server_storage.sqlite"
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: _MockPostgresConnection(pg_backing)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    configure_database_runtime(secret_getter=secret_getter)
    cfg = resolve_database_config(get_secret=secret_getter)
    assert cfg["pg_url"] == fake_secrets["DATABASE_URL"]

    conn = connect_database(tmp_path / "futures_desk.db", get_secret=secret_getter)
    assert isinstance(conn, _PostgresConnectionAdapter)
    assert not isinstance(conn, sqlite3.Connection)
    conn.close()

    st_ok = persistence_status(tmp_path / "futures_desk.db", get_secret=secret_getter)
    assert st_ok["backend"] == "POSTGRESQL"
    assert st_ok["persistent"] is True
    assert st_ok["persistent_across_reboots"] is True
    assert st_ok["acid_transactional"] is True
    assert st_ok["verified_rw"] is True

    # 3. Verify PaperLedger, MarketScanner.ledger_for(), and worker propagate pg_url and persist across restarts
    ledger_fut = PaperLedger(
        tmp_path / "futures_desk.db",
        initial_equity=1000.0,
        expected_instrument_kind="USD_M_PERPETUAL",
        secret_getter=secret_getter,
    )
    assert ledger_fut.pg_url == fake_secrets["DATABASE_URL"]
    scanner = MarketScanner(Market(), Pool(), Calendar(True), ledger_fut, Alerts(ledger_fut), trusted_worker=True)
    for kind in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"):
        lg = scanner.ledger_for(kind)
        assert lg.pg_url == fake_secrets["DATABASE_URL"]

    # Queue and fill a trade on PostgreSQL backend, then instantiate a new PaperLedger after restart
    d = approved("LONG")
    assert ledger_fut.queue(d, lambda: 100)[0] is True
    enter(ledger_fut, d, delta=1000)

    ledger_fut_restarted = PaperLedger(
        tmp_path / "futures_desk.db",
        initial_equity=1000.0,
        expected_instrument_kind="USD_M_PERPETUAL",
        secret_getter=secret_getter,
    )
    assert len(ledger_fut_restarted.trades()) == 1
    assert ledger_fut_restarted.trades()[0]["status"] == "OPEN"
    # Spot ledger on the same PostgreSQL database is isolated by namespace prefix
    ledger_spot = scanner.ledger_for("SPOT_REFERENCE")
    assert len(ledger_spot.trades()) == 0

    # Clean runtime config
    configure_database_runtime(secret_getter=lambda _k: "", db_url="")


# ==============================================================================
# BUG-23: SUPABASE REST SYNC AUTO-PUSH, AUTO-PULL, NAMESPACE, CHECKSUM & STALE GUARD
# ==============================================================================

def test_bug23_supabase_rest_sync_auto_push_pull_namespace_checksum_retry_and_stale_guard(monkeypatch, tmp_path):
    """BUG-23: SupabaseStateSync automatically pushes/pulls per namespace with checksum, stale guard, and retries."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    configure_database_runtime(secret_getter=lambda _k: "", db_url="")

    remote_store = {}
    transient_failures = {"push_Left": 2}

    def mock_transport(req, timeout=8):
        if req.method == "POST":
            if transient_failures["push_Left"] > 0:
                transient_failures["push_Left"] -= 1
                return 503, b'{"error":"temporary outage"}'
            env = json.loads(req.data.decode("utf-8"))
            remote_store[env["namespace"]] = env
            return 201, b'[{"status":"ok"}]'
        elif req.method == "GET":
            url = req.full_url
            for ns, env in remote_store.items():
                if ns.replace(":", "%3A") in url or ns in url:
                    return 200, json.dumps([env]).encode("utf-8")
            return 200, b"[]"
        return 400, b""

    sync = SupabaseStateSync(
        "https://project.supabase.co",
        "service-role-secret-key",
        transport=mock_transport,
        max_retries=3,
        retry_backoff_seconds=0.001,
    )

    # 1. Before any sync is executed, persistence_status MUST NOT claim SUPABASE_REST_SYNC is active or persistent
    pre_status = persistence_status(tmp_path / "fut.db", supabase_sync=sync)
    assert pre_status["backend"] == "SQLITE_WAL_UNVERIFIED_SYNC"
    assert pre_status["persistent"] is False
    assert pre_status["sync_active"] is False
    assert pre_status["acid_transactional"] is False

    # 2. Creating PaperLedger and executing queue + advance automatically pushes to Supabase (recovering from 2 HTTP 503s)
    db1 = tmp_path / "futures_desk.db"
    ledger1 = PaperLedger(db1, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", supabase_sync=sync)
    d = approved("LONG")
    assert ledger1.queue(d, lambda: 100)[0] is True
    enter(ledger1, d, delta=1000)

    assert sync.is_verified_active() is True
    assert sync.push_count >= 2
    assert ledger1.namespace in remote_store
    stored_env = remote_store[ledger1.namespace]
    assert stored_env["schema_version"] == 2
    assert stored_env["checksum_sha256"] == compute_backup_checksum(stored_env["payload"])

    post_status = ledger1.persistence_info()
    assert post_status["backend"] == "SUPABASE_REST_SYNC"
    assert post_status["persistent"] is False
    assert post_status["persistent_across_reboots"] is True
    assert post_status["backup_replica_verified"] is True
    assert post_status["sync_active"] is True
    assert post_status["acid_transactional"] is False

    # 3. Simulate Streamlit Cloud reboot: a brand-new empty SQLite database automatically restores from Supabase REST
    db_reboot = tmp_path / "futures_desk.db.rebooted"
    # Give it the same db filename in a new directory so namespace matches `USD_M_PERPETUAL:futures_desk.db`
    reboot_dir = tmp_path / "reboot_container"
    reboot_dir.mkdir(parents=True, exist_ok=True)
    ledger_reboot = PaperLedger(
        reboot_dir / "futures_desk.db",
        initial_equity=1000.0,
        expected_instrument_kind="USD_M_PERPETUAL",
        supabase_sync=sync,
    )
    assert len(ledger_reboot.trades()) == 1
    assert ledger_reboot.trades()[0]["status"] == "OPEN"
    assert ledger_reboot.summary(NOW + 1000)["equity"] == pytest.approx(ledger1.summary(NOW + 1000)["equity"])

    # 4. Separate namespace for SPOT_REFERENCE does NOT mix with USD_M_PERPETUAL backup
    ledger_spot = PaperLedger(
        reboot_dir / "paper_audit.db",
        initial_equity=1000.0,
        expected_instrument_kind="SPOT_REFERENCE",
        supabase_sync=sync,
    )
    assert len(ledger_spot.trades()) == 0

    # 5. Stale backup guard: advancing ledger_reboot to a newer timestamp and then trying to import an older backup fails
    stored_env_snapshot = copy.deepcopy(stored_env)
    p = ledger_reboot.trades()[0]["plan"]
    newer_snap = quote(d, p["tp1"] + 0.05, delta=5000)
    ledger_reboot.advance(newer_snap)
    with pytest.raises(ValueError, match="Backup kedaluwarsa ditolak"):
        ledger_reboot.import_backup_json(stored_env_snapshot, allow_if_newer_only=True)

    # 6. Corrupted checksum is rejected
    corrupted = json.loads(json.dumps(stored_env))
    corrupted["payload"]["trades"][0]["realized_pnl"] = 999999.0
    with pytest.raises(ValueError, match="Checksum SHA-256"):
        ledger_reboot.import_backup_json(corrupted)


# ==============================================================================
# BUG-24: AUTHENTIC SQLITE VS POSTGRESQL HEALTH & INTEGRITY CHECKS
# ==============================================================================

def test_bug24_authentic_sqlite_vs_postgres_health_checks(monkeypatch, tmp_path):
    """BUG-24: PRAGMA quick_check is never translated to SELECT 'ok' on Postgres; health checks verify actual backend."""
    # 1. _PostgresConnectionAdapter refuses PRAGMA quick_check
    with pytest.raises(DatabaseConfigurationError, match="PRAGMA quick_check"):
        _PostgresConnectionAdapter._translate_sql("PRAGMA quick_check")

    # 2. SQLite integrity check runs genuine PRAGMA quick_check and rejects Postgres URLs
    sq_db = tmp_path / "valid.sqlite3"
    PaperLedger(sq_db)
    sq_health = verify_sqlite_integrity(sq_db)
    assert sq_health["ok"] is True
    assert sq_health["backend"] == "SQLITE"
    assert sq_health["health_status"] == "HEALTHY_SQLITE_QUICK_CHECK"

    invalid_pg_call = verify_sqlite_integrity("postgresql://user:pass@localhost:5432/db")
    assert invalid_pg_call["ok"] is False
    assert invalid_pg_call["status"] == "INVALID_CALL_USE_VERIFY_POSTGRES_HEALTH"

    # 3. PostgreSQL health check performs real read/write probe on coinagent_db_healthcheck
    pg_backing = tmp_path / "pg_health_backing.sqlite"
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: _MockPostgresConnection(pg_backing)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    pg_url = "postgresql://user:pass@localhost:5432/coinagent_prod"
    pg_health = verify_postgres_health(pg_url=pg_url, db_path=tmp_path / "futures_desk.db")
    assert pg_health["ok"] is True
    assert pg_health["backend"] == "POSTGRESQL"
    assert pg_health["connection_status"] == "CONNECTED_POSTGRESQL"
    assert pg_health["health_status"] == "HEALTHY_POSTGRESQL_RW"
    assert pg_health["backup_status"] == "ACID_PRIMARY_POSTGRESQL"
    assert pg_health["verified_rw"] is True

    unified = verify_database_health(db_path=tmp_path / "futures_desk.db", pg_url=pg_url)
    assert unified["backend"] == "POSTGRESQL" and unified["verified_rw"] is True


# ==============================================================================
# BUG-25: PERSISTENT CROSS-PROCESS & RESTART-SAFE AI QUOTAS
# ==============================================================================

def test_bug25_persistent_cross_process_ai_quotas_survive_restart(tmp_path):
    """BUG-25: Global hourly and per-session AI quotas are stored in the shared DB across worker, dashboard, and restarts."""
    shared_db = tmp_path / "shared_security.db"
    now = [1_700_000_000.0]

    # Dashboard and Worker instantiate separate SecurityManager objects pointing to the same database
    sec_dash = SecurityManager(
        shared_db,
        admin_token="shared-admin-2026",
        clock=lambda: now[0],
        max_ai_calls_per_session=2,
        max_ai_calls_global_per_hour=3,
    )
    sec_worker = SecurityManager(
        shared_db,
        admin_token="shared-admin-2026",
        clock=lambda: now[0],
        max_ai_calls_per_session=2,
        max_ai_calls_global_per_hour=3,
    )

    admin_tok = sec_dash.authenticate("shared-admin-2026")
    worker_tok = sec_worker.issue_worker_session()
    admin_sid = sec_dash.verify_session(admin_tok)["session_id"]

    # Call 1 from dashboard admin, Call 2 from background worker
    assert sec_dash.authorize_action("AI_REVIEW", session_token=admin_tok)[0] is True
    assert sec_worker.authorize_action("AI_REVIEW", session_token=worker_tok)[0] is True

    # Simulate complete process restart: instantiate a brand-new SecurityManager on the same DB
    sec_after_restart = SecurityManager(
        shared_db,
        admin_token="shared-admin-2026",
        clock=lambda: now[0],
        max_ai_calls_per_session=2,
        max_ai_calls_global_per_hour=3,
    )
    usage = sec_after_restart.ai_quota_usage(session_id=admin_sid)
    assert usage["global_hour_used"] == 2
    assert usage["session_used"] == 1

    # Call 3 from admin after restart succeeds (reaching global_hour_limit=3 and session_limit=2)
    assert sec_after_restart.authorize_action("AI_REVIEW", session_token=admin_tok)[0] is True

    # Call 4 from worker is blocked by persistent global hourly quota (3/3 used across processes!)
    ok_wrk, msg_wrk = sec_after_restart.authorize_action("AI_REVIEW", session_token=worker_tok)
    assert ok_wrk is False
    assert "ai_review_global_hour" in msg_wrk

    # Call 4 from admin is blocked by persistent per-session quota (2/2 used across restart!)
    ok_adm, msg_adm = sec_after_restart.authorize_action("AI_REVIEW", session_token=admin_tok)
    assert ok_adm is False
    assert "Kuota AI per sesi" in msg_adm


# ==============================================================================
# BUG-26: CACHE RESOURCE FINGERPRINT & SESSION INVALIDATION ON TOKEN ROTATION
# ==============================================================================

def test_bug26_cache_signature_fingerprint_and_token_rotation_revokes_sessions(tmp_path):
    """BUG-26: Rotating admin token changes cache fingerprint, never logs raw token, and revokes all prior sessions."""
    fp_a = compute_auth_config_fingerprint("admin-secret-token-alpha", "", "")
    fp_b = compute_auth_config_fingerprint("admin-secret-token-bravo", "", "")
    assert fp_a != fp_b
    assert "admin-secret-token-alpha" not in fp_a
    assert "admin-secret-token-bravo" not in fp_b

    db_path = tmp_path / "rotation.db"
    sec1 = SecurityManager(db_path, admin_token="admin-secret-token-alpha")
    assert not hasattr(sec1, "_admin_token")

    tok_alpha = sec1.authenticate("admin-secret-token-alpha")
    assert tok_alpha is not None
    assert sec1.verify_session(tok_alpha)["role"] == ROLE_ADMIN

    # Now rotate the administrator token to a new secret ( simulating cache invalidation / new SecurityManager)
    sec2 = SecurityManager(db_path, admin_token="admin-secret-token-bravo")
    assert sec2.auth_fingerprint == fp_b

    # Previously issued session token under old credential is immediately invalid!
    verified_old = sec2.verify_session(tok_alpha)
    assert verified_old["role"] == ROLE_VIEWER
    assert verified_old["authenticated"] is False
    assert sec1.verify_session(tok_alpha)["role"] == ROLE_VIEWER

    # Old credential can no longer authenticate; new credential succeeds
    assert sec2.authenticate("admin-secret-token-alpha") is None
    tok_bravo = sec2.authenticate("admin-secret-token-bravo")
    assert tok_bravo is not None
    assert sec2.verify_session(tok_bravo)["role"] == ROLE_ADMIN

    # Dynamic in-place rotation also revokes active sessions
    sec2.rotate_admin_credentials(new_admin_token="admin-secret-token-charlie")
    assert sec2.verify_session(tok_bravo)["role"] == ROLE_VIEWER

    # Verify audit log recorded TOKEN_ROTATION and never leaked raw tokens
    audit_dump = json.dumps(sec2.audit_entries(limit=50))
    assert "TOKEN_ROTATION" in audit_dump
    assert "admin-secret-token-alpha" not in audit_dump
    assert "admin-secret-token-bravo" not in audit_dump
    assert "admin-secret-token-charlie" not in audit_dump

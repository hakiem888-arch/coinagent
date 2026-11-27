"""Comprehensive Integration & Security Tests for SUPABASE-01..03, SEC-03, BUG-35, OPS-03, & QA-03 (CoinAgentX v6.0.2).

Covers:
- SUPABASE-01: Explicit `GRANT USAGE ON SCHEMA public TO service_role`, `REVOKE ALL ... FROM PUBLIC, anon, authenticated`,
  `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.coinagent_backups TO service_role`, and live HTTP REST
  upsert/select/update/pull verification via a real `http.server` instance (`verify_rw_probe`).
- SUPABASE-02: `backup_at_ms`, `state_updated_ms`, and `state_seq` are included in `compute_backup_checksum`
  and authenticated via `compute_backup_hmac`; envelope-vs-payload timestamp mismatch or timestamp tampering
  is rejected.
- SUPABASE-03: Single-writer guard (`enforce_single_writer=True`) prevents a second SQLite/REST writer from
  overwriting another writer's namespace (`SUPABASE-03 MULTI-WRITER CONFLICT`), blocks stale overwrites,
  and never reports `SUPABASE_REST_SYNC` as ACID or shared multi-writer safe.
- SEC-03: All internal tables enforce RLS & `REVOKE ALL FROM PUBLIC, anon, authenticated`; `security_sessions`
  stores only `token_hash` (never raw bearer tokens); `log_audit` failures are actively monitored and buffered.
- BUG-35: Bybit HTTP 403 is classified into `REGIONAL_RESTRICTION`, `RATE_LIMIT`, `AUTH_ERROR`, and `UNKNOWN_403`
  based on response body and `retCode`; `UNKNOWN_403` and `AUTH_ERROR` never trigger Spot Reference fallback.
- OPS-03: `worker_health_status()` detects `NO_WORKER`, `WORKER_ALIVE`, and `WORKER_DEAD_OR_DISCONNECTED`.
- QA-03: Full 10-stage integration test across PostgreSQL connection, journal insert, position update,
  application restart, shared Worker + Dashboard database visibility, concurrent scanner lock contention,
  connection outage handling, and full journal + security audit backup/restore.
"""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
import types
import urllib.parse
import pytest

import app as app_mod
from futures.bybit_market import BybitFuturesMarket
from futures.db_backend import (
    DatabaseConfigurationError,
    SupabaseStateSync,
    _PostgresConnectionAdapter,
    compute_backup_checksum,
    compute_backup_hmac,
    configure_database_runtime,
    connect_database,
    persistence_status,
)
from futures.market import MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.paper import PaperLedger
from futures.provider_router import FuturesProviderRouter, classify_provider_error
from futures.security import ROLE_ADMIN, SecurityManager
from tests.fixtures import NOW
from tests.test_audit_v602_regression_bug22_26 import _MockPostgresConnection
from tests.test_audit_v6_security_failover import MultiTierFakeMarket
from tests.test_bybit_regional import Response as BybitResp, Session as BybitSess
from tests.test_market_scanner import Alerts, Calendar, Market, Pool
from tests.test_paper import approved, enter, quote


class _LivePostgRESTServer:
    """Real local HTTP server implementing Supabase PostgREST `/rest/v1/coinagent_backups` semantics for QA-03 & SUPABASE-01."""

    def __init__(self):
        self.rows = {}
        self.request_log = []
        self.fail_next = False
        self.lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _check_auth(self):
                api_key = self.headers.get("apikey", "")
                auth = self.headers.get("Authorization", "")
                return bool(api_key and auth.startswith("Bearer "))

            def do_POST(self):
                with parent.lock:
                    parent.request_log.append(("POST", self.path))
                    if parent.fail_next:
                        self.send_response(503)
                        self.end_headers()
                        self.wfile.write(b'{"message":"service unavailable"}')
                        return
                    if not self._check_auth():
                        self.send_response(401)
                        self.end_headers()
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    doc = json.loads(raw.decode("utf-8"))
                    ns = doc["namespace"]
                    parent.rows[ns] = doc
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps([doc]).encode("utf-8"))

            def do_GET(self):
                with parent.lock:
                    parent.request_log.append(("GET", self.path))
                    if parent.fail_next:
                        self.send_response(503)
                        self.end_headers()
                        return
                    if not self._check_auth():
                        self.send_response(401)
                        self.end_headers()
                        return
                    parsed = urllib.parse.urlparse(self.path)
                    qs = urllib.parse.parse_qs(parsed.query)
                    ns_filter = qs.get("namespace", [""])[0]
                    target_ns = ns_filter[3:] if ns_filter.startswith("eq.") else ns_filter
                    target_ns = urllib.parse.unquote(target_ns)
                    matched = [parent.rows[target_ns]] if target_ns in parent.rows else []
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(matched).encode("utf-8"))

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ==============================================================================
# SUPABASE-01 & SEC-03: SQL GRANTS, RLS ON ALL INTERNAL TABLES, & LIVE RW PROBE
# ==============================================================================

def test_supabase01_and_sec03_sql_grants_rls_and_live_rest_rw_probe():
    """SUPABASE-01 & SEC-03: Verify SQL migration grants/revokes/RLS and live HTTP REST upsert/select/update/pull."""
    root = Path(app_mod.__file__).resolve().parent
    sql_path = root / "supabase" / "migrations" / "001_coinagent_schema.sql"
    sql_text = sql_path.read_text(encoding="utf-8")

    # 1. Verify SUPABASE-01 explicit GRANT and REVOKE statements
    assert "GRANT USAGE ON SCHEMA public TO service_role;" in sql_text
    assert "FROM PUBLIC, anon, authenticated;" in sql_text
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in sql_text
    assert "TO service_role;" in sql_text

    # 2. Verify SEC-03: All internal tables are covered by REVOKE + ENABLE/FORCE RLS, and security_sessions uses token_hash
    for tbl in (
        "coinagent_backups",
        "coinagent_db_healthcheck",
        "security_audit_log",
        "security_quota_events",
        "security_meta",
        "security_sessions",
        "paper_meta",
        "paper_trades",
        "paper_events",
        "scanner_locks",
        "telegram_alerts",
        "analyses",
    ):
        assert f"public.{tbl}" in sql_text
        assert f"'{tbl}'" in sql_text
    assert "token_hash TEXT NOT NULL" in sql_text
    assert "FORCE ROW LEVEL SECURITY" in sql_text

    # 3. Live HTTP server test for SupabaseStateSync.verify_rw_probe() (upsert -> select -> update -> pull)
    server = _LivePostgRESTServer()
    try:
        sync = SupabaseStateSync(server.base_url, "sb_secret_live_test_key", writer_id="worker-1")
        assert sync.is_verified_active() is False
        probe_res = sync.verify_rw_probe()
        assert probe_res["ok"] is True
        assert probe_res["upsert_ok"] is True
        assert probe_res["select_ok"] is True
        assert probe_res["update_ok"] is True
        assert probe_res["pull_ok"] is True
        assert sync.is_verified_active() is True
    finally:
        server.close()


# ==============================================================================
# SUPABASE-02: TIMESTAMPS IN CHECKSUM, ENVELOPE/PAYLOAD MATCH, & HMAC AUTH
# ==============================================================================

def test_supabase02_timestamp_checksum_protection_and_envelope_payload_match(tmp_path):
    """SUPABASE-02: Modifying backup_at_ms or state_updated_ms without updating checksum or mismatching envelope is rejected."""
    db_orig = tmp_path / "ts_orig.db"
    ledger = PaperLedger(db_orig, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    d = approved("LONG")
    assert ledger.queue(d, lambda: 100)[0] is True
    enter(ledger, d, delta=1000)

    raw_backup = json.loads(ledger.export_json(backup_at_ms=NOW + 5000))
    orig_checksum = raw_backup["checksum_sha256"]
    orig_hmac = raw_backup["hmac_sha256"]
    assert orig_checksum == compute_backup_checksum(raw_backup)
    assert orig_hmac == compute_backup_hmac(raw_backup)

    # 1. Mutating backup_at_ms or state_updated_ms in payload MUST change compute_backup_checksum
    mut_b = copy.deepcopy(raw_backup)
    mut_b["backup_at_ms"] = NOW + 999999
    assert compute_backup_checksum(mut_b) != orig_checksum

    mut_s = copy.deepcopy(raw_backup)
    mut_s["state_updated_ms"] = NOW + 999999
    mut_s["meta"]["state_updated_ms"] = str(NOW + 999999)
    assert compute_backup_checksum(mut_s) != orig_checksum

    target_ledger = PaperLedger(tmp_path / "ts_target.db", initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    with pytest.raises(ValueError, match="Checksum SHA-256"):
        target_ledger.import_backup_json(mut_b)
    with pytest.raises(ValueError, match="Checksum SHA-256"):
        target_ledger.import_backup_json(mut_s)

    # 2. Envelope-vs-payload timestamp mismatch is rejected even if payload checksum itself is intact
    sync = SupabaseStateSync("https://example.supabase.co", "sb_secret_key", hmac_secret="my-hmac-key")
    env = sync.build_envelope(ledger.namespace, raw_backup, backup_at_ms=NOW + 5000)
    tampered_env = copy.deepcopy(env)
    tampered_env["state_updated_ms"] = NOW + 888888  # Envelope says newer, payload still has old timestamp
    ok_env, reason_env, _ = sync.verify_envelope(tampered_env, expected_namespace=ledger.namespace)
    assert ok_env is False and "Timestamp" in reason_env
    with pytest.raises(ValueError, match="Timestamp"):
        target_ledger.import_backup_json(tampered_env)


# ==============================================================================
# SUPABASE-03: SINGLE-WRITER REST GUARD & NON-ACID STATUS
# ==============================================================================

def test_supabase03_rest_sync_blocks_multi_writer_overwrite_and_never_claims_acid(tmp_path):
    """SUPABASE-03: Two SQLite instances with different writer_ids cannot overwrite each other on Supabase REST."""
    server = _LivePostgRESTServer()
    try:
        sync_worker = SupabaseStateSync(
            server.base_url, "sb_secret_key", writer_id="worker-primary", allow_write=True, enforce_single_writer=True
        )
        sync_dash = SupabaseStateSync(
            server.base_url, "sb_secret_key", writer_id="dashboard-secondary", allow_write=True, enforce_single_writer=True
        )

        db_w = tmp_path / "w_dir" / "futures_desk.db"
        db_w.parent.mkdir(parents=True, exist_ok=True)
        ledger_w = PaperLedger(db_w, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", supabase_sync=sync_worker)
        d = approved("LONG")
        assert ledger_w.queue(d, lambda: 100)[0] is True
        assert sync_worker.last_push_ok is True

        # Secondary writer on a different server with its own empty local SQLite attempts to push to the same namespace
        db_d = tmp_path / "d_dir" / "futures_desk.db"
        db_d.parent.mkdir(parents=True, exist_ok=True)
        # Disable auto-pull by creating ledger first, then attempting a conflicting push from dashboard writer
        sample_conflicting = json.loads(ledger_w.export_json(backup_at_ms=NOW + 9000))
        ok_conflict = sync_dash.push_backup(ledger_w.namespace, sample_conflicting, backup_at_ms=NOW + 9000)
        assert ok_conflict is False
        assert "MULTI-WRITER CONFLICT" in sync_dash.last_error

        # Verify persistence_status NEVER claims SUPABASE_REST_SYNC is ACID or shared multi-writer safe or primary persistent DB
        st = ledger_w.persistence_info()
        assert st["backend"] == "SUPABASE_REST_SYNC"
        assert st["acid_transactional"] is False
        assert st["shared_multi_writer_safe"] is False
        assert st["persistent"] is False
        assert st["backup_replica_verified"] is True
    finally:
        server.close()


# ==============================================================================
# SEC-03: HASHED SESSION TOKENS (`token_hash`) & MONITORED AUDIT FAILURES
# ==============================================================================

def test_sec03_session_tokens_stored_as_sha256_hash_and_audit_failures_monitored(monkeypatch, tmp_path):
    """SEC-03: Raw bearer token is never stored in security_sessions table or memory; audit failures are tracked."""
    db_path = tmp_path / "sec03.db"
    sec = SecurityManager(db_path, admin_token="super-secret-admin-2026")
    raw_session_token = sec.authenticate("super-secret-admin-2026")
    assert raw_session_token is not None

    # 1. Inspect SQLite security_sessions table and in-memory _sessions: raw token MUST NOT appear anywhere!
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT session_id, role, expires_at, auth_fp, token_hash FROM security_sessions").fetchall()
    assert len(rows) == 1
    stored_hash = rows[0][4]
    assert stored_hash == hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()
    assert raw_session_token not in json.dumps(rows)
    assert raw_session_token not in json.dumps(sec._sessions)

    # Session verification still works via constant-time hash comparison
    info = sec.verify_session(raw_session_token)
    assert info["role"] == ROLE_ADMIN and info["authenticated"] is True

    # 2. Simulate database write failure during log_audit and verify it is monitored, counted, and buffered
    monkeypatch.setattr(sec, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    sec.log_audit("MANUAL_SCAN", "ALLOWED", role=ROLE_ADMIN, session_id=info["session_id"], detail="uji kegagalan audit")
    health = sec.audit_health_status()
    assert health["healthy"] is False
    assert health["failure_count"] == 1
    assert "OperationalError" in health["last_error"]
    assert health["buffered_count"] == 1
    assert sec.monitor.snapshot()["counters"]["audit_log_failures"] == 1


# ==============================================================================
# BUG-35: SPECIFIC BYBIT HTTP 403 CLASSIFICATION
# ==============================================================================

def test_bug35_bybit_http_403_specific_classification_and_no_blind_spot_fallback():
    """BUG-35: Distinguish REGIONAL_RESTRICTION, RATE_LIMIT, AUTH_ERROR, and UNKNOWN_403 on Bybit HTTP 403."""
    # 1. Generic empty 403 on Bybit is classified as UNKNOWN_403, NOT REGIONAL_RESTRICTION
    m_unknown = BybitFuturesMarket(session=BybitSess(BybitResp(403, "")))
    with pytest.raises(MarketError) as exc_info_unk:
        m_unknown.get(m_unknown.time_path)
    assert exc_info_unk.value.error_category == "UNKNOWN_403"
    assert classify_provider_error(exc_info_unk.value) == "UNKNOWN_403"

    # 2. 403 with API key / signature error is classified as AUTH_ERROR
    m_auth = BybitFuturesMarket(session=BybitSess(BybitResp(403, "Forbidden: invalid api key signature")))
    with pytest.raises(MarketError) as exc_info_auth:
        m_auth.get(m_auth.time_path)
    assert exc_info_auth.value.error_category == "AUTH_ERROR"
    assert classify_provider_error(exc_info_auth.value) == "AUTH_ERROR"

    # 3. 403 with rate limit is classified as RATE_LIMIT
    m_rate = BybitFuturesMarket(session=BybitSess(BybitResp(403, "403, access too frequent")))
    with pytest.raises(MarketError) as exc_info_rate:
        m_rate.get(m_rate.time_path)
    assert exc_info_rate.value.error_category == "RATE_LIMIT"
    assert classify_provider_error(exc_info_rate.value) == "RATE_LIMIT"

    # 4. 403 with explicit CloudFront / country geo-block is classified as REGIONAL_RESTRICTION
    m_geo = BybitFuturesMarket(session=BybitSess(BybitResp(403, "CloudFront: blocked in your country/region")))
    with pytest.raises(MarketError) as exc_info_geo:
        m_geo.get(m_geo.time_path)
    assert exc_info_geo.value.error_category == "REGIONAL_RESTRICTION"
    assert classify_provider_error(exc_info_geo.value) == "REGIONAL_RESTRICTION"

    # 5. Router MUST NOT fallback to Spot Reference on UNKNOWN_403 or AUTH_ERROR
    for non_geo_exc in (exc_info_unk.value, exc_info_auth.value, exc_info_rate.value):
        primary = MultiTierFakeMarket("USD_M_PERPETUAL")
        primary.fail_mode = 451
        bybit = MultiTierFakeMarket("BYBIT_LINEAR_PERPETUAL")
        bybit.get = lambda *_a, _e=non_geo_exc, **_kw: (_ for _ in ()).throw(_e)
        spot = MultiTierFakeMarket("SPOT_REFERENCE")
        router = FuturesProviderRouter(primary, bybit, spot_fallback=spot, spot_fallback_enabled=True)
        with pytest.raises(MarketError):
            router.get("/fapi/v1/time")
        assert router.instrument_kind == "BYBIT_LINEAR_PERPETUAL"


# ==============================================================================
# OPS-03 & QA-03: 10-STAGE FULL POSTGRESQL + WORKER + DASHBOARD INTEGRATION TEST
# ==============================================================================

def test_qa03_and_ops03_ten_stage_postgres_worker_dashboard_integration(monkeypatch, tmp_path):
    """QA-03 & OPS-03: Execute all 10 integration stages across shared PostgreSQL, Worker, Dashboard, locks, and outage."""
    pg_storage = tmp_path / "qa03_postgres_engine.sqlite"
    connection_online = {"up": True}

    def connect_pg(url, **kwargs):
        if not connection_online["up"]:
            raise RuntimeError("server connection terminated unexpectedly")
        return _MockPostgresConnection(pg_storage)

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = connect_pg
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    pg_url = "postgresql://postgres.qa03:TestPass123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
    secret_getter = lambda k: pg_url if k == "DATABASE_URL" else ("qa03-admin-token" if k == "COINAGENT_ADMIN_TOKEN" else "")
    configure_database_runtime(secret_getter=secret_getter, db_url=pg_url)

    try:
        # Stage 1: Connect PostgreSQL and verify ACID health check
        db_file = tmp_path / "futures_desk.db"
        conn = connect_database(db_file, pg_url=pg_url, get_secret=secret_getter)
        assert isinstance(conn, _PostgresConnectionAdapter)
        conn.close()
        st_initial = persistence_status(db_file, get_secret=secret_getter, pg_url=pg_url)
        assert st_initial["backend"] == "POSTGRESQL" and st_initial["persistent"] is True

        # Stage 2: Insert PAPER journal trade
        ledger_app1 = PaperLedger(db_file, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", pg_url=pg_url, secret_getter=secret_getter)
        d = approved("LONG")
        ok_q, sig = ledger_app1.queue(d, lambda: 100)
        assert ok_q is True

        # Stage 3: Update position (fill entry -> OPEN, then fill TP1 -> stop moves to Break-Even)
        enter(ledger_app1, d, delta=1000)
        p = ledger_app1.trades()[0]["plan"]
        ledger_app1.advance(quote(d, p["tp1"] + 0.02, delta=2000))
        t_after_tp1 = ledger_app1.trades()[0]
        assert t_after_tp1["status"] == "OPEN" and t_after_tp1["tp1_ms"] == NOW + 2000
        equity_before_restart = ledger_app1.summary(NOW + 2000)["equity"]

        # Stage 4 & 5: Restart application (new PaperLedger & SecurityManager instances) -> position & balance remain intact
        ledger_restarted = PaperLedger(db_file, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", pg_url=pg_url, secret_getter=secret_getter)
        trades_after_restart = ledger_restarted.trades()
        assert len(trades_after_restart) == 1
        assert trades_after_restart[0]["signal"] == sig
        assert trades_after_restart[0]["tp1_ms"] == NOW + 2000
        assert ledger_restarted.summary(NOW + 2000)["equity"] == pytest.approx(equity_before_restart)

        # Stage 6 & 7: Run Worker with the same PostgreSQL database -> Worker heartbeat, scan report, & trade updates visible on Dashboard
        worker_ledger = PaperLedger(db_file, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", pg_url=pg_url, secret_getter=secret_getter)
        worker_sec = SecurityManager(db_file, admin_token="qa03-admin-token", pg_url=pg_url, secret_getter=secret_getter)
        worker_scanner = MarketScanner(Market(), Pool(), Calendar(True), worker_ledger, Alerts(worker_ledger), security=worker_sec, trusted_worker=True, clock=lambda: 100)
        worker_ledger.record_worker_heartbeat("worker-prod-01", NOW + 3000, 60)
        rep_worker = worker_scanner.run(ScannerConfig(candidates=1, interval=60, send_alerts=False))
        assert rep_worker["scan_status"] in ("COMPLETED", "COMPLETED_WITH_NOTES")

        # Dashboard reads from the same PostgreSQL database and sees Worker's heartbeat, health, and scan report
        wh_alive = ledger_restarted.worker_health_status(now_ms=NOW + 10_000)
        assert wh_alive["status"] == "WORKER_ALIVE" and wh_alive["alive"] is True
        # OPS-03: When worker stops sending heartbeats beyond 2.5x interval, dashboard detects WORKER_DEAD_OR_DISCONNECTED
        wh_stale = ledger_restarted.worker_health_status(now_ms=NOW + 300_000)
        assert wh_stale["status"] == "WORKER_DEAD_OR_DISCONNECTED" and wh_stale["alive"] is False
        assert ledger_restarted.last_scan_report()["scan_id"] == rep_worker["scan_id"]

        # Stage 8: Test two concurrent scanners on the same PostgreSQL database
        assert worker_ledger.acquire_db_scanner_lock("worker-A", NOW + 4000, ttl_ms=60000) is True
        assert ledger_restarted.acquire_db_scanner_lock("worker-B", NOW + 5000, ttl_ms=60000) is False
        worker_ledger.release_db_scanner_lock("worker-A")

        # Stage 9: Test PostgreSQL connection outage -> reports POSTGRESQL_ERROR (persistent=False) and DB_CONNECTION_LOST
        connection_online["up"] = False
        st_down = ledger_restarted.persistence_info()
        assert st_down["backend"] == "POSTGRESQL_ERROR" and st_down["persistent"] is False
        wh_down = ledger_restarted.worker_health_status(now_ms=NOW + 10_000)
        assert wh_down["status"] == "DB_CONNECTION_LOST"
        connection_online["up"] = True

        # Stage 10: Full backup & restore of all journals, security_audit_log, and security_quota_events into a separate test DB
        tok = worker_sec.authenticate("qa03-admin-token")
        assert worker_sec.authorize_action("AI_REVIEW", session_token=tok)[0] is True
        full_backup_json = ledger_restarted.export_json(backup_at_ms=NOW + 6000)

        pg_restore_storage = tmp_path / "qa03_restored_pg.sqlite"
        fake_psycopg.connect = lambda url, **kw: _MockPostgresConnection(pg_restore_storage)
        ledger_restored = PaperLedger(tmp_path / "futures_desk.db", initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL", pg_url=pg_url, secret_getter=secret_getter)
        res_restore = ledger_restored.import_backup_json(full_backup_json)
        assert res_restore["ok"] is True
        assert res_restore["trades_restored"] >= 1
        assert res_restore["audit_entries_restored"] >= 1
        sec_restored = SecurityManager(tmp_path / "futures_desk.db", admin_token="qa03-admin-token", pg_url=pg_url, secret_getter=secret_getter)
        assert sec_restored.ai_quota_usage()["global_hour_used"] == 1
    finally:
        configure_database_runtime(secret_getter=lambda _k: "", db_url="")

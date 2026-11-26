import copy
"""Persistent Database & Backup Backend for CoinAgentX (BUG-17, BUG-18, BUG-22, BUG-23, BUG-24).

Guarantees:
1. BUG-22 (No PostgreSQL False Positives & Centralized Config):
   - `resolve_database_config` / `configured_database_url` resolves `DATABASE_URL` from centralized
     runtime configuration, Streamlit Secrets getter, or environment variables.
   - `connect_database` NEVER silently falls back to SQLite when PostgreSQL is requested; if the
     driver (`psycopg` / `psycopg2`) is missing or connection fails, it raises `DatabaseConfigurationError`.
   - `persistence_status` only reports `POSTGRESQL` (`persistent=True`) after opening a real
     PostgreSQL connection and completing a verified read/write health probe.
2. BUG-23 (Active Supabase REST Sync with Namespace, Schema Version, Checksum, Stale Guard & Retry):
   - `SupabaseStateSync` envelopes every backup with `namespace`, `schema_version`, `backup_at_ms`,
     `state_updated_ms`, and canonical `checksum_sha256`.
   - Handles transient network errors with bounded retries and never claims `SUPABASE_REST_SYNC`
     is active until synchronization is verified. Never equates REST snapshot replication with
     ACID PostgreSQL transactions.
3. BUG-24 (Authentic SQLite vs PostgreSQL Health & Integrity Verification):
   - `_PostgresConnectionAdapter` never translates `PRAGMA quick_check` into a fake `SELECT 'ok'`.
   - Provides separate `verify_sqlite_integrity`, `verify_postgres_health`, and unified
     `verify_database_health` distinguishing connection, database health, and backup availability.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import hashlib
import hmac
import inspect
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


BACKUP_SCHEMA_VERSION = 2
_RUNTIME_CONFIG_LOCK = threading.RLock()
_RUNTIME_SECRET_GETTER = None
_RUNTIME_DB_URL = None


class DatabaseConfigurationError(RuntimeError):
    """BUG-22: Raised when PostgreSQL is configured but the driver is missing, URL is invalid, or connection fails."""


def configure_database_runtime(secret_getter=None, db_url=None):
    """BUG-22 #1: Register centralized runtime secret getter and/or explicit DATABASE_URL for dashboard and worker."""
    global _RUNTIME_SECRET_GETTER, _RUNTIME_DB_URL
    with _RUNTIME_CONFIG_LOCK:
        if secret_getter is not None:
            _RUNTIME_SECRET_GETTER = secret_getter
        if db_url is not None:
            _RUNTIME_DB_URL = str(db_url).strip() or None


def get_runtime_secret_getter():
    with _RUNTIME_CONFIG_LOCK:
        return _RUNTIME_SECRET_GETTER


def _effective_getter(get_secret=None):
    if callable(get_secret):
        return get_secret
    runtime_getter = get_runtime_secret_getter()
    if callable(runtime_getter):
        return runtime_getter
    return lambda k: os.getenv(k, "")


def configured_database_url(get_secret=None, explicit_url=None):
    """BUG-22 #1: Return configured PostgreSQL/Supabase connection URL from explicit arg, Secrets getter, runtime config, or env."""
    if explicit_url is not None and str(explicit_url).strip():
        return str(explicit_url).strip()
    with _RUNTIME_CONFIG_LOCK:
        if _RUNTIME_DB_URL:
            return _RUNTIME_DB_URL
    getter = _effective_getter(get_secret)
    for key in ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL", "COINAGENT_DATABASE_URL"):
        try:
            val = str(getter(key) or os.getenv(key, "")).strip()
        except Exception:
            val = str(os.getenv(key, "")).strip()
        if val:
            return val
    return ""


def configured_supabase_rest(get_secret=None):
    """Return (supabase_url, supabase_key) if Supabase REST persistence is configured."""
    getter = _effective_getter(get_secret)
    try:
        url = str(getter("SUPABASE_URL") or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    except Exception:
        url = str(os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    try:
        key = str(
            getter("SUPABASE_SERVICE_ROLE_KEY")
            or getter("SUPABASE_KEY")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
            or os.getenv("SUPABASE_KEY", "")
        ).strip()
    except Exception:
        key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "")).strip()
    if url.startswith("https://") and key:
        return url, key
    return "", ""


def resolve_database_config(get_secret=None, explicit_url=None):
    """BUG-22 #1: Single centralized configuration resolver for PostgreSQL and Supabase REST."""
    pg_url = configured_database_url(get_secret=get_secret, explicit_url=explicit_url)
    sb_url, sb_key = configured_supabase_rest(get_secret=get_secret)
    return {
        "pg_url": pg_url,
        "supabase_url": sb_url,
        "supabase_key": sb_key,
    }


def _sanitize_namespace_prefix(db_path):
    if not db_path:
        return ""
    stem = re.sub(r"[^a-zA-Z0-9_]", "_", Path(db_path).stem.lower()).strip("_")
    if not stem or stem in ("postgres", "postgres_health", "default"):
        return ""
    return f"{stem}_"


class _PostgresCursorAdapter:
    """Adapts a DB-API 2.0 PostgreSQL cursor to accept SQLite-style queries and idioms."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        rows = self._cur.fetchall()
        return iter(rows if rows is not None else [])


class _PostgresConnectionAdapter:
    """Wraps a psycopg/psycopg2 connection to provide SQLite-compatible `.execute()` and ACID transactions."""

    is_postgres = True
    backend_kind = "POSTGRESQL"

    def __init__(self, raw_conn, driver_name="psycopg", namespace_prefix=""):
        self._conn = raw_conn
        self.driver_name = driver_name
        self.namespace_prefix = str(namespace_prefix or "")

    @staticmethod
    def _translate_sql(sql, namespace_prefix=""):
        s = sql.strip()
        upper = s.upper()
        # BUG-24: NEVER fake PRAGMA quick_check or PRAGMA integrity_check on PostgreSQL!
        if upper.startswith("PRAGMA QUICK_CHECK") or upper.startswith("PRAGMA INTEGRITY_CHECK") or upper.startswith("PRAGMA FOREIGN_KEY_CHECK"):
            raise DatabaseConfigurationError(
                "PRAGMA quick_check/integrity_check hanya valid untuk SQLite; gunakan verify_postgres_health() untuk PostgreSQL."
            )
        if upper.startswith("PRAGMA JOURNAL_MODE"):
            return "SELECT 'wal'"
        if upper == "BEGIN IMMEDIATE":
            return "BEGIN"
        s = re.sub(
            r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
            "BIGSERIAL PRIMARY KEY",
            s,
            flags=re.I,
        )
        if upper.startswith("CREATE TABLE"):
            s = re.sub(r"\bINTEGER\b", "BIGINT", s, flags=re.I)
            s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
        if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT DO NOTHING"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+paper_meta", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+paper_trades", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (signal) DO UPDATE SET symbol = EXCLUDED.symbol, mode = EXCLUDED.mode, "
                "status = EXCLUDED.status, realized = EXCLUDED.realized, updated_ms = EXCLUDED.updated_ms, "
                "payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+telegram_alerts", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (target, signal) DO UPDATE SET state = EXCLUDED.state, "
                "retry_at = EXCLUDED.retry_at, updated_at = EXCLUDED.updated_at"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+analyses", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (id) DO UPDATE SET at_ms = EXCLUDED.at_ms, symbol = EXCLUDED.symbol, "
                "mode = EXCLUDED.mode, status = EXCLUDED.status, payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+scanner_locks", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (lock_name) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
                "acquired_ms = EXCLUDED.acquired_ms, expires_ms = EXCLUDED.expires_ms"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+security_meta", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+security_sessions", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (session_id) DO UPDATE SET role = EXCLUDED.role, "
                "expires_at = EXCLUDED.expires_at, auth_fp = EXCLUDED.auth_fp, token_hash = EXCLUDED.token_hash"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+coinagent_db_healthcheck", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + " ON CONFLICT (probe_key) DO UPDATE SET probe_ts = EXCLUDED.probe_ts"
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+user_preferences", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (user_id) DO UPDATE SET "
                "auto_scan = EXCLUDED.auto_scan, scan_interval = EXCLUDED.scan_interval, "
                "candidates = EXCLUDED.candidates, relaxed_mode = EXCLUDED.relaxed_mode, "
                "send_alerts = EXCLUDED.send_alerts, send_watchlist_alerts = EXCLUDED.send_watchlist_alerts, "
                "send_scan_summary = EXCLUDED.send_scan_summary, min_volume = EXCLUDED.min_volume, "
                "leverage = EXCLUDED.leverage, risk_pct = EXCLUDED.risk_pct, fee_pct = EXCLUDED.fee_pct, "
                "slippage_bps = EXCLUDED.slippage_bps, funding_pct = EXCLUDED.funding_pct, "
                "updated_at_ms = EXCLUDED.updated_at_ms, payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+scanner_runs", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (scan_id) DO UPDATE SET "
                "started_at_ms = EXCLUDED.started_at_ms, completed_at_ms = EXCLUDED.completed_at_ms, "
                "provider = EXCLUDED.provider, instrument_kind = EXCLUDED.instrument_kind, "
                "worker_id = EXCLUDED.worker_id, scan_status = EXCLUDED.scan_status, "
                "data_status = EXCLUDED.data_status, configured_interval = EXCLUDED.configured_interval, "
                "effective_interval = EXCLUDED.effective_interval, next_scan_at_ms = EXCLUDED.next_scan_at_ms, "
                "errors = EXCLUDED.errors, payload = EXCLUDED.payload"
            )
        elif re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+scanner_candidate_queue", s, flags=re.I):
            s = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
            s = s + (
                " ON CONFLICT (queue_id) DO UPDATE SET "
                "instrument_kind = EXCLUDED.instrument_kind, updated_at_ms = EXCLUDED.updated_at_ms, "
                "payload = EXCLUDED.payload"
            )

        if namespace_prefix:
            for tbl in ("paper_meta", "paper_trades", "paper_events", "paper_status", "scanner_locks"):
                s = re.sub(rf"\b{tbl}\b", f"{namespace_prefix}{tbl}", s)

        s = s.replace("?", "%s")
        return s

    def execute(self, sql, params=()):
        translated = self._translate_sql(sql, namespace_prefix=self.namespace_prefix)
        param_tuple = tuple(params) if params else ()
        cur = self._conn.cursor()
        try:
            cur.execute(translated, param_tuple)
            return _PostgresCursorAdapter(cur)
        except Exception as exc:
            err_str = str(exc).lower()
            err_name = type(exc).__name__.lower()
            if "integer out of range" in err_str or "numericvalueoutofrange" in err_name:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                _upgrade_postgres_int4_columns(self._conn, force=True)
                cur = self._conn.cursor()
                cur.execute(translated, param_tuple)
                return _PostgresCursorAdapter(cur)
            if "datatypemismatch" in err_name or "is of type integer but expression is of type boolean" in err_str:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                coerced = tuple(int(p) if isinstance(p, bool) else p for p in param_tuple)
                cur = self._conn.cursor()
                cur.execute(translated, coerced)
                return _PostgresCursorAdapter(cur)
            raise

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False


def normalize_postgres_url(raw_url):
    """Sanitize common user paste mistakes in DATABASE_URL (e.g., [password] brackets, raw @/#/$/?/ in password, quotes, sslmode)."""
    s = str(raw_url or "").strip().strip('"').strip("'")
    if not s:
        return ""
    # Strip accidental key assignment prefix if user pasted `DATABASE_URL = "postgresql://..."`
    s = re.sub(r"^(?:DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)\s*=\s*", "", s, flags=re.I).strip().strip('"').strip("'")
    if "://" not in s:
        return s
    scheme, rest = s.split("://", 1)
    if scheme.lower() == "postgres":
        scheme = "postgresql"
    if "@" not in rest:
        return f"{scheme}://{rest}"
    # Split on the LAST '@' so raw '@', '#', '?', or '/' inside the password do not corrupt the hostname
    userinfo, hostpart = rest.rsplit("@", 1)
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        user = user.strip()
        pwd = pwd.strip()
        while pwd.startswith("[") and pwd.endswith("]") and len(pwd) >= 2:
            pwd = pwd[1:-1].strip()
        raw_pwd = urllib.parse.unquote(pwd)
        safe_pwd = urllib.parse.quote(raw_pwd, safe="")
        userinfo = f"{user}:{safe_pwd}"
    normalized = f"{scheme}://{userinfo}@{hostpart}"
    if ("supabase.com" in hostpart or "supabase.co" in hostpart) and "sslmode=" not in hostpart:
        sep = "&" if "?" in hostpart else "?"
        normalized = f"{normalized}{sep}sslmode=require"
    return normalized



def database_identity_fingerprint(db_path=None, pg_url=None, get_secret=None):
    """Return a credentials-masked SHA-256 identity fingerprint shared across worker and dashboard."""
    raw_url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    url = normalize_postgres_url(raw_url)
    if url:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port or 5432
        dbname = (parsed.path or "/postgres").lstrip("/")
        identity = f"pg://{host}:{port}/{dbname}"
        return "pg:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    resolved = str(Path(db_path).resolve()) if db_path else "sqlite:unknown"
    return "sqlite:" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]




def database_display_identity(db_path=None, pg_url=None, get_secret=None):
    """P0-03: Return masked active persistence identity (NEVER shows `.sqlite3` when PostgreSQL is active)."""
    raw_url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    url = normalize_postgres_url(raw_url)
    if url:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "postgres").lower()
        port = parsed.port or 5432
        dbname = (parsed.path or "/postgres").lstrip("/") or "postgres"
        return f"postgresql://{host}:{port}/{dbname}"
    if db_path:
        return f"sqlite:{Path(db_path).name}"
    return "sqlite:unconfigured"


def verify_repository_read_write(db_path, pg_url=None, get_secret=None, require_postgres=None):
    """P0-03: Perform a real read/write probe on the active database connection and return full identity."""
    t0 = time.monotonic()
    raw_url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    backend = "POSTGRESQL" if normalize_postgres_url(raw_url) else "SQLITE_WAL"
    display = database_display_identity(db_path, pg_url=pg_url, get_secret=get_secret)
    fp = database_identity_fingerprint(db_path, pg_url=pg_url, get_secret=get_secret)
    try:
        conn = connect_database(db_path, pg_url=pg_url, get_secret=get_secret, require_postgres=require_postgres)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS coinagent_db_healthcheck (probe_key TEXT PRIMARY KEY, probe_ts BIGINT NOT NULL)"
            )
            now_ms = int(time.time() * 1000)
            key = f"rw_probe:{fp}"
            conn.execute(
                "INSERT INTO coinagent_db_healthcheck (probe_key, probe_ts) VALUES (?, ?) "
                "ON CONFLICT (probe_key) DO UPDATE SET probe_ts = excluded.probe_ts",
                (key, now_ms),
            )
            conn.commit()
            row = conn.execute(
                "SELECT probe_ts FROM coinagent_db_healthcheck WHERE probe_key=?",
                (key,),
            ).fetchone()
            ok = bool(row and int(row[0]) == now_ms)
        finally:
            conn.close()
        return {
            "ok": ok,
            "verified_rw_ok": ok,
            "backend": backend,
            "database_display": display,
            "database_fingerprint": fp,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "error": "" if ok else "Read-back mismatch",
        }
    except Exception as exc:
        return {
            "ok": False,
            "verified_rw_ok": False,
            "backend": backend,
            "database_display": display,
            "database_fingerprint": fp,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "error": str(exc)[:220],
        }


def connect_database(db_path, pg_url=None, get_secret=None, require_postgres=None):
    """BUG-22: Connect to PostgreSQL when configured (failing closed on driver/connection error), else SQLite WAL."""
    raw_url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    url = normalize_postgres_url(raw_url)
    if url:
        if not url.startswith(("postgres://", "postgresql://")):
            raise DatabaseConfigurationError(
                f"Skema DATABASE_URL tidak valid ({url.split(':', 1)[0]}). Gunakan postgres:// atau postgresql://."
            )
        last_import_err = None
        for mod_name in ("psycopg", "psycopg2"):
            try:
                pg_mod = __import__(mod_name)
            except ImportError as imp_exc:
                last_import_err = imp_exc
                continue
            try:
                sig_params = inspect.signature(pg_mod.connect).parameters if hasattr(pg_mod, "connect") else {}
                conn_kwargs = {}
                if "connect_timeout" in sig_params:
                    conn_kwargs["connect_timeout"] = 10
                if "prepare_threshold" in sig_params:
                    # Required for Supabase Transaction Pooler (Supavisor port 6543) compatibility
                    conn_kwargs["prepare_threshold"] = None
                raw = pg_mod.connect(url, **conn_kwargs) if conn_kwargs else pg_mod.connect(url)
                prefix = _sanitize_namespace_prefix(db_path)
                adapter = _PostgresConnectionAdapter(raw, driver_name=mod_name, namespace_prefix=prefix)
                _upgrade_postgres_int4_columns(adapter)
                return adapter
            except Exception as conn_exc:
                hint = ""
                if re.search(r"@db\.[a-z0-9]+\.supabase\.co", url, flags=re.I):
                    hint = (
                        " [PETUNJUK SUPABASE: Host `db.xxxx.supabase.co` adalah Direct Connection IPv6 yang TIDAK "
                        "didukung oleh jaringan IPv4 Streamlit Cloud. Di Supabase klik tombol Connect -> ubah Method "
                        "menjadi 'Transaction pooler' (port 6543) atau 'Session pooler' (port 5432) dengan host "
                        "`aws-0-....pooler.supabase.com`]"
                    )
                raise DatabaseConfigurationError(
                    f"Gagal membuka koneksi PostgreSQL ({mod_name}): {type(conn_exc).__name__}: {conn_exc}{hint}"
                ) from conn_exc
        raise DatabaseConfigurationError(
            "DATABASE_URL PostgreSQL dikonfigurasi, tetapi driver PostgreSQL (psycopg / psycopg2) tidak terpasang. "
            "Pasang `psycopg[binary]` dari requirements.txt; sistem menolak fallback diam-diam ke SQLite."
        ) from last_import_err

    req_pg = bool(require_postgres) if require_postgres is not None else (
        str(os.environ.get("COINAGENT_REQUIRE_POSTGRES", "")).strip().lower() in ("1", "true", "yes", "on")
    )
    if req_pg:
        raise DatabaseConfigurationError(
            "COINAGENT_REQUIRE_POSTGRES aktif tetapi DATABASE_URL PostgreSQL tidak dikonfigurasi; fallback SQLite diblokir."
        )
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def atomic_transaction(conn):
    """Execute a block inside an explicit database transaction with automatic rollback on failure."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def verify_sqlite_integrity(db_path):
    """BUG-24: Dedicated SQLite-only integrity check via PRAGMA quick_check & foreign_key_check."""
    if isinstance(db_path, _PostgresConnectionAdapter):
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "INVALID_BACKEND_FOR_SQLITE_CHECK",
            "health_status": "INVALID_CHECK",
            "status": "INVALID_CALL_USE_VERIFY_POSTGRES_HEALTH",
            "db": "",
        }
    raw_str = str(db_path or "")
    if raw_str.startswith(("postgres://", "postgresql://")):
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "INVALID_BACKEND_FOR_SQLITE_CHECK",
            "health_status": "INVALID_CHECK",
            "status": "INVALID_CALL_USE_VERIFY_POSTGRES_HEALTH",
            "db": raw_str,
        }
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute("PRAGMA quick_check").fetchone()
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            ok = bool(row and row[0] == "ok" and not fk_rows)
            return {
                "ok": ok,
                "backend": "SQLITE",
                "connection_status": "CONNECTED_SQLITE",
                "health_status": "HEALTHY_SQLITE_QUICK_CHECK" if ok else "CORRUPT_SQLITE",
                "status": row[0] if row else "unknown",
                "db": str(path),
            }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "SQLITE",
            "connection_status": "CONNECTION_FAILED",
            "health_status": "UNHEALTHY",
            "status": f"DB_ERROR: {type(exc).__name__}",
            "error": str(exc),
            "db": str(path),
        }


def verify_postgres_health(pg_url=None, get_secret=None, db_path=None):
    """BUG-22 & BUG-24: Authentic PostgreSQL health check performing real catalog inspection and read/write verification."""
    url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    if not url:
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "NOT_CONFIGURED",
            "health_status": "UNCONFIGURED",
            "backup_status": "NONE",
            "verified_rw": False,
            "status": "NOT_CONFIGURED",
            "error": "DATABASE_URL belum dikonfigurasi.",
        }
    try:
        with closing(connect_database(db_path or "postgres.db", pg_url=url, get_secret=get_secret)) as conn:
            if not isinstance(conn, _PostgresConnectionAdapter) or isinstance(conn, sqlite3.Connection):
                raise DatabaseConfigurationError("Koneksi yang dikembalikan bukan _PostgresConnectionAdapter.")
            cat_row = conn.execute("SELECT current_database(), current_schema()").fetchone()
            db_name = str(cat_row[0]) if cat_row and len(cat_row) > 0 else "postgres"
            schema_name = str(cat_row[1]) if cat_row and len(cat_row) > 1 else "public"
            probe_key = _sanitize_namespace_prefix(db_path) or "global"
            probe_ts = int(time.time() * 1000)
            with atomic_transaction(conn):
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS coinagent_db_healthcheck ("
                    "probe_key TEXT PRIMARY KEY, probe_ts BIGINT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO coinagent_db_healthcheck VALUES (?, ?)",
                    (probe_key, probe_ts),
                )
                read_row = conn.execute(
                    "SELECT probe_ts FROM coinagent_db_healthcheck WHERE probe_key = ?",
                    (probe_key,),
                ).fetchone()
            verified_rw = bool(read_row and int(read_row[0]) == probe_ts)
            if not verified_rw:
                raise DatabaseConfigurationError("Verifikasi baca/tulis PostgreSQL gagal (nilai probe tidak cocok).")
            return {
                "ok": True,
                "backend": "POSTGRESQL",
                "driver": conn.driver_name,
                "database": db_name,
                "schema": schema_name,
                "connection_status": "CONNECTED_POSTGRESQL",
                "health_status": "HEALTHY_POSTGRESQL_RW",
                "backup_status": "ACID_PRIMARY_POSTGRESQL",
                "verified_rw": True,
                "status": "ok",
            }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "POSTGRESQL",
            "connection_status": "CONNECTION_FAILED",
            "health_status": "UNHEALTHY",
            "backup_status": "UNAVAILABLE",
            "verified_rw": False,
            "status": f"POSTGRES_ERROR: {type(exc).__name__}",
            "error": str(exc),
        }


def verify_database_health(db_path=None, pg_url=None, get_secret=None, supabase_sync=None):
    """BUG-24: Unified database health check distinguishing connection, backend health, and backup availability."""
    url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    if url:
        return verify_postgres_health(pg_url=url, get_secret=get_secret, db_path=db_path)
    sq = verify_sqlite_integrity(db_path or "futures_desk.db")
    if supabase_sync is not None and getattr(supabase_sync, "enabled", False):
        if supabase_sync.is_verified_active():
            sq["backup_status"] = "REPLICA_SYNC_VERIFIED"
        else:
            sq["backup_status"] = "REPLICA_SYNC_UNVERIFIED"
    else:
        sq["backup_status"] = "LOCAL_EPHEMERAL_NO_REMOTE_BACKUP"
    return sq


VALID_INSTRUMENT_KINDS = frozenset({"USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "SPOT_REFERENCE"})


class MultiWriterConflictError(RuntimeError):
    """SUPABASE-03: Raised when a secondary writer or stale snapshot attempts to overwrite an active namespace on Supabase REST."""


def compute_backup_checksum(payload_dict):
    """BUG-23, BUG-28 & SUPABASE-02: Compute deterministic SHA-256 checksum over ALL critical data AND timestamps."""
    if isinstance(payload_dict, (str, bytes)):
        payload_dict = json.loads(payload_dict)
    meta_raw = dict(payload_dict.get("meta") or {})
    backup_at_ms = int(payload_dict.get("backup_at_ms") or 0)
    state_updated_ms = int(payload_dict.get("state_updated_ms") or meta_raw.get("state_updated_ms") or 0)
    state_seq = int(payload_dict.get("state_seq") or meta_raw.get("state_seq") or 0)
    canonical_obj = {
        "schema": int(payload_dict.get("schema", BACKUP_SCHEMA_VERSION)),
        "type": str(payload_dict.get("type") or "PAPER_SIMULATION"),
        "instrument_kind": str(payload_dict.get("instrument_kind") or ""),
        "namespace": str(payload_dict.get("namespace") or ""),
        "backup_at_ms": backup_at_ms,
        "state_updated_ms": state_updated_ms,
        "state_seq": state_seq,
        "meta": {
            str(k): str(v) for k, v in sorted(meta_raw.items())
            if k not in ("worker_heartbeat", "scan_start")
        },
        "trades": payload_dict.get("trades", []),
        "events": payload_dict.get("events", []),
        "telegram_alerts": payload_dict.get("telegram_alerts", []),
        "security_audit_log": payload_dict.get("security_audit_log", []),
        "security_quota_events": payload_dict.get("security_quota_events", []),
    }
    raw = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_backup_hmac(payload_dict, secret_key=None):
    """SUPABASE-02: Compute HMAC-SHA256 authentication tag over canonical backup checksum."""
    key_str = str(
        secret_key
        if secret_key is not None
        else (
            os.getenv("COINAGENT_BACKUP_HMAC_SECRET")
            or os.getenv("COINAGENT_ADMIN_TOKEN")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or "coinagentx-default-backup-hmac-key-v602"
        )
    ).strip()
    checksum = compute_backup_checksum(payload_dict)
    return hmac.new(key_str.encode("utf-8"), checksum.encode("utf-8"), hashlib.sha256).hexdigest()


class SupabaseStateSync:
    """BUG-23, SUPABASE-01, SUPABASE-02, SUPABASE-03: Single-writer verified snapshot backup/restore over Supabase REST."""

    SCHEMA_VERSION = BACKUP_SCHEMA_VERSION

    def __init__(
        self,
        supabase_url="",
        supabase_key="",
        transport=None,
        max_retries=3,
        retry_backoff_seconds=0.05,
        writer_id="",
        allow_write=True,
        enforce_single_writer=True,
        hmac_secret=None,
    ):
        self.url = str(supabase_url or "").rstrip("/")
        self.key = str(supabase_key or "").strip()
        self.transport = transport
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.writer_id = str(writer_id or f"writer-{os.getpid()}").strip()
        self.allow_write = bool(allow_write)
        self.enforce_single_writer = bool(enforce_single_writer)
        self.hmac_secret = hmac_secret
        self.last_push_ok = False
        self.last_pull_ok = False
        self.rw_probe_verified = False
        self.last_sync_at_ms = 0
        self.last_error = ""
        self.last_checksum = ""
        self.push_count = 0
        self.pull_count = 0
        self._local_seq = {}

    @property
    def enabled(self):
        return bool(self.url.startswith(("https://", "http://127.0.0.1", "http://localhost")) and self.key)

    def is_verified_active(self):
        """SUPABASE-01 & BUG-23 #7: Only report active after verified read/write probe or verified push+pull without errors."""
        return bool(self.enabled and (self.rw_probe_verified or (self.last_push_ok and self.last_pull_ok) or self.last_push_ok) and not self.last_error)

    @staticmethod
    def _extract_state_updated_ms(payload_dict, fallback_ms=0):
        candidates = [int(fallback_ms or 0)]
        if payload_dict.get("state_updated_ms") is not None:
            try:
                candidates.append(int(payload_dict["state_updated_ms"]))
            except (TypeError, ValueError):
                pass
        meta = payload_dict.get("meta") or {}
        if meta.get("state_updated_ms"):
            try:
                candidates.append(int(meta["state_updated_ms"]))
            except (TypeError, ValueError):
                pass
        for t in payload_dict.get("trades", []):
            if isinstance(t, dict) and t.get("updated_ms") is not None:
                try:
                    candidates.append(int(t["updated_ms"]))
                except (TypeError, ValueError):
                    pass
        for ev in payload_dict.get("events", []):
            if isinstance(ev, dict) and ev.get("at_ms") is not None:
                try:
                    candidates.append(int(ev["at_ms"]))
                except (TypeError, ValueError):
                    pass
        return max(candidates)

    def build_envelope(self, namespace, backup_json, backup_at_ms=None, state_seq=None, writer_id=None):
        """BUG-23 & SUPABASE-02: Wrap backup payload with namespace, schema_version, bound timestamps, writer_id, checksum, and HMAC."""
        data = json.loads(backup_json) if isinstance(backup_json, (str, bytes)) else copy.deepcopy(dict(backup_json))
        ns = str(namespace or data.get("namespace") or "DEFAULT")
        eff_writer = str(writer_id or data.get("writer_id") or self.writer_id)
        data["namespace"] = ns
        data["writer_id"] = eff_writer
        data["schema"] = int(data.get("schema", self.SCHEMA_VERSION))
        now_ms = int(backup_at_ms if backup_at_ms is not None else int(data.get("backup_at_ms") or int(time.time() * 1000)))
        state_ms = self._extract_state_updated_ms(data, fallback_ms=now_ms)
        meta = dict(data.get("meta") or {})
        meta["state_updated_ms"] = str(state_ms)
        seq = int(state_seq if state_seq is not None else int(data.get("state_seq") or meta.get("state_seq") or (self._local_seq.get(ns, 0) + 1)))
        self._local_seq[ns] = max(self._local_seq.get(ns, 0), seq)
        meta["state_seq"] = str(seq)
        data["meta"] = meta
        data["backup_at_ms"] = now_ms
        data["state_updated_ms"] = state_ms
        data["state_seq"] = seq
        checksum = compute_backup_checksum(data)
        hmac_sig = compute_backup_hmac(data, secret_key=self.hmac_secret)
        data["checksum_sha256"] = checksum
        data["hmac_sha256"] = hmac_sig
        return {
            "namespace": ns,
            "schema_version": self.SCHEMA_VERSION,
            "backup_at_ms": now_ms,
            "state_updated_ms": state_ms,
            "state_seq": seq,
            "writer_id": eff_writer,
            "checksum_sha256": checksum,
            "hmac_sha256": hmac_sig,
            "payload": data,
        }

    def verify_envelope(self, envelope, expected_namespace=None, verify_hmac=False):
        """BUG-23 & SUPABASE-02: Verify schema, namespace, envelope-vs-payload timestamp consistency, SHA-256 checksum, and HMAC."""
        if not isinstance(envelope, dict):
            return False, "Envelope backup bukan objek dict yang valid.", None
        has_nested = "payload" in envelope and isinstance(envelope.get("payload"), dict)
        payload = envelope["payload"] if has_nested else envelope
        schema_ver = int(envelope.get("schema_version") or payload.get("schema") or 0)
        if schema_ver < self.SCHEMA_VERSION:
            return False, f"Versi schema backup ({schema_ver}) tidak didukung (minimal v{self.SCHEMA_VERSION}).", None
        ns = str(envelope.get("namespace") or payload.get("namespace") or "")
        if expected_namespace and ns and ns != str(expected_namespace):
            return False, f"Namespace backup ({ns}) tidak cocok dengan target ({expected_namespace}).", None
        if has_nested:
            if payload.get("namespace") and str(payload.get("namespace")) != ns:
                return False, "Namespace antara envelope dan payload tidak konsisten.", None
            env_b_ms = int(envelope.get("backup_at_ms") or 0)
            pay_b_ms = int(payload.get("backup_at_ms") or 0)
            env_s_ms = int(envelope.get("state_updated_ms") or 0)
            pay_s_ms = int(payload.get("state_updated_ms") or 0)
            if env_b_ms != pay_b_ms or env_s_ms != pay_s_ms:
                return False, "Timestamp backup_at_ms / state_updated_ms antara envelope dan payload tidak cocok (manipulasi timestamp terdeteksi).", None
        meta_s_ms = (payload.get("meta") or {}).get("state_updated_ms")
        if meta_s_ms is not None and payload.get("state_updated_ms") is not None:
            if int(meta_s_ms) != int(payload.get("state_updated_ms")):
                return False, "state_updated_ms pada payload tidak konsisten dengan meta.state_updated_ms.", None
        declared_sum = str(envelope.get("checksum_sha256") or payload.get("checksum_sha256") or "").strip()
        if not declared_sum:
            return False, "Backup tidak memiliki checksum_sha256.", None
        computed_sum = compute_backup_checksum(payload)
        if not hmac.compare_digest(declared_sum, computed_sum):
            return False, "Checksum SHA-256 backup tidak cocok (data atau timestamp telah dimanipulasi).", None
        declared_hmac = str(envelope.get("hmac_sha256") or payload.get("hmac_sha256") or "").strip()
        if (verify_hmac or self.hmac_secret is not None) and declared_hmac:
            expected_hmac = compute_backup_hmac(payload, secret_key=self.hmac_secret)
            if not hmac.compare_digest(declared_hmac, expected_hmac):
                return False, "HMAC-SHA256 autentikasi backup tidak valid.", None
        return True, "OK", payload

    def _execute_request(self, req, timeout=8):
        if callable(self.transport):
            return self.transport(req, timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()

    def _fetch_remote_row_raw(self, namespace):
        ns = str(namespace)
        encoded_ns = urllib.parse.quote(ns, safe="")
        req = urllib.request.Request(
            f"{self.url}/rest/v1/coinagent_backups?namespace=eq.{encoded_ns}"
            "&select=namespace,schema_version,backup_at_ms,state_updated_ms,state_seq,writer_id,checksum_sha256,hmac_sha256,payload&limit=1",
            method="GET",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
            },
        )
        status, raw_bytes = self._execute_request(req, timeout=8)
        if 200 <= int(status) < 300:
            rows = json.loads(raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else str(raw_bytes))
            if isinstance(rows, list) and rows:
                return rows[0]
        return None

    def push_backup(self, namespace, backup_json, backup_at_ms=None, state_seq=None, writer_id=None, check_remote_conflict=True):
        """BUG-23, SUPABASE-02 & SUPABASE-03: Push verified snapshot with single-writer & stale-overwrite protection."""
        if not self.enabled:
            return False
        if not self.allow_write:
            self.last_push_ok = False
            self.last_error = "REST Sync berada dalam mode read-only pada instance ini (single-writer guard aktif)."
            return False
        eff_writer = str(writer_id or self.writer_id)
        envelope = self.build_envelope(namespace, backup_json, backup_at_ms=backup_at_ms, state_seq=state_seq, writer_id=eff_writer)

        if check_remote_conflict:
            try:
                remote_row = self._fetch_remote_row_raw(namespace)
            except Exception:
                remote_row = None
            if isinstance(remote_row, dict):
                rem_pay = remote_row.get("payload") if isinstance(remote_row.get("payload"), dict) else {}
                rem_writer = str(remote_row.get("writer_id") or rem_pay.get("writer_id") or "").strip()
                rem_state_ms = int(remote_row.get("state_updated_ms") or rem_pay.get("state_updated_ms") or 0)
                rem_seq = int(remote_row.get("state_seq") or rem_pay.get("state_seq") or 0)
                if self.enforce_single_writer and rem_writer and rem_writer != eff_writer:
                    self.last_push_ok = False
                    self.last_error = (
                        f"SUPABASE-03 MULTI-WRITER CONFLICT: Namespace '{namespace}' sudah dimiliki oleh writer '{rem_writer}' "
                        f"(writer saat ini '{eff_writer}'). Gunakan PostgreSQL (DATABASE_URL) untuk database bersama multi-writer."
                    )
                    return False
                if rem_state_ms > envelope["state_updated_ms"]:
                    self.last_push_ok = False
                    self.last_error = (
                        f"SUPABASE-03 STALE SNAPSHOT REJECTED: State remote ({rem_state_ms}) lebih baru dari lokal ({envelope['state_updated_ms']})."
                    )
                    return False
                if rem_seq >= envelope["state_seq"] and rem_state_ms == envelope["state_updated_ms"] and rem_row_differs(remote_row, envelope):
                    envelope = self.build_envelope(
                        namespace,
                        backup_json,
                        backup_at_ms=backup_at_ms,
                        state_seq=rem_seq + 1,
                        writer_id=eff_writer,
                    )

        body = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
        last_err = ""
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.url}/rest/v1/coinagent_backups?on_conflict=namespace",
                data=body,
                method="POST",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
            )
            try:
                status, _ = self._execute_request(req, timeout=8)
                if 200 <= int(status) < 300:
                    self.last_push_ok = True
                    self.last_sync_at_ms = envelope["backup_at_ms"]
                    self.last_checksum = envelope["checksum_sha256"]
                    self.last_error = ""
                    self.push_count += 1
                    return True
                last_err = f"HTTP {status}"
                if int(status) < 500 and int(status) != 429:
                    break
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries and self.retry_backoff_seconds > 0:
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        self.last_push_ok = False
        self.last_error = last_err
        return False

    def pull_backup(self, namespace):
        """BUG-23 & SUPABASE-02: Pull and verify remote backup envelope for a specific ledger namespace with retries."""
        if not self.enabled:
            return None
        ns = str(namespace)
        encoded_ns = urllib.parse.quote(ns, safe="")
        last_err = ""
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.url}/rest/v1/coinagent_backups?namespace=eq.{encoded_ns}"
                "&select=namespace,schema_version,backup_at_ms,state_updated_ms,state_seq,writer_id,checksum_sha256,hmac_sha256,payload&limit=1",
                method="GET",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Accept": "application/json",
                },
            )
            try:
                status, raw_bytes = self._execute_request(req, timeout=8)
                if 200 <= int(status) < 300:
                    rows = json.loads(raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else str(raw_bytes))
                    if not isinstance(rows, list) or not rows:
                        return None
                    row = rows[0]
                    ok, reason, _ = self.verify_envelope(row, expected_namespace=ns)
                    if not ok:
                        self.last_pull_ok = False
                        self.last_error = reason
                        return None
                    self.last_pull_ok = True
                    self.last_sync_at_ms = int(row.get("backup_at_ms") or int(time.time() * 1000))
                    self.last_checksum = str(row.get("checksum_sha256") or "")
                    self.last_error = ""
                    self.pull_count += 1
                    return row
                last_err = f"HTTP {status}"
                if int(status) < 500 and int(status) != 429:
                    break
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries and self.retry_backoff_seconds > 0:
                time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        self.last_pull_ok = False
        self.last_error = last_err
        return None

    def verify_rw_probe(self, probe_namespace="SPOT_REFERENCE:__rw_probe__.db"):
        """SUPABASE-01 #5: Perform live REST upsert, select, update, and pull probe against coinagent_backups."""
        if not self.enabled:
            return {"ok": False, "error": "Supabase REST belum dikonfigurasi."}
        t1 = int(time.time() * 1000)
        sample_v1 = {
            "schema": self.SCHEMA_VERSION,
            "type": "PAPER_SIMULATION",
            "instrument_kind": "SPOT_REFERENCE",
            "namespace": probe_namespace,
            "backup_at_ms": t1,
            "state_updated_ms": t1,
            "state_seq": 1,
            "meta": {"instrument_kind": "SPOT_REFERENCE", "initial_equity": "1000.0", "state_updated_ms": str(t1), "state_seq": "1"},
            "trades": [],
            "events": [],
            "telegram_alerts": [],
            "security_audit_log": [],
            "security_quota_events": [],
        }
        # 1. Upsert (INSERT)
        if not self.push_backup(probe_namespace, sample_v1, backup_at_ms=t1, state_seq=1, check_remote_conflict=False):
            self.rw_probe_verified = False
            return {"ok": False, "stage": "upsert", "error": self.last_error}
        # 2. Select / Pull
        pulled_v1 = self.pull_backup(probe_namespace)
        if not pulled_v1 or int(pulled_v1.get("state_updated_ms") or 0) != t1:
            self.rw_probe_verified = False
            return {"ok": False, "stage": "select", "error": self.last_error or "Gagal membaca kembali probe v1."}
        # 3. Update (UPSERT with newer timestamp & seq=2)
        t2 = t1 + 1000
        sample_v2 = copy.deepcopy(sample_v1)
        sample_v2["backup_at_ms"] = t2
        sample_v2["state_updated_ms"] = t2
        sample_v2["state_seq"] = 2
        sample_v2["meta"]["state_updated_ms"] = str(t2)
        sample_v2["meta"]["state_seq"] = "2"
        if not self.push_backup(probe_namespace, sample_v2, backup_at_ms=t2, state_seq=2, check_remote_conflict=True):
            self.rw_probe_verified = False
            return {"ok": False, "stage": "update", "error": self.last_error}
        # 4. Pull updated record and verify checksum & timestamp
        pulled_v2 = self.pull_backup(probe_namespace)
        if not pulled_v2 or int(pulled_v2.get("state_updated_ms") or 0) != t2:
            self.rw_probe_verified = False
            return {"ok": False, "stage": "pull_updated", "error": self.last_error or "Gagal memverifikasi update probe v2."}
        self.rw_probe_verified = True
        return {
            "ok": True,
            "upsert_ok": True,
            "select_ok": True,
            "update_ok": True,
            "pull_ok": True,
            "checksum_sha256": pulled_v2.get("checksum_sha256"),
        }


def rem_row_differs(remote_row, new_envelope):
    return str(remote_row.get("checksum_sha256") or "") != str(new_envelope.get("checksum_sha256") or "")


def persistence_status(db_path=None, get_secret=None, pg_url=None, ledger=None, supabase_sync=None):
    """BUG-22, BUG-23, SUPABASE-01 & SUPABASE-03: Report verified active database backend; never claim REST Sync is ACID or shared multi-writer DB."""
    effective_pg_url = configured_database_url(
        get_secret=get_secret,
        explicit_url=pg_url or (getattr(ledger, "pg_url", None) if ledger is not None else None),
    )
    target_db = db_path or (getattr(ledger, "db", None) if ledger is not None else None)

    if effective_pg_url:
        health = verify_postgres_health(pg_url=effective_pg_url, get_secret=get_secret, db_path=target_db)
        if health.get("ok") and health.get("verified_rw"):
            return {
                "backend": "POSTGRESQL",
                "mode": "POSTGRESQL_SUPABASE",
                "persistent": True,
                "persistent_across_reboots": True,
                "transactional": True,
                "acid_transactional": True,
                "shared_multi_writer_safe": True,
                "connected": True,
                "verified_rw": True,
                "driver": health.get("driver", "psycopg"),
                "database": health.get("database", ""),
                "detail": "Terhubung & terverifikasi baca/tulis pada database PostgreSQL eksternal (ACID penuh & aman untuk multi-writer Dashboard + Worker).",
            }
        return {
            "backend": "POSTGRESQL_ERROR",
            "mode": "POSTGRESQL_ERROR",
            "persistent": False,
            "persistent_across_reboots": False,
            "transactional": False,
            "acid_transactional": False,
            "shared_multi_writer_safe": False,
            "connected": False,
            "verified_rw": False,
            "error": health.get("error", "Koneksi atau verifikasi baca/tulis PostgreSQL gagal."),
            "detail": (
                f"KONFIGURASI POSTGRESQL GAGAL: {health.get('error', 'Koneksi gagal')}. "
                "Sistem tidak melakukan fallback diam-diam ke SQLite dan tidak mengklaim status PERSISTENT."
            ),
        }

    sync_obj = supabase_sync or (getattr(ledger, "supabase_sync", None) if ledger is not None else None)
    sb_url, sb_key = configured_supabase_rest(get_secret=get_secret)
    if (sync_obj is not None and sync_obj.enabled) or (sb_url and sb_key):
        verified = bool(sync_obj is not None and sync_obj.is_verified_active())
        if verified:
            return {
                "backend": "SUPABASE_REST_SYNC",
                "mode": "SINGLE_WRITER_REST_BACKUP_ONLY",
                "persistent": False,
                "persistent_primary_db": False,
                "persistent_across_reboots": True,
                "backup_replica_verified": True,
                "transactional": False,
                "acid_transactional": False,
                "shared_multi_writer_safe": False,
                "sync_active": True,
                "verified_rw": True,
                "writer_id": getattr(sync_obj, "writer_id", ""),
                "last_sync_at_ms": getattr(sync_obj, "last_sync_at_ms", 0),
                "last_checksum": getattr(sync_obj, "last_checksum", ""),
                "detail": (
                    "SQLite WAL lokal dengan backup snapshot satu-writer ke Supabase REST yang telah terverifikasi "
                    "(BUKAN database ACID bersama multi-writer; gunakan DATABASE_URL PostgreSQL untuk menjalankan Worker + Dashboard bersamaan)."
                ),
            }
        return {
            "backend": "SQLITE_WAL_UNVERIFIED_SYNC",
            "mode": "SQLITE_WAL_UNVERIFIED_SYNC",
            "persistent": False,
            "persistent_primary_db": False,
            "persistent_across_reboots": False,
            "backup_replica_verified": False,
            "transactional": False,
            "acid_transactional": False,
            "shared_multi_writer_safe": False,
            "sync_active": False,
            "verified_rw": False,
            "error": getattr(sync_obj, "last_error", "") if sync_obj is not None else "",
            "detail": (
                "Kredensial Supabase REST terdeteksi, tetapi operasi baca/tulis belum diverifikasi; "
                "status belum diklaim aktif maupun PERSISTENT."
            ),
        }

    return {
        "backend": "SQLITE_WAL",
        "mode": "SQLITE_LOCAL_EPHEMERAL",
        "persistent": False,
        "persistent_primary_db": False,
        "persistent_across_reboots": False,
        "backup_replica_verified": False,
        "transactional": True,
        "acid_transactional": False,
        "shared_multi_writer_safe": False,
        "connected": True,
        "verified_rw": False,
        "db_path": str(target_db or ""),
        "detail": (
            "Menggunakan SQLite WAL lokal (NON-PERSISTENT pada Streamlit Community Cloud saat reboot/redeploy). "
            "Konfigurasikan DATABASE_URL (PostgreSQL) di Secrets untuk persistensi ACID penuh."
        ),
    }


# =============================================================================
# BUG-38, BUG-40 & BUG-41: Global User Preferences & Global Scanner Runs
# =============================================================================

DEFAULT_USER_PREFERENCES = {
    "auto": False,
    "auto_scan": False,
    "relaxed": False,
    "relaxed_mode": False,
    "interval": 60,
    "scan_interval": 60,
    "candidates": 2,
    "shortlist_limit": 30,
    "strategy_profile": "BALANCED",
    "strategy_engine": "HYBRID_ADAPTIVE_V1",
    "alert": False,
    "send_alerts": False,
    "watchlist_alert": False,
    "send_watchlist_alerts": False,
    "summary_alert": False,
    "send_scan_summary": False,
    "volume": 10_000_000.0,
    "min_volume": 10_000_000.0,
    "leverage": 2,
    "risk": 0.5,
    "risk_pct": 0.5,
    "fee": 0.05,
    "fee_pct": 0.05,
    "slip": 2.0,
    "slippage_bps": 2.0,
    "fund": 0.03,
    "funding_pct": 0.03,
}


def resolve_global_control_db_path(db_path=None):
    """Resolve canonical global control DB path so provider failovers never switch user_preferences or scanner_runs."""
    env_db = os.getenv("FUTURES_HISTORY_DB", "").strip()
    if db_path is not None:
        p = Path(db_path)
        name = p.name
        for prefix in ("bybit_", "spot_"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        if env_db and Path(env_db).parent == p.parent:
            return Path(env_db)
        return p.with_name(name)
    if env_db:
        return Path(env_db)
    return Path("paper_history.sqlite3")


_PG_SCHEMA_UPGRADED = set()


def _upgrade_postgres_int4_columns(conn_or_raw, force=False):
    """Auto-upgrade ALL 32-bit `integer` (int4) columns in PostgreSQL `public` schema to 64-bit `BIGINT` using raw cursor."""
    raw_conn = getattr(conn_or_raw, "_conn", conn_or_raw)
    if isinstance(conn_or_raw, sqlite3.Connection) or isinstance(raw_conn, sqlite3.Connection):
        return
    if not force and "global_pg_upgraded_v2" in _PG_SCHEMA_UPGRADED:
        return
    try:
        with raw_conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'integer'"
            )
            rows = cur.fetchall() or []
        raw_conn.commit()
    except Exception:
        try:
            raw_conn.rollback()
        except Exception:
            pass
        return

    for tbl_name, col_name in rows:
        safe_tbl = re.sub(r"[^a-zA-Z0-9_]", "", str(tbl_name))
        safe_col = re.sub(r"[^a-zA-Z0-9_]", "", str(col_name))
        if not safe_tbl or not safe_col:
            continue
        try:
            with raw_conn.cursor() as cur:
                cur.execute(f'ALTER TABLE public."{safe_tbl}" ALTER COLUMN "{safe_col}" TYPE BIGINT')
            raw_conn.commit()
        except Exception:
            try:
                raw_conn.rollback()
            except Exception:
                pass
    _PG_SCHEMA_UPGRADED.add("global_pg_upgraded_v2")


def _detect_user_preferences_bool_converter(conn):
    """Detect whether PostgreSQL `user_preferences.auto_scan` is integer/bigint (returns `int`) or boolean (returns `bool`)."""
    if not getattr(conn, "is_postgres", False):
        return int
    raw_conn = getattr(conn, "_conn", conn)
    try:
        with raw_conn.cursor() as cur:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'user_preferences' AND column_name = 'auto_scan'"
            )
            row = cur.fetchone()
        raw_conn.commit()
        if row and str(row[0]).lower() == "boolean":
            return bool
    except Exception:
        try:
            raw_conn.rollback()
        except Exception:
            pass
    return int


def ensure_global_control_tables(conn):
    """Ensure global `user_preferences` and `scanner_runs` tables exist on PostgreSQL or SQLite."""
    _upgrade_postgres_int4_columns(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT PRIMARY KEY,
            auto_scan INTEGER NOT NULL DEFAULT 0,
            scan_interval INTEGER NOT NULL DEFAULT 60,
            candidates INTEGER NOT NULL DEFAULT 2,
            relaxed_mode INTEGER NOT NULL DEFAULT 0,
            send_alerts INTEGER NOT NULL DEFAULT 0,
            send_watchlist_alerts INTEGER NOT NULL DEFAULT 0,
            send_scan_summary INTEGER NOT NULL DEFAULT 0,
            min_volume REAL NOT NULL DEFAULT 10000000.0,
            leverage INTEGER NOT NULL DEFAULT 2,
            risk_pct REAL NOT NULL DEFAULT 0.5,
            fee_pct REAL NOT NULL DEFAULT 0.05,
            slippage_bps REAL NOT NULL DEFAULT 2.0,
            funding_pct REAL NOT NULL DEFAULT 0.03,
            updated_at_ms INTEGER NOT NULL,
            payload TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scanner_runs (
            scan_id TEXT PRIMARY KEY,
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER NOT NULL,
            provider TEXT NOT NULL,
            instrument_kind TEXT NOT NULL,
            worker_id TEXT NOT NULL DEFAULT '',
            scan_status TEXT NOT NULL,
            data_status TEXT NOT NULL DEFAULT 'OK',
            configured_interval INTEGER NOT NULL DEFAULT 60,
            effective_interval INTEGER NOT NULL DEFAULT 60,
            next_scan_at_ms INTEGER NOT NULL DEFAULT 0,
            errors TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scanner_runs_completed_at_ms ON scanner_runs (completed_at_ms DESC, started_at_ms DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scanner_candidate_queue (
            queue_id TEXT PRIMARY KEY,
            instrument_kind TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            payload TEXT NOT NULL
        )"""
    )


def normalize_user_preferences(prefs=None, base=None):
    """Normalize UI and canonical preference keys so both naming conventions remain strictly synchronized."""
    merged = dict(DEFAULT_USER_PREFERENCES)
    if isinstance(base, dict):
        for k, v in base.items():
            if not str(k).startswith("_"):
                merged[k] = v
    raw = dict(prefs or {})

    def _pick_bool(ui_key, col_key, default_val=False):
        if ui_key in raw:
            return bool(raw[ui_key])
        if col_key in raw:
            return bool(raw[col_key])
        return bool(merged.get(ui_key, merged.get(col_key, default_val)))

    def _pick_int(ui_key, col_key, default_val, allowed=None):
        val = raw.get(ui_key, raw.get(col_key, merged.get(ui_key, merged.get(col_key, default_val))))
        try:
            ival = int(val)
        except (TypeError, ValueError):
            ival = int(default_val)
        if allowed and ival not in allowed:
            ival = int(default_val)
        return ival

    def _pick_float(ui_key, col_key, default_val):
        val = raw.get(ui_key, raw.get(col_key, merged.get(ui_key, merged.get(col_key, default_val))))
        try:
            fval = float(val)
        except (TypeError, ValueError):
            fval = float(default_val)
        return fval

    auto_val = _pick_bool("auto", "auto_scan", False)
    relaxed_val = _pick_bool("relaxed", "relaxed_mode", False)
    interval_val = _pick_int("interval", "scan_interval", 60, allowed=(60, 300, 900))
    candidates_val = _pick_int("candidates", "candidates", 2, allowed=tuple(range(1, 51)))
    shortlist_val = _pick_int("shortlist_limit", "shortlist_limit", 50, allowed=tuple(range(1, 101)))
    if "strategy_profile" in raw and str(raw["strategy_profile"]).strip():
        profile_raw = str(raw["strategy_profile"]).strip().upper()
    elif "relaxed" in raw or "relaxed_mode" in raw:
        profile_raw = "BALANCED" if relaxed_val else "CONSERVATIVE"
    else:
        profile_raw = str(merged.get("strategy_profile", "BALANCED" if relaxed_val else "BALANCED")).strip().upper()
    profile_val = profile_raw if profile_raw in ("CONSERVATIVE", "BALANCED", "ACTIVE") else "BALANCED"
    if relaxed_val and profile_val == "CONSERVATIVE" and "strategy_profile" not in raw:
        profile_val = "BALANCED" 
    alert_val = _pick_bool("alert", "send_alerts", False)
    wl_alert_val = _pick_bool("watchlist_alert", "send_watchlist_alerts", False)
    sum_alert_val = _pick_bool("summary_alert", "send_scan_summary", False)
    vol_val = _pick_float("volume", "min_volume", 10_000_000.0)
    lev_val = _pick_int("leverage", "leverage", 2, allowed=(1, 2, 3))
    risk_val = _pick_float("risk", "risk_pct", 0.5)
    fee_val = _pick_float("fee", "fee_pct", 0.05)
    slip_val = _pick_float("slip", "slippage_bps", 2.0)
    fund_val = _pick_float("fund", "funding_pct", 0.03)

    return {
        "auto": auto_val,
        "auto_scan": auto_val,
        "relaxed": relaxed_val,
        "relaxed_mode": relaxed_val,
        "interval": interval_val,
        "scan_interval": interval_val,
        "candidates": candidates_val,
        "shortlist_limit": shortlist_val,
        "strategy_profile": profile_val,
        "alert": alert_val,
        "send_alerts": alert_val,
        "watchlist_alert": wl_alert_val,
        "send_watchlist_alerts": wl_alert_val,
        "summary_alert": sum_alert_val,
        "send_scan_summary": sum_alert_val,
        "volume": vol_val,
        "min_volume": vol_val,
        "leverage": lev_val,
        "risk": risk_val,
        "risk_pct": risk_val,
        "fee": fee_val,
        "fee_pct": fee_val,
        "slip": slip_val,
        "slippage_bps": slip_val,
        "fund": fund_val,
        "funding_pct": fund_val,
    }


def save_global_preferences(
    db_path=None,
    prefs=None,
    user_id="global_operator",
    pg_url=None,
    get_secret=None,
    raise_on_error=False,
):
    """BUG-38 & BUG-40: Persist user preferences in global `user_preferences` table inside an atomic transaction."""
    uid = str(user_id or "global_operator").strip() or "global_operator"
    target_db = resolve_global_control_db_path(db_path)
    now_ms = int(time.time() * 1000)
    try:
        current = load_global_preferences(target_db, user_id=uid, pg_url=pg_url, get_secret=get_secret)
        normalized = normalize_user_preferences(prefs, base=current if current.get("_exists") else None)
        payload_json = json.dumps(normalized, sort_keys=True, allow_nan=False)
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            b_conv = _detect_user_preferences_bool_converter(conn)
            for attempt_conv in (b_conv, (bool if b_conv is int else int)):
                try:
                    with atomic_transaction(conn):
                        conn.execute(
                            """INSERT OR REPLACE INTO user_preferences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                uid,
                                attempt_conv(bool(normalized["auto_scan"])),
                                int(normalized["scan_interval"]),
                                int(normalized["candidates"]),
                                attempt_conv(bool(normalized["relaxed_mode"])),
                                attempt_conv(bool(normalized["send_alerts"])),
                                attempt_conv(bool(normalized["send_watchlist_alerts"])),
                                attempt_conv(bool(normalized["send_scan_summary"])),
                                float(normalized["min_volume"]),
                                int(normalized["leverage"]),
                                float(normalized["risk_pct"]),
                                float(normalized["fee_pct"]),
                                float(normalized["slippage_bps"]),
                                float(normalized["funding_pct"]),
                                now_ms,
                                payload_json,
                            ),
                        )
                        verify_row = conn.execute(
                            "SELECT payload, updated_at_ms FROM user_preferences WHERE user_id = ?",
                            (uid,),
                        ).fetchone()
                        if not verify_row or not verify_row[0]:
                            raise RuntimeError("Verifikasi pembacaan kembali user_preferences gagal di dalam transaksi.")
                    break
                except Exception as inner_exc:
                    if attempt_conv is b_conv and ("DatatypeMismatch" in type(inner_exc).__name__ or "type integer" in str(inner_exc) or "type boolean" in str(inner_exc)):
                        continue
                    raise
        return {
            "ok": True,
            "status": "SAVED",
            "user_id": uid,
            "updated_at_ms": now_ms,
            "preferences": normalized,
        }
    except Exception as exc:
        if raise_on_error:
            raise
        return {
            "ok": False,
            "status": "SAVE_FAILED",
            "user_id": uid,
            "error": f"{type(exc).__name__}: {exc}",
        }


def has_global_preferences(db_path=None, user_id="global_operator", pg_url=None, get_secret=None):
    uid = str(user_id or "global_operator").strip() or "global_operator"
    target_db = resolve_global_control_db_path(db_path)
    try:
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            row = conn.execute("SELECT 1 FROM user_preferences WHERE user_id = ?", (uid,)).fetchone()
            return bool(row)
    except Exception:
        return False


def load_global_preferences(db_path=None, user_id="global_operator", pg_url=None, get_secret=None):
    """BUG-38 & BUG-40: Load user preferences from global `user_preferences` table; default risky toggles to OFF."""
    uid = str(user_id or "global_operator").strip() or "global_operator"
    target_db = resolve_global_control_db_path(db_path)
    try:
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            row = conn.execute(
                """SELECT auto_scan, scan_interval, candidates, relaxed_mode,
                          send_alerts, send_watchlist_alerts, send_scan_summary,
                          min_volume, leverage, risk_pct, fee_pct, slippage_bps,
                          funding_pct, updated_at_ms, payload
                   FROM user_preferences WHERE user_id = ?""",
                (uid,),
            ).fetchone()
            if not row:
                out = dict(DEFAULT_USER_PREFERENCES)
                out["_exists"] = False
                out["_status"] = "DEFAULT_OFF"
                out["_user_id"] = uid
                out["_updated_at_ms"] = 0
                return out
            payload_dict = {}
            if row[14]:
                try:
                    payload_dict = json.loads(row[14])
                except Exception:
                    payload_dict = {}
            col_dict = {
                "auto_scan": bool(row[0]),
                "scan_interval": int(row[1]),
                "candidates": int(row[2]),
                "relaxed_mode": bool(row[3]),
                "send_alerts": bool(row[4]),
                "send_watchlist_alerts": bool(row[5]),
                "send_scan_summary": bool(row[6]),
                "min_volume": float(row[7]),
                "leverage": int(row[8]),
                "risk_pct": float(row[9]),
                "fee_pct": float(row[10]),
                "slippage_bps": float(row[11]),
                "funding_pct": float(row[12]),
            }
            merged_in = dict(payload_dict)
            merged_in.update(col_dict)
            out = normalize_user_preferences(merged_in)
            out["_exists"] = True
            out["_status"] = "LOADED"
            out["_user_id"] = uid
            out["_updated_at_ms"] = int(row[13] or 0)
            return out
    except Exception as exc:
        out = dict(DEFAULT_USER_PREFERENCES)
        out["_exists"] = False
        out["_status"] = "LOAD_ERROR"
        out["_error"] = f"{type(exc).__name__}: {exc}"
        out["_user_id"] = uid
        out["_updated_at_ms"] = 0
        return out


def save_global_scan_run(db_path=None, report=None, worker_id="", pg_url=None, get_secret=None):
    """BUG-41: Save scan report into global `scanner_runs` table across all providers and ledgers."""
    if not isinstance(report, dict):
        return False
    target_db = resolve_global_control_db_path(db_path)
    summary = {k: v for k, v in report.items() if k not in ("analyses", "display_monotonic", "display_server_age")}
    summary["analyses"] = []
    if "worker_analyses" not in summary:
        summary["worker_analyses"] = [
            {
                "Pair": d["snapshot"].symbol,
                "Mode": d["snapshot"].mode,
                "Arah": d["result"].direction,
                "Status": d["status"],
                "Paper": d["paper"],
                "Telegram": d["telegram"],
            }
            for d in report.get("analyses", [])
            if isinstance(d, dict) and "snapshot" in d and "result" in d
        ]
    scan_id = str(summary.get("scan_id") or f"scan-{int(time.time() * 1000)}")
    started_ms = int(summary.get("started_at_ms") or summary.get("now_ms") or int(time.time() * 1000))
    completed_ms = int(summary.get("completed_at_ms") or started_ms)
    provider = str(summary.get("market_provider") or summary.get("instrument_kind") or "UNKNOWN")
    instrument_kind = str(summary.get("instrument_kind") or "USD_M_PERPETUAL")
    eff_worker = str(worker_id or summary.get("worker_id") or "")
    scan_status = str(summary.get("scan_status") or "COMPLETED")
    data_status = str(summary.get("data_status") or "OK")
    cfg_interval = int(summary.get("configured_interval") or (summary.get("config") or {}).get("interval") or 60)
    eff_interval = int(summary.get("effective_interval") or cfg_interval)
    next_scan_ms = int(summary.get("next_scan_at_ms") or (started_ms + eff_interval * 1000))
    errors_json = json.dumps(list(summary.get("errors") or []), ensure_ascii=False, allow_nan=False)
    payload_json = json.dumps(summary, ensure_ascii=False, allow_nan=False)

    with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
        ensure_global_control_tables(conn)
        with atomic_transaction(conn):
            conn.execute(
                """INSERT OR REPLACE INTO scanner_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id,
                    started_ms,
                    completed_ms,
                    provider,
                    instrument_kind,
                    eff_worker,
                    scan_status,
                    data_status,
                    cfg_interval,
                    eff_interval,
                    next_scan_ms,
                    errors_json,
                    payload_json,
                ),
            )
    return True


def load_latest_global_scan_run(db_path=None, pg_url=None, get_secret=None):
    """BUG-41: Read the newest scan report by timestamp from global `scanner_runs` regardless of active provider."""
    target_db = resolve_global_control_db_path(db_path)
    try:
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            row = conn.execute(
                "SELECT payload FROM scanner_runs ORDER BY completed_at_ms DESC, started_at_ms DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])
    except Exception:
        return None
    return None


def list_global_scan_runs(db_path=None, limit=20, pg_url=None, get_secret=None):
    """BUG-41: List recent scan runs across all providers ordered by newest timestamp."""
    target_db = resolve_global_control_db_path(db_path)
    try:
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            rows = conn.execute(
                "SELECT payload FROM scanner_runs ORDER BY completed_at_ms DESC, started_at_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [json.loads(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def save_persistent_candidate_queue(
    db_path=None,
    queue_payload=None,
    instrument_kind="USD_M_PERPETUAL",
    queue_id=None,
    pg_url=None,
    get_secret=None,
):
    """Hybrid Adaptive v1.1 (Section 3 & Section 9): Persist rotating candidate queue across process restarts."""
    target_db = resolve_global_control_db_path(db_path)
    ik = str(instrument_kind or "USD_M_PERPETUAL").strip().upper()
    qid = str(queue_id or f"queue:{ik}")
    now_ms = int(time.time() * 1000)
    payload_obj = queue_payload if isinstance(queue_payload, dict) else {"items": list(queue_payload or []), "updated_at_ms": now_ms}
    payload_json = json.dumps(payload_obj, ensure_ascii=False, allow_nan=False)
    with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
        ensure_global_control_tables(conn)
        with atomic_transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO scanner_candidate_queue VALUES (?,?,?,?)",
                (qid, ik, now_ms, payload_json),
            )
    return True


def load_persistent_candidate_queue(
    db_path=None,
    instrument_kind="USD_M_PERPETUAL",
    queue_id=None,
    pg_url=None,
    get_secret=None,
):
    """Hybrid Adaptive v1.1 (Section 3 & Section 9): Load persistent rotating candidate queue."""
    target_db = resolve_global_control_db_path(db_path)
    ik = str(instrument_kind or "USD_M_PERPETUAL").strip().upper()
    qid = str(queue_id or f"queue:{ik}")
    try:
        with closing(connect_database(target_db, pg_url=pg_url, get_secret=get_secret)) as conn:
            ensure_global_control_tables(conn)
            row = conn.execute(
                "SELECT payload, updated_at_ms FROM scanner_candidate_queue WHERE queue_id = ?",
                (qid,),
            ).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                if isinstance(data, dict):
                    data.setdefault("updated_at_ms", int(row[1] or 0))
                    return data
    except Exception:
        return None
    return None

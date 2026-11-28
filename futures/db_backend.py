"""Lean persistent database backend for CoinAgentX v7 (Supabase PostgreSQL, Supabase REST sync, SQLite)."""
from contextlib import closing, contextmanager
from pathlib import Path
import copy
import hashlib
import inspect
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

BACKUP_SCHEMA_VERSION = 2


class DatabaseConfigurationError(RuntimeError):
    """Raised when PostgreSQL is configured but driver is missing, URL invalid, or connection fails."""


def _effective_getter(get_secret=None):
    return get_secret if callable(get_secret) else (lambda k: os.getenv(k, ""))


def configured_database_url(get_secret=None, explicit_url=None):
    if explicit_url is not None and str(explicit_url).strip():
        return str(explicit_url).strip()
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


def normalize_postgres_url(raw_url):
    """Sanitize common paste mistakes in DATABASE_URL (quotes, key prefix, raw password symbols, sslmode)."""
    s = str(raw_url or "").strip().strip('"').strip("'")
    if not s:
        return ""
    s = re.sub(r"^(?:DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)\s*=\s*", "", s, flags=re.I).strip().strip('"').strip("'")
    if "://" not in s:
        return s
    scheme, rest = s.split("://", 1)
    if "@" in rest:
        userinfo, hostpart = rest.rsplit("@", 1)
        if ":" in userinfo:
            user, pwd = userinfo.split(":", 1)
            if pwd.startswith("[") and pwd.endswith("]") and len(pwd) >= 2:
                pwd = pwd[1:-1]
            pwd_enc = urllib.parse.quote(urllib.parse.unquote(pwd), safe="")
            rest = f"{user}:{pwd_enc}@{hostpart}"
    out = f"{scheme}://{rest}"
    if "supabase.com" in out.lower() or "supabase.co" in out.lower():
        if "sslmode=" not in out.lower():
            out += ("&" if "?" in out else "?") + "sslmode=require"
    return out


class _PostgresCursorAdapter:
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
    """Wraps psycopg/psycopg2 connection with SQLite-compatible `.execute()` and ACID transactions."""

    is_postgres = True
    backend_kind = "POSTGRESQL"

    def __init__(self, raw_conn, driver_name="psycopg"):
        self._conn = raw_conn
        self.driver_name = driver_name

    @staticmethod
    def _translate_sql(sql, namespace_prefix=""):
        _ = namespace_prefix  # Kept for signature compatibility; simple_* tables are never rewritten.
        s = sql.strip()
        upper = s.upper()
        if upper == "BEGIN IMMEDIATE":
            return "BEGIN"
        if upper.startswith("CREATE TABLE"):
            s = re.sub(r"\bINTEGER\b", "BIGINT", s, flags=re.I)
            s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
        return s.replace("?", "%s")

    def execute(self, sql, params=()):
        translated = self._translate_sql(sql)
        param_tuple = tuple(params) if params else ()
        cur = self._conn.cursor()
        cur.execute(translated, param_tuple)
        return _PostgresCursorAdapter(cur)

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


def connect_database(db_path, pg_url=None, get_secret=None):
    """Connect to PostgreSQL when configured (failing closed on error), else SQLite WAL."""
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
                    conn_kwargs["prepare_threshold"] = None  # Required for Supavisor port 6543
                raw = pg_mod.connect(url, **conn_kwargs) if conn_kwargs else pg_mod.connect(url)
                return _PostgresConnectionAdapter(raw, driver_name=mod_name)
            except Exception as conn_exc:
                hint = ""
                if re.search(r"@db\.[a-z0-9]+\.supabase\.co\b", url, flags=re.I):
                    hint = (
                        " [PETUNJUK SUPABASE: Host `db.xxxx.supabase.co` adalah Direct Connection IPv6 yang tidak "
                        "didukung jaringan IPv4 Streamlit Cloud. Gunakan Transaction Pooler port 6543 "
                        "(`aws-0-....pooler.supabase.com`)]"
                    )
                raise DatabaseConfigurationError(
                    f"Gagal membuka koneksi PostgreSQL ({mod_name}): {type(conn_exc).__name__}: {conn_exc}{hint}"
                ) from conn_exc
        raise DatabaseConfigurationError(
            "DATABASE_URL PostgreSQL dikonfigurasi, tetapi driver psycopg/psycopg2 tidak terpasang."
        ) from last_import_err

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def atomic_transaction(conn):
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


def verify_postgres_health(pg_url=None, get_secret=None, db_path=None):
    url = configured_database_url(get_secret=get_secret, explicit_url=pg_url)
    if not url:
        return {"ok": False, "error": "DATABASE_URL tidak dikonfigurasi."}
    try:
        with closing(connect_database(db_path or "postgres_health", pg_url=url, get_secret=get_secret)) as conn:
            row = conn.execute("SELECT 1").fetchone()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS simple_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            now_str = str(int(time.time() * 1000))
            conn.execute(
                "INSERT INTO simple_meta(key,value) VALUES('health_probe_ms',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now_str,),
            )
            conn.commit()
            return {"ok": bool(row and int(row[0]) == 1), "error": ""}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def compute_backup_checksum(payload_dict):
    canonical = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class SupabaseStateSync:
    """Verified JSON snapshot backup/restore over Supabase REST (`coinagent_backups` table)."""

    SCHEMA_VERSION = BACKUP_SCHEMA_VERSION

    def __init__(self, supabase_url="", supabase_key="", transport=None, max_retries=2, retry_backoff_seconds=0.05, writer_id="simple-reversal-v7", enforce_single_writer=False):
        self.url = str(supabase_url or "").rstrip("/")
        self.key = str(supabase_key or "").strip()
        self.transport = transport
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.writer_id = str(writer_id or f"writer-{os.getpid()}").strip()
        self.enforce_single_writer = bool(enforce_single_writer)
        self.last_push_ok = False
        self.last_pull_ok = False
        self.last_error = ""

    @property
    def enabled(self):
        return bool(self.url.startswith(("https://", "http://127.0.0.1", "http://localhost")) and self.key)

    def _execute_request(self, req, timeout=8):
        if callable(self.transport):
            return self.transport(req, timeout=timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read()

    def build_envelope(self, namespace, payload_dict):
        data = copy.deepcopy(dict(payload_dict))
        now_ms = int(time.time() * 1000)
        return {
            "namespace": str(namespace),
            "schema_version": self.SCHEMA_VERSION,
            "backup_at_ms": now_ms,
            "state_updated_ms": int(data.get("state_updated_ms") or now_ms),
            "state_seq": 1,
            "writer_id": self.writer_id,
            "checksum_sha256": compute_backup_checksum(data),
            "payload": data,
        }

    def push_backup(self, namespace, payload_dict, **_ignored):
        if not self.enabled:
            return False
        envelope = self.build_envelope(namespace, payload_dict)
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
                    self.last_error = ""
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
        if not self.enabled:
            return None
        encoded_ns = urllib.parse.quote(str(namespace), safe="")
        last_err = ""
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.url}/rest/v1/coinagent_backups?namespace=eq.{encoded_ns}"
                "&select=namespace,schema_version,backup_at_ms,state_updated_ms,checksum_sha256,payload&limit=1",
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
                    payload = row.get("payload")
                    if isinstance(payload, dict) and row.get("checksum_sha256"):
                        if compute_backup_checksum(payload) != row["checksum_sha256"]:
                            self.last_error = "Checksum backup Supabase tidak cocok"
                            return None
                    self.last_pull_ok = True
                    self.last_error = ""
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

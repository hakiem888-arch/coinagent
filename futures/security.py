"""Server-side Authentication, RBAC Authorization, Rate Limiting, Audit Logging,
Database Health Verification, Persistent Cross-Process AI Quotas, and Token Rotation (BUG-10, BUG-13, BUG-15, BUG-16, BUG-24, BUG-25, BUG-26).

Security Principles:
1. Default authentication NEVER grants ADMIN to anonymous sessions (`auth_required = True` by default).
2. Unauthenticated sessions always receive `ROLE_VIEWER` (Read-Only).
3. All privileged actions (`MANUAL_SCAN`, `AUTO_SCAN`, `TELEGRAM_TEST`, `TELEGRAM_ALERT`,
   `JOURNAL_EXPORT`, `JOURNAL_RESTORE`, `CONFIG_UPDATE`, `AI_REVIEW`) require a verified
   server-side `ADMIN` or `WORKER` session token.
4. BUG-25: Enforces both per-session and global hourly AI quotas in the shared database
   using atomic transactions (`security_quota_events`) so worker, dashboard, and process restarts
   share a single consistent counter without race conditions.
5. BUG-26: Computes a one-way cryptographic authentication fingerprint (`compute_auth_config_fingerprint`),
   never stores or logs raw admin tokens, and automatically invalidates all prior sessions
   whenever the administrator token or password hash is rotated.
"""
from contextlib import closing
from pathlib import Path
import hashlib
import hmac
import os
try:
    import resource
except ImportError:  # Windows does not provide the resource module.
    resource = None
import secrets
import threading
import time
from .db_backend import (
    atomic_transaction,
    configured_database_url,
    connect_database,
    verify_database_health,
    verify_postgres_health,
    verify_sqlite_integrity,
)


_PROCESS_SECRET = secrets.token_hex(32)
_SHARED_SESSIONS = {}
_ACTIVE_AUTH_FINGERPRINTS = {}
ROLE_ADMIN = "ADMIN"
ROLE_WORKER = "WORKER"
ROLE_VIEWER = "VIEWER"

PRIVILEGED_ACTIONS = frozenset({
    "MANUAL_SCAN",
    "AUTO_SCAN",
    "TELEGRAM_TEST",
    "TELEGRAM_ALERT",
    "JOURNAL_EXPORT",
    "JOURNAL_RESTORE",
    "CONFIG_UPDATE",
    "AI_REVIEW",
})


class PermissionDeniedError(PermissionError):
    """Raised when a non-administrator attempts to execute a restricted server action."""


class RateLimitExceededError(RuntimeError):
    """Raised when a caller exceeds the configured server-side rate limit."""


class DeploymentSecurityError(RuntimeError):
    """Raised on public deployment startup when administrator authentication is not configured."""


def compute_auth_config_fingerprint(admin_token="", admin_password_hash="", db_url=""):
    """BUG-26: Compute a one-way cryptographic fingerprint of authentication and DB config without exposing raw secrets."""
    raw_tok = str(admin_token or "").strip()
    raw_hash = str(admin_password_hash or "").strip().lower()
    effective_admin_hash = raw_hash or (
        hashlib.sha256(raw_tok.encode("utf-8")).hexdigest() if raw_tok else ""
    )
    db_digest = (
        hashlib.sha256(str(db_url or "").strip().encode("utf-8")).hexdigest()[:16]
        if db_url else "sqlite"
    )
    if not effective_admin_hash:
        return f"unconfigured:{db_digest}"
    fp = hashlib.pbkdf2_hmac(
        "sha256",
        effective_admin_hash.encode("utf-8"),
        b"coinagentx-auth-fp-v602",
        1000,
    ).hex()[:32]
    return f"configured:{fp}:{db_digest}"


class ResourceMonitor:
    """BUG-13 & v6.0.4 Section 11: Track CPU time, RSS memory, and granular API/scan/refresh telemetry counters."""

    def __init__(self, clock=time.monotonic, instrumented=True):
        self.clock = clock
        self.started_at = clock()
        self.lock = threading.Lock()
        self.instrumented = bool(instrumented)
        self.counters = {
            "market_api_requests": 0,
            "market_api_success": 0,
            "market_api_errors": 0,
            "groq_ai_requests": 0,
            "groq_requests": 0,
            "groq_success": 0,
            "groq_errors": 0,
            "telegram_requests": 0,
            "telegram_success": 0,
            "telegram_errors": 0,
            "manual_scans": 0,
            "auto_scans": 0,
            "worker_scans": 0,
            "dashboard_refreshes": 0,
            "journal_exports": 0,
            "journal_restores": 0,
            "rate_limited_events": 0,
            "unauthorized_attempts": 0,
            "audit_log_failures": 0,
        }
        self.active_channels = set()

    def increment(self, key, amount=1):
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + int(amount)
            self.active_channels.add(key)
            if key == "groq_ai_requests":
                self.counters["groq_requests"] = self.counters.get("groq_requests", 0) + int(amount)
                self.active_channels.add("groq_requests")
            elif key == "groq_requests":
                self.counters["groq_ai_requests"] = self.counters.get("groq_ai_requests", 0) + int(amount)
                self.active_channels.add("groq_ai_requests")

    def telemetry_snapshot(self, uninstrumented_keys=()):
        """Section 11: Return telemetry counters, reporting UNKNOWN / NOT_INSTRUMENTED when instrumentation is disabled for a key."""
        snap = self.snapshot()
        if not self.instrumented:
            return {k: "NOT_INSTRUMENTED" for k in self.counters}
        out = dict(snap["counters"])
        for k in uninstrumented_keys:
            out[k] = "NOT_INSTRUMENTED"
        out["uptime_seconds"] = snap["uptime_seconds"]
        out["cpu_process_seconds"] = snap.get("cpu_process_seconds", 0.0)
        out["memory_rss_mb"] = snap["memory_rss_mb"]
        out["instrumentation_status"] = "ACTIVE" if self.instrumented else "NOT_INSTRUMENTED"
        return out

    @staticmethod
    def memory_rss_mb():
        try:
            if resource is None:
                return 0.0
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return round(float(usage) / 1024.0, 2)
        except Exception:
            return 0.0

    def snapshot(self):
        with self.lock:
            counts = dict(self.counters)
        return {
            "uptime_seconds": round(max(0.0, self.clock() - self.started_at), 2),
            "cpu_process_seconds": round(time.process_time(), 4),
            "memory_rss_mb": self.memory_rss_mb(),
            "counters": counts,
        }


GLOBAL_MONITOR = ResourceMonitor()


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter for scans, Telegram, and exports."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.events = {}
        self.last_success = {}

    def check_and_record(self, bucket, max_calls, window_seconds, min_interval_seconds=0.0):
        now = self.clock()
        with self.lock:
            history = [t for t in self.events.get(bucket, []) if now - t < window_seconds]
            self.events[bucket] = history
            last = self.last_success.get(bucket)
            if min_interval_seconds > 0 and last is not None and 0 <= now - last < min_interval_seconds:
                wait_s = round(min_interval_seconds - (now - last), 1)
                return False, f"Rate limit aktif untuk '{bucket}': tunggu {wait_s} detik sebelum mencoba lagi."
            if len(history) >= max_calls:
                return False, f"Batas frekuensi '{bucket}' tercapai (maksimal {max_calls} kali per {int(window_seconds)} detik)."
            history.append(now)
            self.last_success[bucket] = now
            return True, "OK"

    def reset_cooldown(self, bucket):
        with self.lock:
            self.last_success.pop(bucket, None)


class SecurityManager:
    """BUG-10, BUG-13, BUG-15, BUG-16, BUG-25, BUG-26: Server-side Auth, RBAC, Persistent AI Quotas, and Audit Log."""

    def __init__(
        self,
        db_path,
        admin_token=None,
        admin_password_hash=None,
        require_auth=True,
        clock=time.time,
        monotonic=time.monotonic,
        manual_scan_cooldown=15.0,
        max_manual_scans_per_min=5,
        max_ai_calls_per_session=60,
        max_ai_calls_global_per_hour=240,
        max_ai_calls_per_min=10,
        max_telegram_per_min=10,
        max_exports_per_5min=5,
        pg_url=None,
        secret_getter=None,
    ):
        self.db_path = Path(db_path)
        self.secret_getter = secret_getter
        self.pg_url = configured_database_url(get_secret=secret_getter, explicit_url=pg_url)
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        raw_token = str(
            admin_token if admin_token is not None else (
                os.getenv("COINAGENT_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN") or ""
            )
        ).strip()
        raw_hash = str(
            admin_password_hash if admin_password_hash is not None else (
                os.getenv("COINAGENT_ADMIN_PASSWORD_HASH") or os.getenv("ADMIN_PASSWORD_HASH") or ""
            )
        ).strip().lower()
        # BUG-26 #2: Never retain raw_token in memory or logs; store only its SHA-256 digest and one-way fingerprint.
        self._admin_hash = raw_hash or (hashlib.sha256(raw_token.encode("utf-8")).hexdigest() if raw_token else "")
        self.auth_fingerprint = compute_auth_config_fingerprint(raw_token, raw_hash, self.pg_url)
        del raw_token
        # BUG-15: Authentication is ALWAYS required by default (`auth_required = True`).
        self.auth_required = True if require_auth is None else bool(require_auth)
        self._server_secret = hashlib.sha256(
            (_PROCESS_SECRET + "|" + self._admin_hash + "|" + self.auth_fingerprint + "|" + str(self.db_path)).encode("utf-8")
        ).hexdigest()
        self._db_key = str(self.db_path.resolve()) if not self.pg_url else f"pg:{self.pg_url}:{self.db_path.name}"
        self._sessions = _SHARED_SESSIONS.setdefault(self._db_key, {})
        self.limiter = SlidingWindowRateLimiter(clock=monotonic)
        self.monitor = ResourceMonitor(clock=monotonic)
        self.manual_scan_cooldown = float(manual_scan_cooldown)
        self.max_manual_scans_per_min = int(max_manual_scans_per_min)
        self.max_ai_calls_per_session = int(max_ai_calls_per_session)
        self.max_ai_calls_global_per_hour = int(max_ai_calls_global_per_hour)
        self.max_ai_calls_per_min = int(max_ai_calls_per_min)
        self.max_telegram_per_min = int(max_telegram_per_min)
        self.max_exports_per_5min = int(max_exports_per_5min)
        self._session_ai_counts = {}
        self.audit_failure_count = 0
        self.last_audit_error = ""
        self.buffered_audit_failures = []
        self._ensure_tables()
        self._sync_auth_fingerprint_and_invalidate_if_rotated()

    @property
    def admin_configured(self):
        return bool(self._admin_hash)

    def _quota_now_seconds(self):
        if self.monotonic is not time.monotonic and self.clock is time.time:
            return float(self.monotonic())
        return float(self.clock())

    def rotate_admin_credentials(self, new_admin_token=None, new_admin_password_hash=None):
        """BUG-26 #3: Rotate admin credentials dynamically and immediately invalidate all previously issued sessions."""
        raw_tok = str(new_admin_token or "").strip()
        raw_hash = str(new_admin_password_hash or "").strip().lower()
        new_hash = raw_hash or (hashlib.sha256(raw_tok.encode("utf-8")).hexdigest() if raw_tok else "")
        new_fp = compute_auth_config_fingerprint(raw_tok, raw_hash, self.pg_url)
        del raw_tok
        with self.lock:
            self._admin_hash = new_hash
            self.auth_fingerprint = new_fp
            self._server_secret = hashlib.sha256(
                (_PROCESS_SECRET + "|" + self._admin_hash + "|" + self.auth_fingerprint + "|" + str(self.db_path)).encode("utf-8")
            ).hexdigest()
            self._sync_auth_fingerprint_and_invalidate_if_rotated(force_revoke=True)
        return self.auth_fingerprint

    def _sync_auth_fingerprint_and_invalidate_if_rotated(self, force_revoke=False):
        """BUG-26 #3: Detect token/hash rotation across cache rebuilds or restarts and revoke old sessions."""
        with self.lock:
            prev_mem_fp = _ACTIVE_AUTH_FINGERPRINTS.get(self._db_key)
            rotated = bool(force_revoke or (prev_mem_fp and prev_mem_fp != self.auth_fingerprint))
            try:
                with closing(self._connect()) as conn:
                    with atomic_transaction(conn):
                        row = conn.execute("SELECT value FROM security_meta WHERE key='auth_fingerprint'").fetchone()
                        prev_db_fp = row[0] if row else None
                        if prev_db_fp and prev_db_fp != self.auth_fingerprint:
                            rotated = True
                        if rotated:
                            conn.execute("DELETE FROM security_sessions")
                            self._sessions.clear()
                        conn.execute(
                            "INSERT OR REPLACE INTO security_meta VALUES ('auth_fingerprint', ?)",
                            (self.auth_fingerprint,),
                        )
            except Exception:
                if rotated:
                    self._sessions.clear()
            _ACTIVE_AUTH_FINGERPRINTS[self._db_key] = self.auth_fingerprint
            if rotated:
                self.log_audit(
                    "TOKEN_ROTATION",
                    "SESSIONS_REVOKED",
                    role=ROLE_ADMIN,
                    session_id="system",
                    detail="Rotasi token/kredensial admin terdeteksi; seluruh sesi administrator sebelumnya telah dibatalkan.",
                )

    def verify_public_deployment_startup(self, enforce_strict=False):
        """BUG-15: Startup check that fails public deployment if admin authentication is not configured."""
        is_public_env = bool(
            enforce_strict
            or str(os.getenv("COINAGENT_PUBLIC_DEPLOYMENT", "")).lower() in ("1", "true", "yes", "on")
            or str(os.getenv("STREAMLIT_SHARING_MODE", "")).strip()
        )
        if not self.admin_configured:
            msg = (
                "KEAMANAN DEPLOYMENT: COINAGENT_ADMIN_TOKEN atau ADMIN_PASSWORD_HASH belum dikonfigurasi di Secrets. "
                "Semua sesi pengunjung dikunci ke mode VIEWER (Read-Only) dan tindakan yang memakai kredensial server dinonaktifkan."
            )
            self.log_audit("STARTUP_SECURITY_CHECK", "LOCKED_READONLY", role=ROLE_VIEWER, detail=msg)
            if is_public_env and enforce_strict:
                raise DeploymentSecurityError(msg)
            return {"ok": False, "configured": False, "mode": "LOCKED_READONLY", "reason": msg}
        self.log_audit("STARTUP_SECURITY_CHECK", "OK", role=ROLE_ADMIN, detail="Konfigurasi autentikasi admin terverifikasi")
        return {"ok": True, "configured": True, "mode": "RBAC_ACTIVE", "reason": "Autentikasi administrator terkonfigurasi."}

    def _connect(self):
        return connect_database(self.db_path, pg_url=self.pg_url, get_secret=self.secret_getter)

    def _ensure_tables(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS security_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at_ms INTEGER NOT NULL,
                role TEXT NOT NULL,
                session_id TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS security_quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_ts REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_security_quota_bucket_ts ON security_quota_events(bucket, event_ts)")
            conn.execute("CREATE TABLE IF NOT EXISTS security_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("""CREATE TABLE IF NOT EXISTS security_sessions (
                session_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                expires_at REAL NOT NULL,
                auth_fp TEXT NOT NULL,
                token_hash TEXT NOT NULL
            )""")

    def log_audit(self, action, status, role=ROLE_VIEWER, session_id="anonymous", detail="", strict=False):
        """SEC-03: Record security audit event and actively monitor/buffer any write failure (never silently ignored)."""
        at_ms = int(self.clock() * 1000)
        safe_detail = str(detail or "")[:300]
        safe_sid = str(session_id or "anonymous")[:32]
        with self.lock:
            try:
                with closing(self._connect()) as conn, conn:
                    conn.execute(
                        "INSERT INTO security_audit_log(at_ms, role, session_id, action, status, detail) VALUES (?,?,?,?,?,?)",
                        (at_ms, str(role), safe_sid, str(action), str(status), safe_detail),
                    )
            except Exception as exc:
                self.audit_failure_count += 1
                self.last_audit_error = f"{type(exc).__name__}: {exc}"
                self.monitor.increment("audit_log_failures")
                GLOBAL_MONITOR.increment("audit_log_failures")
                if len(self.buffered_audit_failures) < 100:
                    self.buffered_audit_failures.append({
                        "at_ms": at_ms,
                        "role": str(role),
                        "session_id": safe_sid,
                        "action": str(action),
                        "status": str(status),
                        "detail": safe_detail,
                        "error": self.last_audit_error,
                    })
                if strict:
                    raise

    def audit_health_status(self):
        """SEC-03: Report audit subsystem health and any failed/buffered audit writes."""
        with self.lock:
            return {
                "healthy": self.audit_failure_count == 0,
                "failure_count": self.audit_failure_count,
                "last_error": self.last_audit_error,
                "buffered_count": len(self.buffered_audit_failures),
            }

    def audit_entries(self, limit=100):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, at_ms, role, session_id, action, status, detail FROM security_audit_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            dict(zip(["id", "at_ms", "role", "session_id", "action", "status", "detail"], r))
            for r in rows
        ]

    def _issue_signed_token(self, role, ttl_seconds=3600, prefix=""):
        session_id = (prefix + secrets.token_hex(10))[:24]
        expires_at = self.clock() + float(ttl_seconds)
        fp_tag = hashlib.sha256(self.auth_fingerprint.encode('utf-8')).hexdigest()[:16]
        payload = f"{session_id}:{role}:{int(expires_at)}:{fp_tag}"
        sig = hmac.new(self._server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token = f"{payload}:{sig}"
        # SEC-03: Store ONLY SHA-256 hash of session token (`token_hash`), never the raw bearer token!
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.lock:
            self._sessions[session_id] = {
                "role": role,
                "expires_at": expires_at,
                "auth_fp": self.auth_fingerprint,
                "token_hash": token_hash,
            }
            try:
                with closing(self._connect()) as conn:
                    with atomic_transaction(conn):
                        conn.execute(
                            "INSERT OR REPLACE INTO security_sessions VALUES (?,?,?,?,?)",
                            (session_id, role, float(expires_at), self.auth_fingerprint, token_hash),
                        )
            except Exception:
                pass
        return token, session_id

    def issue_worker_session(self, ttl_seconds=86400 * 30):
        """BUG-16 #4 & BUG-18: Issue a signed internal session token for a trusted background worker process."""
        token, sid = self._issue_signed_token(ROLE_WORKER, ttl_seconds=ttl_seconds, prefix="wrk-")
        self.log_audit("WORKER_INIT", "ALLOWED", role=ROLE_WORKER, session_id=sid, detail="Sesi worker terpercaya diinisialisasi")
        return token

    def authenticate(self, credential, ttl_seconds=3600):
        """Verify administrator credential on the server side using constant-time digest comparison."""
        candidate = str(credential or "").strip()
        if not candidate or not self._admin_hash:
            self.monitor.increment("unauthorized_attempts")
            self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Kredensial kosong atau token admin belum dikonfigurasi")
            return None
        cand_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        matched = hmac.compare_digest(cand_hash, self._admin_hash) or (
            len(candidate) == 64 and hmac.compare_digest(candidate.lower(), self._admin_hash)
        )
        del candidate
        if not matched:
            self.monitor.increment("unauthorized_attempts")
            self.log_audit("ADMIN_LOGIN", "DENIED", role=ROLE_VIEWER, detail="Token administrator tidak valid")
            return None
        token, session_id = self._issue_signed_token(ROLE_ADMIN, ttl_seconds=ttl_seconds, prefix="adm-")
        self.log_audit("ADMIN_LOGIN", "ALLOWED", role=ROLE_ADMIN, session_id=session_id, detail="Login administrator berhasil")
        return token

    def revoke_session(self, session_token):
        info = self.verify_session(session_token)
        if info["session_id"] != "anonymous":
            with self.lock:
                self._sessions.pop(info["session_id"], None)
                try:
                    with closing(self._connect()) as conn:
                        with atomic_transaction(conn):
                            conn.execute("DELETE FROM security_sessions WHERE session_id=?", (info["session_id"],))
                except Exception:
                    pass
            self.log_audit("ADMIN_LOGOUT", "ALLOWED", role=info["role"], session_id=info["session_id"], detail="Logout administrator")

    def verify_session(self, session_token=None):
        """BUG-15 & BUG-26: Return server-verified role ('ADMIN', 'WORKER', or 'VIEWER'). Rejects rotated/expired tokens."""
        if not session_token:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        parts = str(session_token).split(":")
        if len(parts) == 5:
            session_id, role, exp_str, fp_tag, sig = parts
            if fp_tag != hashlib.sha256(self.auth_fingerprint.encode('utf-8')).hexdigest()[:16]:
                return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            payload = f"{session_id}:{role}:{exp_str}:{fp_tag}"
        elif len(parts) == 4:
            session_id, role, exp_str, sig = parts
            payload = f"{session_id}:{role}:{exp_str}"
        else:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}

        expected_sig = hmac.new(self._server_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
        try:
            expires_at = float(exp_str)
        except ValueError:
            return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}

        now = self.clock()
        with self.lock:
            stored = self._sessions.get(session_id)
            if stored is None:
                try:
                    with closing(self._connect()) as conn:
                        row = conn.execute(
                            "SELECT role, expires_at, auth_fp, token_hash FROM security_sessions WHERE session_id=?",
                            (session_id,),
                        ).fetchone()
                        if row:
                            stored = {
                                "role": row[0],
                                "expires_at": float(row[1]),
                                "auth_fp": row[2],
                                "token_hash": row[3],
                            }
                            self._sessions[session_id] = stored
                except Exception:
                    stored = None
            cand_token_hash = hashlib.sha256(str(session_token).encode("utf-8")).hexdigest()
            if (
                not stored
                or stored.get("auth_fp", self.auth_fingerprint) != self.auth_fingerprint
                or (stored.get("token_hash") and not hmac.compare_digest(cand_token_hash, str(stored["token_hash"])))
                or now >= min(expires_at, float(stored["expires_at"]))
            ):
                self._sessions.pop(session_id, None)
                return {"role": ROLE_VIEWER, "session_id": "anonymous", "authenticated": False}
            return {"role": stored["role"], "session_id": session_id, "authenticated": True}

    def _check_and_record_ai_quota(self, session_id):
        """BUG-25: Persistent cross-process & restart-safe AI quota enforcement using atomic DB transactions."""
        now_ts = self._quota_now_seconds()
        sid = str(session_id or "anonymous")
        with self.lock:
            with closing(self._connect()) as conn:
                with atomic_transaction(conn):
                    # 1. Per-session quota (persistent across restarts + in-memory mirror)
                    row_sess = conn.execute(
                        "SELECT COUNT(*) FROM security_quota_events WHERE bucket='ai_review' AND session_id=?",
                        (sid,),
                    ).fetchone()
                    db_sess_used = int(row_sess[0] if row_sess else 0)
                    used_sess = max(db_sess_used, self._session_ai_counts.get(sid, 0))
                    if used_sess >= self.max_ai_calls_per_session:
                        return False, f"Kuota AI per sesi tercapai ({self.max_ai_calls_per_session} panggilan)."

                    # 2. Global hourly quota (persistent across worker, dashboard, and process restarts)
                    cutoff_hour = now_ts - 3600.0
                    row_hour = conn.execute(
                        "SELECT COUNT(*) FROM security_quota_events WHERE bucket='ai_review' AND event_ts > ?",
                        (cutoff_hour,),
                    ).fetchone()
                    used_hour = int(row_hour[0] if row_hour else 0)
                    if used_hour >= self.max_ai_calls_global_per_hour:
                        return False, (
                            f"Batas frekuensi 'ai_review_global_hour' tercapai "
                            f"(maksimal {self.max_ai_calls_global_per_hour} kali per 3600 detik)."
                        )

                    # 3. Global per-minute burst limit
                    cutoff_min = now_ts - 60.0
                    row_min = conn.execute(
                        "SELECT COUNT(*) FROM security_quota_events WHERE bucket='ai_review' AND event_ts > ?",
                        (cutoff_min,),
                    ).fetchone()
                    used_min = int(row_min[0] if row_min else 0)
                    if used_min >= self.max_ai_calls_per_min:
                        return False, (
                            f"Batas frekuensi 'ai_review' tercapai "
                            f"(maksimal {self.max_ai_calls_per_min} kali per 60 detik)."
                        )

                    # Prune expired events older than 24 hours and record this call atomically
                    conn.execute(
                        "DELETE FROM security_quota_events WHERE event_ts <= ?",
                        (now_ts - 86400.0,),
                    )
                    conn.execute(
                        "INSERT INTO security_quota_events(bucket, session_id, event_ts) VALUES (?,?,?)",
                        ("ai_review", sid, float(now_ts)),
                    )
                    self._session_ai_counts[sid] = used_sess + 1
        return True, "OK"

    def ai_quota_usage(self, session_id=None):
        """Return current persistent global hourly and per-session AI quota usage from the shared database."""
        now_ts = self._quota_now_seconds()
        with closing(self._connect()) as conn:
            row_hour = conn.execute(
                "SELECT COUNT(*) FROM security_quota_events WHERE bucket='ai_review' AND event_ts > ?",
                (now_ts - 3600.0,),
            ).fetchone()
            global_hour_used = int(row_hour[0] if row_hour else 0)
            sess_used = 0
            if session_id:
                row_sess = conn.execute(
                    "SELECT COUNT(*) FROM security_quota_events WHERE bucket='ai_review' AND session_id=?",
                    (str(session_id),),
                ).fetchone()
                sess_used = int(row_sess[0] if row_sess else 0)
        return {
            "global_hour_used": global_hour_used,
            "global_hour_limit": self.max_ai_calls_global_per_hour,
            "session_used": sess_used,
            "session_limit": self.max_ai_calls_per_session,
        }

    def authorize_action(self, action, session_token=None, last_scan_failed=False):
        """BUG-10, BUG-15, BUG-16, BUG-25: Server-side authorization + persistent quota/rate-limit gate."""
        info = self.verify_session(session_token)
        role, sid = info["role"], info["session_id"]
        if action in PRIVILEGED_ACTIONS and role not in (ROLE_ADMIN, ROLE_WORKER):
            self.monitor.increment("unauthorized_attempts")
            msg = f"Akses ditolak (UNAUTHORIZED): tindakan '{action}' memerlukan hak akses ADMINISTRATOR atau WORKER terpercaya."
            self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
            return False, msg

        if action == "MANUAL_SCAN":
            cooldown = 0.0 if (last_scan_failed or role == ROLE_WORKER) else self.manual_scan_cooldown
            ok, reason = self.limiter.check_and_record(
                "manual_scan", self.max_manual_scans_per_min, 60.0, min_interval_seconds=cooldown
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("manual_scans")

        elif action == "AUTO_SCAN":
            self.monitor.increment("auto_scans")

        elif action == "TELEGRAM_TEST":
            if role != ROLE_ADMIN:
                self.monitor.increment("unauthorized_attempts")
                msg = "Akses ditolak (UNAUTHORIZED): TELEGRAM_TEST hanya untuk ADMINISTRATOR."
                self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
                return False, msg
            ok, reason = self.limiter.check_and_record(
                "telegram_test", 3, 600.0, min_interval_seconds=30.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("telegram_requests")

        elif action == "TELEGRAM_ALERT":
            ok, reason = self.limiter.check_and_record(
                "telegram_alert", self.max_telegram_per_min, 60.0, min_interval_seconds=0.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("telegram_requests")

        elif action in ("JOURNAL_EXPORT", "JOURNAL_RESTORE"):
            if role != ROLE_ADMIN:
                self.monitor.increment("unauthorized_attempts")
                msg = f"Akses ditolak (UNAUTHORIZED): {action} hanya untuk ADMINISTRATOR."
                self.log_audit(action, "DENIED", role=role, session_id=sid, detail=msg)
                return False, msg
            ok, reason = self.limiter.check_and_record(
                action.lower(), self.max_exports_per_5min, 300.0, min_interval_seconds=0.0
            )
            if not ok:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason)
                return False, reason
            self.monitor.increment("journal_exports" if action == "JOURNAL_EXPORT" else "journal_restores")

        elif action == "AI_REVIEW":
            ok_ai, reason_ai = self._check_and_record_ai_quota(sid)
            if not ok_ai:
                self.monitor.increment("rate_limited_events")
                self.log_audit(action, "RATE_LIMITED", role=role, session_id=sid, detail=reason_ai)
                return False, reason_ai
            self.monitor.increment("groq_ai_requests")

        self.log_audit(action, "ALLOWED", role=role, session_id=sid, detail=f"Tindakan {action} diizinkan")
        return True, "ALLOWED"

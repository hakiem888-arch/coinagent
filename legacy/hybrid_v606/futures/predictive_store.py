"""Transactional Database Repository for Predictive Watchlist Signals, Events, and Alert Outbox (v6.0.5 Stage 3).

Shares the exact same persistence backend (`connect_database` -> PostgreSQL or SQLite WAL)
between the independent Worker and the Streamlit Dashboard.
- Never silently falls back to SQLite when PostgreSQL is configured (`fail-closed`).
- Uses atomic transactions for signal state transitions + event history + deduplicated alert outbox.
- Preserves active signals across worker restarts without duplicate alerts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .db_backend import (
    connect_database,
    configured_database_url,
    database_identity_fingerprint,
)
from .predictive_watchlist import (
    ACTIVE_STAGES,
    LifecycleTransitionResult,
    PredictiveSignal,
)

MIGRATION_VERSION = "002_predictive_watchlist_v605"


class PredictiveStoreError(RuntimeError):
    """Raised on mandatory persistence failure (fail-closed)."""


class PredictiveSignalRepository:
    def __init__(self, db_path: Path | str, *, pg_url: Optional[str] = None, secret_getter=None, strict_pg: Optional[bool] = None):
        self.db = Path(db_path)
        self.secret_getter = secret_getter
        self.pg_url = pg_url if pg_url is not None else configured_database_url(get_secret=secret_getter)
        self.strict_pg = strict_pg
        self._init_schema()

    def _connect(self):
        return connect_database(
            self.db,
            pg_url=self.pg_url,
            get_secret=self.secret_getter,
            require_postgres=self.strict_pg,
        )

    def database_identity(self) -> dict[str, Any]:
        return {
            "backend": "POSTGRESQL" if self.pg_url else "SQLITE_WAL",
            "database_name": self.db.name if not self.pg_url else "postgresql",
            "database_fingerprint": database_identity_fingerprint(self.db, pg_url=self.pg_url),
            "migration_version": MIGRATION_VERSION,
        }

    def _init_schema(self) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at_ms BIGINT NOT NULL,
                        description TEXT NOT NULL DEFAULT ''
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS predictive_signals (
                        signal_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        signal_state TEXT NOT NULL DEFAULT 'ACTIVE',
                        early_score REAL NOT NULL DEFAULT 0.0,
                        setup_quality_score REAL,
                        confirmation_score REAL,
                        detected_price REAL NOT NULL,
                        trigger_price REAL NOT NULL,
                        invalidation_price REAL NOT NULL,
                        current_price REAL NOT NULL,
                        distance_to_trigger_pct REAL NOT NULL DEFAULT 0.0,
                        provider TEXT NOT NULL,
                        instrument_kind TEXT NOT NULL DEFAULT 'USD_M_PERPETUAL',
                        m15_regime TEXT NOT NULL DEFAULT 'UNKNOWN',
                        provisional_context TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL DEFAULT '',
                        confidence_label TEXT NOT NULL DEFAULT 'PROVISIONAL_SETUP_QUALITY',
                        reason_codes TEXT NOT NULL DEFAULT '[]',
                        detected_at_ms BIGINT NOT NULL,
                        updated_at_ms BIGINT NOT NULL,
                        expires_at_ms BIGINT NOT NULL,
                        alert_state TEXT NOT NULL DEFAULT 'PENDING',
                        dedup_key TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL DEFAULT '{}'
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_predictive_signals_symbol_state ON predictive_signals (symbol, signal_state, updated_at_ms)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_predictive_signals_stage_updated ON predictive_signals (stage, updated_at_ms)"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS predictive_signal_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        signal_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        from_stage TEXT NOT NULL DEFAULT '',
                        to_stage TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        early_score REAL NOT NULL DEFAULT 0.0,
                        setup_quality_score REAL,
                        price REAL NOT NULL DEFAULT 0.0,
                        provider TEXT NOT NULL DEFAULT '',
                        at_ms BIGINT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL DEFAULT '{}'
                    )""" if not self.pg_url else """CREATE TABLE IF NOT EXISTS predictive_signal_events (
                        id BIGSERIAL PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        signal_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        from_stage TEXT NOT NULL DEFAULT '',
                        to_stage TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        early_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        setup_quality_score DOUBLE PRECISION,
                        price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        provider TEXT NOT NULL DEFAULT '',
                        at_ms BIGINT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL DEFAULT '{}'
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_predictive_signal_events_signal_id ON predictive_signal_events (signal_id, at_ms)"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS predictive_alert_outbox (
                        outbox_id TEXT PRIMARY KEY,
                        dedup_key TEXT NOT NULL UNIQUE,
                        signal_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        delivery_state TEXT NOT NULL DEFAULT 'PENDING',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at_ms BIGINT NOT NULL DEFAULT 0,
                        created_at_ms BIGINT NOT NULL,
                        sent_at_ms BIGINT,
                        last_error TEXT NOT NULL DEFAULT '',
                        message_text TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_predictive_alert_outbox_state_retry ON predictive_alert_outbox (delivery_state, next_retry_at_ms)"
                )
                now_ms = int(time.time() * 1000)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at_ms, description) VALUES (?, ?, ?) ON CONFLICT (version) DO NOTHING",
                    (MIGRATION_VERSION, now_ms, "Predictive Watchlist v1.0 tables"),
                )
                conn.commit()
        except Exception as exc:
            raise PredictiveStoreError(f"Inisialisasi tabel Predictive Watchlist gagal: {exc}") from exc

    def health_check(self) -> dict[str, Any]:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT COUNT(*) FROM predictive_signals").fetchone()
                out_row = conn.execute("SELECT COUNT(*) FROM predictive_alert_outbox WHERE delivery_state='PENDING'").fetchone()
                mig = conn.execute("SELECT version FROM schema_migrations WHERE version=?", (MIGRATION_VERSION,)).fetchone()
            return {
                "ok": True,
                "signal_count": int(row[0]) if row else 0,
                "pending_outbox": int(out_row[0]) if out_row else 0,
                "migration_version": mig[0] if mig else MIGRATION_VERSION,
                **self.database_identity(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc)[:200],
                **self.database_identity(),
            }

    @staticmethod
    def alert_dedup_key(signal: PredictiveSignal, event_type: str, bucket_ms: int = 0) -> str:
        """Deterministic deduplication key so restarts or repeated scans never spam identical stage alerts."""
        ev = str(event_type or signal.stage).upper()
        base = f"{signal.signal_id}:{signal.symbol}:{signal.direction}:{ev}"
        if bucket_ms > 0:
            base += f":{int(bucket_ms)}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]

    def _upsert_signal_in_tx(self, conn, sig: PredictiveSignal) -> None:
        payload_json = json.dumps(sig.to_dict(), ensure_ascii=False, default=str)
        reasons_json = json.dumps(sig.reason_codes, ensure_ascii=False)
        conn.execute(
            """INSERT INTO predictive_signals (
                signal_id, symbol, direction, stage, signal_state,
                early_score, setup_quality_score, confirmation_score,
                detected_price, trigger_price, invalidation_price, current_price,
                distance_to_trigger_pct, provider, instrument_kind,
                m15_regime, provisional_context, strategy_id, confidence_label,
                reason_codes, detected_at_ms, updated_at_ms, expires_at_ms,
                alert_state, dedup_key, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (signal_id) DO UPDATE SET
                stage = excluded.stage,
                signal_state = excluded.signal_state,
                early_score = excluded.early_score,
                setup_quality_score = excluded.setup_quality_score,
                confirmation_score = excluded.confirmation_score,
                trigger_price = excluded.trigger_price,
                invalidation_price = excluded.invalidation_price,
                current_price = excluded.current_price,
                distance_to_trigger_pct = excluded.distance_to_trigger_pct,
                provider = excluded.provider,
                instrument_kind = excluded.instrument_kind,
                m15_regime = excluded.m15_regime,
                provisional_context = excluded.provisional_context,
                strategy_id = excluded.strategy_id,
                confidence_label = excluded.confidence_label,
                reason_codes = excluded.reason_codes,
                updated_at_ms = excluded.updated_at_ms,
                expires_at_ms = excluded.expires_at_ms,
                alert_state = excluded.alert_state,
                payload = excluded.payload
            """,
            (
                sig.signal_id,
                sig.symbol,
                sig.direction,
                sig.stage,
                sig.signal_state,
                float(sig.early_score),
                sig.setup_quality_score,
                sig.confirmation_score,
                float(sig.detected_price),
                float(sig.trigger_price),
                float(sig.invalidation_price),
                float(sig.current_price),
                float(sig.distance_to_trigger_pct),
                sig.provider,
                sig.instrument_kind,
                sig.m15_regime,
                sig.provisional_context,
                sig.strategy_id,
                sig.confidence_label,
                reasons_json,
                int(sig.detected_at_ms),
                int(sig.updated_at_ms),
                int(sig.expires_at_ms),
                sig.alert_state,
                sig.dedup_key,
                payload_json,
            ),
        )

    def save_transition(
        self,
        transition: LifecycleTransitionResult,
        *,
        alert_message: Optional[str] = None,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Atomically persist signal state, replaced signal (if direction changed), event history, and alert outbox."""
        stamp = int(now_ms if now_ms is not None else time.time() * 1000)
        outbox_queued = False
        try:
            with closing(self._connect()) as conn:
                if transition.replaced_signal is not None:
                    self._upsert_signal_in_tx(conn, transition.replaced_signal)
                self._upsert_signal_in_tx(conn, transition.signal)

                for evt in transition.events:
                    conn.execute(
                        """INSERT INTO predictive_signal_events (
                            event_id, signal_id, symbol, direction, from_stage, to_stage,
                            event_type, early_score, setup_quality_score, price, provider,
                            at_ms, reason, payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            evt["event_id"],
                            evt["signal_id"],
                            evt["symbol"],
                            evt["direction"],
                            evt.get("from_stage", ""),
                            evt["to_stage"],
                            evt["event_type"],
                            float(evt.get("early_score", 0.0)),
                            evt.get("setup_quality_score"),
                            float(evt.get("price", 0.0)),
                            evt.get("provider", ""),
                            int(evt.get("at_ms", stamp)),
                            evt.get("reason", ""),
                            json.dumps(evt, ensure_ascii=False, default=str),
                        ),
                    )

                if transition.should_alert and alert_message:
                    ev_type = str(transition.alert_event_type or transition.signal.stage)
                    dkey = self.alert_dedup_key(transition.signal, ev_type)
                    exists = conn.execute(
                        "SELECT 1 FROM predictive_alert_outbox WHERE dedup_key=?",
                        (dkey,),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            """INSERT INTO predictive_alert_outbox (
                                outbox_id, dedup_key, signal_id, symbol, direction, stage,
                                event_type, delivery_state, attempts, next_retry_at_ms,
                                created_at_ms, sent_at_ms, last_error, message_text
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, 0, ?, NULL, '', ?)
                            ON CONFLICT (dedup_key) DO NOTHING
                            """,
                            (
                                f"out-{uuid4().hex[:12]}",
                                dkey,
                                transition.signal.signal_id,
                                transition.signal.symbol,
                                transition.signal.direction,
                                transition.signal.stage,
                                ev_type,
                                stamp,
                                alert_message,
                            ),
                        )
                        outbox_queued = True
                conn.commit()
            return {"ok": True, "outbox_queued": outbox_queued, "signal_id": transition.signal.signal_id}
        except Exception as exc:
            raise PredictiveStoreError(f"Gagal menyimpan transisi sinyal prediktif: {exc}") from exc

    def get_active_signal(self, symbol: str) -> Optional[PredictiveSignal]:
        sym = str(symbol or "").strip().upper()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM predictive_signals WHERE symbol=? AND signal_state='ACTIVE' ORDER BY updated_at_ms DESC LIMIT 1",
                (sym,),
            ).fetchone()
        if not row:
            return None
        return PredictiveSignal.from_dict(json.loads(row[0]))

    def list_signals(
        self,
        *,
        stage_filter: Optional[str] = None,
        direction_filter: Optional[str] = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> list[PredictiveSignal]:
        clauses = []
        params: list[Any] = []
        if active_only:
            clauses.append("signal_state = 'ACTIVE'")
        if stage_filter and stage_filter.upper() != "ALL":
            clauses.append("stage = ?")
            params.append(stage_filter.upper())
        if direction_filter and direction_filter.upper() in ("LONG", "SHORT"):
            clauses.append("direction = ?")
            params.append(direction_filter.upper())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT payload FROM predictive_signals {where} ORDER BY updated_at_ms DESC LIMIT ?"
        params.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [PredictiveSignal.from_dict(json.loads(r[0])) for r in rows]

    def list_events(self, signal_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            if signal_id:
                rows = conn.execute(
                    "SELECT payload FROM predictive_signal_events WHERE signal_id=? ORDER BY at_ms DESC LIMIT ?",
                    (signal_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM predictive_signal_events ORDER BY at_ms DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def pending_outbox(self, now_ms: Optional[int] = None, limit: int = 20) -> list[dict[str, Any]]:
        stamp = int(now_ms if now_ms is not None else time.time() * 1000)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT outbox_id, dedup_key, signal_id, symbol, direction, stage, event_type,
                          delivery_state, attempts, next_retry_at_ms, created_at_ms, message_text
                   FROM predictive_alert_outbox
                   WHERE delivery_state IN ('PENDING', 'RETRY') AND next_retry_at_ms <= ?
                   ORDER BY created_at_ms ASC LIMIT ?""",
                (stamp, int(limit)),
            ).fetchall()
        cols = (
            "outbox_id", "dedup_key", "signal_id", "symbol", "direction", "stage", "event_type",
            "delivery_state", "attempts", "next_retry_at_ms", "created_at_ms", "message_text",
        )
        return [dict(zip(cols, r)) for r in rows]

    def mark_outbox_sent(self, outbox_id: str, now_ms: Optional[int] = None) -> None:
        stamp = int(now_ms if now_ms is not None else time.time() * 1000)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE predictive_alert_outbox SET delivery_state='SENT', sent_at_ms=?, last_error='' WHERE outbox_id=?",
                (stamp, outbox_id),
            )
            conn.commit()

    def mark_outbox_retry(self, outbox_id: str, error: str, *, now_ms: Optional[int] = None, max_attempts: int = 5) -> None:
        stamp = int(now_ms if now_ms is not None else time.time() * 1000)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT attempts FROM predictive_alert_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            backoff_ms = min(300_000, (2 ** min(attempts, 6)) * 5000)
            state = "FAILED" if attempts >= max_attempts else "RETRY"
            conn.execute(
                "UPDATE predictive_alert_outbox SET delivery_state=?, attempts=?, next_retry_at_ms=?, last_error=? WHERE outbox_id=?",
                (state, attempts, stamp + backoff_ms, str(error)[:200], outbox_id),
            )
            conn.commit()

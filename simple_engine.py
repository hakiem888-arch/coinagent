"""CoinAgentX Simple Reversal Engine.

Production signal logic uses ONLY Bollinger Bands on closed M15 candles and
RSI(14) plus a two-candle reversal confirmation on closed M5 candles.
Paper-trade risk management supports Step-Based R-Multiple Trailing Stop (Option 1).
The application is paper-only and never submits exchange orders.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import math
import os
import time
import uuid

import numpy as np
import pandas as pd
import requests

from futures.db_backend import (
    SupabaseStateSync,
    atomic_transaction,
    configured_database_url,
    configured_supabase_rest,
    connect_database,
    verify_postgres_health,
)
from futures.market import MarketError
from futures.provider_router import FuturesProviderRouter

APP_VERSION = "7.0.5-simple"
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

# Option 1: Step-Based R-Multiple Trailing Stop ladder:
# (trigger_r, lock_r)
TRAILING_STEP_LADDER = (
    (1.0, 0.05),   # Profit >= +1.0R -> geser SL ke Break-Even (+0.05R buffer fee)
    (1.5, 0.50),   # Profit >= +1.5R -> kunci +0.50R
    (2.0, 1.20),   # Profit >= +2.0R -> kunci +1.20R (aktif bila TP > 2R atau Open)
)


@dataclass(frozen=True)
class ScanSettings:
    symbols: int = 50
    interval_seconds: int = 60
    initial_equity: float = 1000.0
    risk_pct: float = 0.5
    leverage: int = 2
    send_telegram: bool = True
    trailing_stop_enabled: bool = True
    tp_multiple: float = 2.0  # 2.0 = 2R default, 3.0 = 3R, 4.0 = 4R, 0.0 = Open (Trailing Only)


def bollinger_bands(close: pd.Series, period: int = BB_PERIOD, deviations: float = BB_STD) -> pd.DataFrame:
    """SMA + population standard deviation; standard BB(20, 2)."""
    values = pd.to_numeric(close, errors="coerce")
    mid = values.rolling(period, min_periods=period).mean()
    std = values.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"middle": mid, "upper": mid + deviations * std, "lower": mid - deviations * std})


def rsi_wilder(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI(14); no other momentum indicators are calculated."""
    values = pd.to_numeric(close, errors="coerce")
    delta = values.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - 100.0 / (1.0 + rs)
    result = result.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    result = result.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    result = result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return result


def initial_risk_distance(trade: dict) -> float:
    """Recover original 1R price distance cleanly from risk_usdt / quantity (schema-compatible)."""
    qty = float(trade.get("quantity") or 0.0)
    risk_usdt = float(trade.get("risk_usdt") or 0.0)
    if qty > 0 and risk_usdt > 0:
        dist = risk_usdt / qty
        if math.isfinite(dist) and dist > 0:
            return dist
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    return abs(entry - stop)


def compute_step_trailing_lock_r(favorable_r: float) -> float | None:
    """Return locked R-multiple for Option 1 (Step-Based R-Multiple Trailing Stop), or None if < 1.0R."""
    if not math.isfinite(favorable_r) or favorable_r < 1.0:
        return None
    locked = None
    for trigger_r, lock_r in TRAILING_STEP_LADDER:
        if favorable_r >= trigger_r:
            locked = lock_r
    # Extend dynamically every +0.5R above +2.0R (e.g. +2.5R -> +1.70R, +3.0R -> +2.20R)
    if favorable_r >= 2.5:
        extra_steps = int(math.floor((favorable_r - 2.0) / 0.5))
        dynamic_lock = round(1.20 + extra_steps * 0.50, 4)
        if locked is None or dynamic_lock > locked:
            locked = dynamic_lock
    return locked


def describe_trailing_state(trade: dict) -> dict:
    """Compute human-readable trailing stop state and R-multiple metrics for UI/reporting."""
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    mark = float(trade["mark_price"])
    sign = 1 if trade["direction"] == "LONG" else -1
    r_dist = initial_risk_distance(trade)
    if r_dist <= 0:
        return {
            "initial_stop": stop,
            "current_r": 0.0,
            "locked_r": -1.0,
            "trail_stage": "SL Awal",
        }
    initial_stop = entry - sign * r_dist
    current_r = (mark - entry) * sign / r_dist
    locked_r = (stop - entry) * sign / r_dist
    if locked_r >= 0.45:
        stage = f"🔒 Kunci +{locked_r:.2f}R"
    elif locked_r >= 0.01:
        stage = f"🛡️ BE (+{locked_r:.2f}R)"
    elif locked_r >= -0.01:
        stage = "🛡️ Break-Even"
    else:
        stage = "SL Awal (-1.00R)"
    return {
        "initial_stop": initial_stop,
        "current_r": current_r,
        "locked_r": locked_r,
        "trail_stage": stage,
    }


def evaluate_reversal(m15: pd.DataFrame, m5: pd.DataFrame, symbol: str = "", tp_multiple: float = 2.0) -> dict | None:
    """Evaluate a single reversal setup using closed M15/M5 candles only.

    M15: one of the two latest closed candles must touch an outer Bollinger band.
    M5: the latest closed candle must confirm direction against the prior candle.
    RSI: the prior M5 RSI must be in the corresponding extreme and the latest
    RSI must turn back in the trade direction.
    """
    if m15 is None or m5 is None or len(m15) < BB_PERIOD or len(m5) < RSI_PERIOD + 2:
        return None
    required = {"open_time", "close_time", "open", "high", "low", "close"}
    if not required.issubset(m15.columns) or not required.issubset(m5.columns):
        return None

    bb = bollinger_bands(m15["close"])
    m15_work = m15.reset_index(drop=True)
    m5_work = m5.reset_index(drop=True)
    bb = bb.reset_index(drop=True)
    touch = None
    # Accept a touch in either of the two latest closed M15 candles, but the
    # M5 confirmation must close after the touched M15 candle.
    for idx in range(len(m15_work) - 1, max(-1, len(m15_work) - 3), -1):
        row, bands = m15_work.iloc[idx], bb.iloc[idx]
        if not (math.isfinite(float(bands.lower)) and math.isfinite(float(bands.upper))):
            continue
        hit_lower = float(row.low) <= float(bands.lower)
        hit_upper = float(row.high) >= float(bands.upper)
        if hit_lower and hit_upper:
            return None  # ambiguous candle touched both sides
        if hit_lower:
            touch = {"direction": "LONG", "band": "LOWER", "band_value": float(bands.lower),
                     "close_ms": int(row.close_time)}
            break
        if hit_upper:
            touch = {"direction": "SHORT", "band": "UPPER", "band_value": float(bands.upper),
                     "close_ms": int(row.close_time)}
            break
    if touch is None:
        return None

    current, previous = m5_work.iloc[-1], m5_work.iloc[-2]
    if int(current.close_time) <= touch["close_ms"]:
        return None
    rsi = rsi_wilder(m5_work["close"])
    rsi_now, rsi_prev = float(rsi.iloc[-1]), float(rsi.iloc[-2])
    if not (math.isfinite(rsi_now) and math.isfinite(rsi_prev)):
        return None

    tp_mult = max(0.0, float(tp_multiple))
    direction = touch["direction"]
    if direction == "LONG":
        candle_ok = (
            float(current.high) > float(previous.high)
            and float(current.close) > float(previous.close)
            and float(current.close) > float(current.open)
        )
        rsi_ok = rsi_prev <= RSI_OVERSOLD and rsi_now > rsi_prev
        stop = min(float(previous.low), float(current.low))
        risk = float(current.close) - stop
        target = (float(current.close) + tp_mult * risk) if tp_mult > 0 else 0.0
    else:
        candle_ok = (
            float(current.low) < float(previous.low)
            and float(current.close) < float(previous.close)
            and float(current.close) < float(current.open)
        )
        rsi_ok = rsi_prev >= RSI_OVERBOUGHT and rsi_now < rsi_prev
        stop = max(float(previous.high), float(current.high))
        risk = stop - float(current.close)
        target = (float(current.close) - tp_mult * risk) if tp_mult > 0 else 0.0

    if not candle_ok or not rsi_ok or risk <= 0:
        return None
    if tp_mult > 0 and target <= 0:
        return None
    return {
        "symbol": str(symbol).upper(),
        "direction": direction,
        "m15_touch_band": touch["band"],
        "m15_touch_close_ms": touch["close_ms"],
        "m15_band_value": touch["band_value"],
        "m5_close_ms": int(current.close_time),
        "entry": float(current.close),
        "stop": float(stop),
        "target": float(target),
        "tp_multiple": tp_mult,
        "rsi_previous": rsi_prev,
        "rsi_current": rsi_now,
        "previous_high": float(previous.high),
        "current_high": float(current.high),
        "previous_low": float(previous.low),
        "current_low": float(current.low),
        "previous_close": float(previous.close),
        "current_close": float(current.close),
    }


class SimpleStore:
    """Durable store:
    1. Primary: Supabase / PostgreSQL via DATABASE_URL / SUPABASE_DB_URL / POSTGRES_URL.
    2. Fallback: Supabase REST snapshot sync (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY)
       into table `coinagent_backups` (namespace `SIMPLE_REVERSAL_V7`) so paper trades survive reboots
       even when DATABASE_URL is not configured.
    3. Local fallback: SQLite file (ephemeral on Streamlit Cloud).
    """

    SUPABASE_REST_NAMESPACE = "SIMPLE_REVERSAL_V7"

    def __init__(self, db_path: str | Path, secret_getter=None, supabase_sync: SupabaseStateSync | None = None):
        self.db_path = Path(db_path)
        self.secret_getter = secret_getter
        self.pg_url = configured_database_url(get_secret=secret_getter)
        sb_url, sb_key = configured_supabase_rest(get_secret=secret_getter)
        if supabase_sync is not None:
            self.supabase_sync = supabase_sync
        elif not self.pg_url and sb_url and sb_key:
            self.supabase_sync = SupabaseStateSync(
                supabase_url=sb_url,
                supabase_key=sb_key,
                writer_id="simple-reversal-v7",
                enforce_single_writer=False,
            )
        else:
            self.supabase_sync = None
        self.restored_from_supabase = False
        self.last_supabase_sync_note = ""
        self._init_schema()
        if not self.pg_url and self.supabase_sync and self.supabase_sync.enabled:
            self._restore_from_supabase_if_empty()

    @property
    def storage_mode(self) -> str:
        if self.pg_url:
            return "SUPABASE_POSTGRES"
        if self.supabase_sync and self.supabase_sync.enabled:
            return "SUPABASE_REST_SYNC"
        return "LOCAL_SQLITE"

    @property
    def storage_label(self) -> str:
        if self.pg_url:
            return "Supabase / PostgreSQL (Permanen)"
        if self.supabase_sync and self.supabase_sync.enabled:
            return "Supabase REST Backup Sync (Permanen)"
        return "SQLite Lokal (Sementara — hilang saat reboot cloud)"

    @property
    def is_persistent_cloud(self) -> bool:
        return self.storage_mode in ("SUPABASE_POSTGRES", "SUPABASE_REST_SYNC")

    def connect(self):
        return connect_database(self.db_path, pg_url=self.pg_url, get_secret=self.secret_getter)

    def _init_schema(self):
        with closing(self.connect()) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS simple_signals (
                signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                m15_touch_close_ms BIGINT NOT NULL, m5_close_ms BIGINT NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL, stop_price DOUBLE PRECISION NOT NULL,
                target_price DOUBLE PRECISION NOT NULL, rsi_previous DOUBLE PRECISION NOT NULL,
                rsi_current DOUBLE PRECISION NOT NULL, touch_band TEXT NOT NULL,
                provider TEXT NOT NULL, instrument_kind TEXT NOT NULL, created_ms BIGINT NOT NULL,
                alert_status TEXT NOT NULL DEFAULT '', paper_status TEXT NOT NULL DEFAULT '',
                UNIQUE(symbol, direction, m15_touch_close_ms, m5_close_ms))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS simple_paper_trades (
                signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL, stop_price DOUBLE PRECISION NOT NULL,
                target_price DOUBLE PRECISION NOT NULL, quantity DOUBLE PRECISION NOT NULL,
                leverage INTEGER NOT NULL, risk_usdt DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL, opened_ms BIGINT NOT NULL, updated_ms BIGINT NOT NULL,
                closed_ms BIGINT, exit_price DOUBLE PRECISION, pnl_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
                mark_price DOUBLE PRECISION NOT NULL, instrument_kind TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS simple_scan_lock (
                lock_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, expires_ms BIGINT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS simple_scan_runs (
                run_id TEXT PRIMARY KEY, at_ms BIGINT NOT NULL, provider TEXT NOT NULL,
                instrument_kind TEXT NOT NULL, scanned INTEGER NOT NULL, signals INTEGER NOT NULL,
                failed INTEGER NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL)""")
            conn.execute("CREATE TABLE IF NOT EXISTS simple_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_simple_signals_created ON simple_signals(created_ms)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_simple_trades_status ON simple_paper_trades(status)")
            conn.commit()

    def _export_snapshot_dict(self) -> dict:
        now_ms = int(time.time() * 1000)
        with closing(self.connect()) as conn:
            meta_rows = conn.execute("SELECT key, value FROM simple_meta").fetchall()
            sig_rows = conn.execute(
                "SELECT signal_id,symbol,direction,m15_touch_close_ms,m5_close_ms,entry_price,stop_price,target_price,"
                "rsi_previous,rsi_current,touch_band,provider,instrument_kind,created_ms,alert_status,paper_status "
                "FROM simple_signals ORDER BY created_ms DESC LIMIT 1000"
            ).fetchall()
            trade_rows = conn.execute(
                "SELECT signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,risk_usdt,status,"
                "opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind "
                "FROM simple_paper_trades ORDER BY opened_ms DESC LIMIT 2000"
            ).fetchall()
            run_rows = conn.execute(
                "SELECT run_id,at_ms,provider,instrument_kind,scanned,signals,failed,status,note "
                "FROM simple_scan_runs ORDER BY at_ms DESC LIMIT 200"
            ).fetchall()
        sig_keys = [
            "signal_id", "symbol", "direction", "m15_touch_close_ms", "m5_close_ms",
            "entry_price", "stop_price", "target_price", "rsi_previous", "rsi_current",
            "touch_band", "provider", "instrument_kind", "created_ms", "alert_status", "paper_status",
        ]
        trade_keys = [
            "signal_id", "symbol", "direction", "entry_price", "stop_price", "target_price",
            "quantity", "leverage", "risk_usdt", "status", "opened_ms", "updated_ms",
            "closed_ms", "exit_price", "pnl_usdt", "mark_price", "instrument_kind",
        ]
        run_keys = ["run_id", "at_ms", "provider", "instrument_kind", "scanned", "signals", "failed", "status", "note"]
        return {
            "namespace": self.SUPABASE_REST_NAMESPACE,
            "state_updated_ms": now_ms,
            "meta": {str(k): str(v) for k, v in meta_rows},
            "signals": [dict(zip(sig_keys, r)) for r in sig_rows],
            "trades": [dict(zip(trade_keys, r)) for r in trade_rows],
            "runs": [dict(zip(run_keys, r)) for r in run_rows],
        }

    def _sync_to_supabase_rest(self) -> bool:
        if self.pg_url or not (self.supabase_sync and self.supabase_sync.enabled):
            return False
        try:
            snap = self._export_snapshot_dict()
            ok = self.supabase_sync.push_backup(
                self.SUPABASE_REST_NAMESPACE,
                snap,
                check_remote_conflict=False,
            )
            self.last_supabase_sync_note = (
                "Backup Supabase REST berhasil"
                if ok
                else f"Backup Supabase REST gagal: {self.supabase_sync.last_error}"
            )
            return bool(ok)
        except Exception as exc:
            self.last_supabase_sync_note = f"Backup Supabase REST error: {type(exc).__name__}: {str(exc)[:140]}"
            return False

    def _restore_from_supabase_if_empty(self) -> bool:
        if self.pg_url or not (self.supabase_sync and self.supabase_sync.enabled):
            return False
        with closing(self.connect()) as conn:
            has_trades = conn.execute("SELECT COUNT(*) FROM simple_paper_trades").fetchone()[0] > 0
            has_signals = conn.execute("SELECT COUNT(*) FROM simple_signals").fetchone()[0] > 0
            has_meta = conn.execute("SELECT COUNT(*) FROM simple_meta").fetchone()[0] > 0
        if has_trades or has_signals or has_meta:
            return False
        row = self.supabase_sync.pull_backup(self.SUPABASE_REST_NAMESPACE)
        if not isinstance(row, dict):
            if self.supabase_sync.last_error:
                self.last_supabase_sync_note = f"Restore Supabase REST gagal: {self.supabase_sync.last_error}"
            return False
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return False
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                for k, v in (payload.get("meta") or {}).items():
                    conn.execute(
                        "INSERT INTO simple_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(k), str(v)),
                    )
                for s in (payload.get("signals") or []):
                    conn.execute(
                        "INSERT INTO simple_signals(signal_id,symbol,direction,m15_touch_close_ms,m5_close_ms,entry_price,stop_price,target_price,"
                        "rsi_previous,rsi_current,touch_band,provider,instrument_kind,created_ms,alert_status,paper_status) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO NOTHING",
                        (
                            s["signal_id"], s["symbol"], s["direction"], s["m15_touch_close_ms"], s["m5_close_ms"],
                            s["entry_price"], s["stop_price"], s["target_price"], s["rsi_previous"], s["rsi_current"],
                            s["touch_band"], s["provider"], s["instrument_kind"], s["created_ms"],
                            s.get("alert_status", ""), s.get("paper_status", ""),
                        ),
                    )
                for t in (payload.get("trades") or []):
                    conn.execute(
                        "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
                        "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO NOTHING",
                        (
                            t["signal_id"], t["symbol"], t["direction"], t["entry_price"], t["stop_price"],
                            t["target_price"], t["quantity"], t["leverage"], t["risk_usdt"], t["status"],
                            t["opened_ms"], t["updated_ms"], t.get("closed_ms"), t.get("exit_price"),
                            t.get("pnl_usdt", 0.0), t["mark_price"], t["instrument_kind"],
                        ),
                    )
                for r in (payload.get("runs") or []):
                    conn.execute(
                        "INSERT INTO simple_scan_runs(run_id,at_ms,provider,instrument_kind,scanned,signals,failed,status,note) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING",
                        (
                            r["run_id"], r["at_ms"], r["provider"], r["instrument_kind"],
                            r["scanned"], r["signals"], r["failed"], r["status"], r.get("note", ""),
                        ),
                    )
        self.restored_from_supabase = True
        self.last_supabase_sync_note = (
            f"Data dipulihkan dari Supabase REST ({len(payload.get('trades') or [])} trade, "
            f"{len(payload.get('signals') or [])} sinyal)"
        )
        return True

    def verify_persistence_health(self) -> dict:
        """Verify live connectivity to Supabase PostgreSQL or Supabase REST backup."""
        if self.pg_url:
            health = verify_postgres_health(pg_url=self.pg_url, get_secret=self.secret_getter, db_path=self.db_path)
            return {
                "mode": self.storage_mode,
                "label": self.storage_label,
                "persistent": bool(health.get("ok")),
                "ok": bool(health.get("ok")),
                "detail": "Koneksi & read/write Supabase PostgreSQL terverifikasi."
                if health.get("ok")
                else str(health.get("error") or "Gagal memverifikasi Supabase PostgreSQL."),
            }
        if self.supabase_sync and self.supabase_sync.enabled:
            ok = self._sync_to_supabase_rest()
            return {
                "mode": self.storage_mode,
                "label": self.storage_label,
                "persistent": ok,
                "ok": ok,
                "detail": self.last_supabase_sync_note or ("Sinkronisasi Supabase REST aktif." if ok else "Gagal sinkronisasi Supabase REST."),
            }
        return {
            "mode": self.storage_mode,
            "label": self.storage_label,
            "persistent": False,
            "ok": False,
            "detail": (
                "DATABASE_URL maupun SUPABASE_URL belum terdeteksi di Secrets. "
                "Data saat ini disimpan di SQLite lokal dan akan hilang saat Streamlit Cloud reboot/update."
            ),
        }

    def get_initial_equity(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
        return float(row[0]) if row else None

    def set_initial_equity_if_missing(self, value: float) -> float:
        chosen = max(50.0, float(value))
        inserted = False
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
                if row:
                    return float(row[0])
                conn.execute(
                    "INSERT INTO simple_meta(key,value) VALUES('initial_equity',?) ON CONFLICT(key) DO NOTHING",
                    (str(chosen),),
                )
                inserted = True
                saved = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
                result = float(saved[0]) if saved else chosen
        if inserted:
            self._sync_to_supabase_rest()
        return result

    def acquire_lock(self, owner: str, now_ms: int, lease_ms: int = 120_000) -> bool:
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute("SELECT owner_id, expires_ms FROM simple_scan_lock WHERE lock_key='scan'").fetchone()
                if row and int(row[1]) > now_ms and str(row[0]) != owner:
                    return False
                conn.execute(
                    "INSERT INTO simple_scan_lock(lock_key,owner_id,expires_ms) VALUES('scan',?,?) "
                    "ON CONFLICT(lock_key) DO UPDATE SET owner_id=excluded.owner_id, expires_ms=excluded.expires_ms",
                    (owner, now_ms + lease_ms),
                )
            return True

    def release_lock(self, owner: str):
        with closing(self.connect()) as conn:
            conn.execute("DELETE FROM simple_scan_lock WHERE lock_key='scan' AND owner_id=?", (owner,))
            conn.commit()

    def add_signal(self, signal: dict, provider: str, instrument_kind: str, settings: ScanSettings) -> tuple[bool, str]:
        created_ms = int(time.time() * 1000)
        sid_src = f"{signal['symbol']}|{signal['direction']}|{signal['m15_touch_close_ms']}|{signal['m5_close_ms']}"
        signal_id = sha256(sid_src.encode()).hexdigest()[:24]
        entry, stop, target = signal["entry"], signal["stop"], signal["target"]
        risk_distance = abs(entry - stop)
        if risk_distance <= 0:
            return False, "Invalid stop distance"
        risk_usdt = max(0.0, settings.initial_equity * min(max(settings.risk_pct, 0.1), 2.0) / 100.0)
        qty = risk_usdt / risk_distance
        max_notional_qty = settings.initial_equity * max(1, min(settings.leverage, 20)) / entry
        qty = min(qty, max_notional_qty)
        if not math.isfinite(qty) or qty <= 0:
            return False, "Invalid paper quantity"
        risk_usdt = qty * risk_distance  # Store the actual risk after the notional cap.
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                existing = conn.execute("SELECT signal_id FROM simple_signals WHERE signal_id=?", (signal_id,)).fetchone()
                if existing:
                    return False, signal_id
                conn.execute(
                    "INSERT INTO simple_signals(signal_id,symbol,direction,m15_touch_close_ms,m5_close_ms,entry_price,stop_price,target_price,"
                    "rsi_previous,rsi_current,touch_band,provider,instrument_kind,created_ms,alert_status,paper_status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (signal_id, signal["symbol"], signal["direction"], signal["m15_touch_close_ms"], signal["m5_close_ms"],
                     entry, stop, target, signal["rsi_previous"], signal["rsi_current"], signal["m15_touch_band"],
                     provider, instrument_kind, created_ms, "NOT_SENT", "SIGNAL_RECORDED"),
                )
                active = conn.execute(
                    "SELECT signal_id FROM simple_paper_trades WHERE symbol=? AND status='OPEN'", (signal["symbol"],)
                ).fetchone()
                if not active:
                    conn.execute(
                        "INSERT INTO simple_paper_trades(signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,"
                        "risk_usdt,status,opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?,NULL,NULL,0,?,?)",
                        (signal_id, signal["symbol"], signal["direction"], entry, stop, target, qty,
                         max(1, min(settings.leverage, 20)), risk_usdt, signal["m5_close_ms"], signal["m5_close_ms"],
                         entry, instrument_kind),
                    )
                    conn.execute("UPDATE simple_signals SET paper_status='PAPER_OPEN' WHERE signal_id=?", (signal_id,))
                else:
                    conn.execute("UPDATE simple_signals SET paper_status='SKIPPED_PAIR_ALREADY_OPEN' WHERE signal_id=?", (signal_id,))
        self._sync_to_supabase_rest()
        return True, signal_id

    def update_alert_status(self, signal_id: str, status: str):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE simple_signals SET alert_status=? WHERE signal_id=?", (str(status)[:220], signal_id))
            conn.commit()
        self._sync_to_supabase_rest()

    def active_trades(self) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,risk_usdt,status,"
                "opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind "
                "FROM simple_paper_trades WHERE status='OPEN' ORDER BY opened_ms"
            ).fetchall()
        keys = ["signal_id", "symbol", "direction", "entry", "stop", "target", "quantity", "leverage", "risk_usdt", "status",
                "opened_ms", "updated_ms", "closed_ms", "exit_price", "pnl_usdt", "mark_price", "instrument_kind"]
        return [dict(zip(keys, row)) for row in rows]

    def update_trade(self, trade: dict):
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE simple_paper_trades SET stop_price=?,status=?,updated_ms=?,closed_ms=?,exit_price=?,pnl_usdt=?,mark_price=? WHERE signal_id=?",
                (trade["stop"], trade["status"], trade["updated_ms"], trade.get("closed_ms"), trade.get("exit_price"),
                 trade.get("pnl_usdt", 0.0), trade["mark_price"], trade["signal_id"]),
            )
            conn.commit()
        self._sync_to_supabase_rest()

    def latest_signals(self, limit: int = 100) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT signal_id,symbol,direction,m15_touch_close_ms,m5_close_ms,entry_price,stop_price,target_price,"
                "rsi_previous,rsi_current,touch_band,provider,instrument_kind,created_ms,alert_status,paper_status "
                "FROM simple_signals ORDER BY created_ms DESC LIMIT ?", (int(limit),)
            ).fetchall()
        keys = ["signal_id", "symbol", "direction", "m15_touch_close_ms", "m5_close_ms", "entry", "stop", "target",
                "rsi_previous", "rsi_current", "touch_band", "provider", "instrument_kind", "created_ms", "alert_status", "paper_status"]
        return [dict(zip(keys, row)) for row in rows]

    def latest_telegram_signals(self, limit: int = 100, sent_only: bool = False) -> list[dict]:
        """Kembalikan hasil analisis yang lolos filter sinyal & alert Telegram beserta metrik risiko."""
        rows = self.latest_signals(limit=max(int(limit), 1))
        enriched = []
        for s in rows:
            alert_status = str(s.get("alert_status") or "")
            is_sent = alert_status == "Alert Telegram terkirim"
            if sent_only and not is_sent:
                continue
            entry = float(s["entry"])
            stop = float(s["stop"])
            target = float(s["target"])
            risk_dist = abs(entry - stop)
            reward_dist = abs(target - entry) if target > 0 else 0.0
            sl_pct = (risk_dist / entry * 100.0) if entry > 0 else 0.0
            tp_pct = (reward_dist / entry * 100.0) if (entry > 0 and target > 0) else 0.0
            rr_ratio = (reward_dist / risk_dist) if (risk_dist > 0 and target > 0) else 0.0
            delta_rsi = float(s["rsi_current"]) - float(s["rsi_previous"])
            item = dict(s)
            item.update(
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                rr_ratio=rr_ratio,
                delta_rsi=delta_rsi,
                telegram_sent=is_sent,
            )
            enriched.append(item)
        return enriched

    def paper_trades(self, limit: int = 100) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT signal_id,symbol,direction,entry_price,stop_price,target_price,quantity,leverage,risk_usdt,status,"
                "opened_ms,updated_ms,closed_ms,exit_price,pnl_usdt,mark_price,instrument_kind "
                "FROM simple_paper_trades ORDER BY opened_ms DESC LIMIT ?", (int(limit),)
            ).fetchall()
        keys = ["signal_id", "symbol", "direction", "entry", "stop", "target", "quantity", "leverage", "risk_usdt", "status",
                "opened_ms", "updated_ms", "closed_ms", "exit_price", "pnl_usdt", "mark_price", "instrument_kind"]
        return [dict(zip(keys, row)) for row in rows]

    def record_run(self, provider, instrument_kind, scanned, signals, failed, status, note=""):
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO simple_scan_runs(run_id,at_ms,provider,instrument_kind,scanned,signals,failed,status,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, int(time.time() * 1000), str(provider), str(instrument_kind), int(scanned), int(signals),
                 int(failed), str(status), str(note)[:500]),
            )
            conn.commit()
        self._sync_to_supabase_rest()

    def latest_run(self):
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT at_ms,provider,instrument_kind,scanned,signals,failed,status,note FROM simple_scan_runs ORDER BY at_ms DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return dict(zip(["at_ms", "provider", "instrument_kind", "scanned", "signals", "failed", "status", "note"], row))

    def paper_summary(self, initial_equity: float):
        saved_equity = self.get_initial_equity()
        initial_equity = float(saved_equity if saved_equity is not None else initial_equity)
        trades = self.paper_trades(5000)
        realized = sum(float(t["pnl_usdt"] or 0) for t in trades if t["status"] != "OPEN")
        unrealized = sum(
            (float(t["mark_price"]) - float(t["entry"])) * float(t["quantity"]) * (1 if t["direction"] == "LONG" else -1)
            for t in trades if t["status"] == "OPEN"
        )
        return {"initial_equity": float(initial_equity), "realized": realized, "unrealized": unrealized,
                "equity": float(initial_equity) + realized + unrealized,
                "active": sum(1 for t in trades if t["status"] == "OPEN"),
                "closed": sum(1 for t in trades if t["status"] != "OPEN")}


class SimpleTradingEngine:
    def __init__(self, market=None, store=None, root: str | Path | None = None, secret_getter=None):
        self.root = Path(root or Path(__file__).resolve().parent)
        self.secret_getter = secret_getter or os.environ.get
        self.market = market or FuturesProviderRouter(spot_fallback_enabled=False)
        db_path = Path(os.getenv("COINAGENT_DB_PATH", str(self.root / "data" / "simple_coinagent.sqlite3")))
        self.store = store or SimpleStore(db_path, secret_getter=self.secret_getter)
        self.owner_id = f"simple-{uuid.uuid4().hex[:10]}"
        self.last_report: dict | None = None
        self.telegram_session = requests.Session()

    def _secret(self, key: str) -> str:
        try:
            return str(self.secret_getter(key) or "").strip()
        except Exception:
            return str(os.getenv(key, "")).strip()

    @property
    def telegram_configured(self) -> bool:
        return bool(self._secret("TELEGRAM_BOT_TOKEN") and self._secret("TELEGRAM_CHAT_ID"))

    def _post_telegram_message(self, text: str) -> str:
        token, chat_id = self._secret("TELEGRAM_BOT_TOKEN"), self._secret("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return "Telegram belum dikonfigurasi"
        try:
            response = self.telegram_session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
                timeout=(5, 10), allow_redirects=False,
            )
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            return "Alert Telegram terkirim" if response.status_code == 200 and payload.get("ok") is True else f"Telegram gagal HTTP {response.status_code}"
        except requests.RequestException as exc:
            return f"Telegram error: {type(exc).__name__}"

    def _send_telegram(self, signal_id: str, signal: dict, provider: str, settings: ScanSettings | None = None) -> str:
        arrow = "🟢 BUY / LONG" if signal["direction"] == "LONG" else "🔴 SELL / SHORT"
        tp_mult = float(signal.get("tp_multiple", 2.0))
        tp_line = f"TP ({tp_mult:g}R): {signal['target']:.8g}" if signal.get("target", 0) > 0 else "TP: Open (Trailing Stop)"
        trailing_on = True if settings is None else bool(settings.trailing_stop_enabled)
        trail_line = (
            "Trailing Stop (Step R): +1.0R→BE(+0.05R) | +1.5R→+0.5R"
            + (" | +2.0R→+1.2R" if (tp_mult == 0.0 or tp_mult > 2.0) else "")
            if trailing_on
            else "Trailing Stop: Nonaktif (SL Tetap)"
        )
        text = (
            f"COINAGENT SIMPLE SIGNAL\n{arrow}\nPair: {signal['symbol']}\n"
            f"Market: {provider} · Futures · PAPER ONLY\n"
            f"M15: menyentuh Bollinger {signal['m15_touch_band']}\n"
            f"M5: candle reversal terkonfirmasi\n"
            f"RSI(14): {signal['rsi_previous']:.2f} → {signal['rsi_current']:.2f}\n"
            f"Entry: {signal['entry']:.8g}\nSL Awal: {signal['stop']:.8g}\n{tp_line}\n"
            f"{trail_line}\n"
            f"Waktu sinyal: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            "Indikator strategi: Bollinger Bands + RSI saja."
        )
        status = self._post_telegram_message(text)
        self.store.update_alert_status(signal_id, status)
        return status

    def _send_trailing_update_telegram(self, trade: dict, old_stop: float, new_stop: float, locked_r: float):
        if not self.telegram_configured:
            return
        arrow = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
        label = f"Break-Even (+{locked_r:.2f}R)" if locked_r < 0.45 else f"Kunci Profit +{locked_r:.2f}R"
        text = (
            f"COINAGENT TRAILING STOP UPDATE\n{arrow} · {trade['symbol']}\n"
            f"Tahap: {label}\n"
            f"Entry: {float(trade['entry']):.8g}\n"
            f"SL Lama: {old_stop:.8g} → SL Baru: {new_stop:.8g}\n"
            f"Mark: {float(trade['mark_price']):.8g}\n"
            f"Waktu: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        self._post_telegram_message(text)

    def _universe_symbols(self, universe: dict, limit: int) -> list[str]:
        info = universe.get("info") or {}
        allowed = set()
        for item in info.get("symbols", []):
            symbol = str(item.get("symbol") or "").upper()
            status = str(item.get("status") or "TRADING").upper()
            contract = str(item.get("contractType") or "PERPETUAL").upper()
            quote = str(item.get("quoteAsset") or "USDT").upper()
            if symbol.endswith("USDT") and status == "TRADING" and quote == "USDT" and contract in ("PERPETUAL", "LINEARPERPETUAL"):
                allowed.add(symbol)
        tickers = []
        for row in universe.get("tickers", []):
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in allowed:
                continue
            try:
                volume = float(row.get("quoteVolume") or row.get("turnover24h") or 0.0)
            except (TypeError, ValueError):
                volume = 0.0
            tickers.append((volume, symbol))
        tickers.sort(reverse=True)
        picked = [symbol for _, symbol in tickers[:max(1, min(int(limit), 200))]]
        # Existing open paper positions remain monitored even when they fall out
        # of the top-volume scan set.
        for trade in self.store.active_trades():
            if trade["symbol"] not in picked:
                picked.append(trade["symbol"])
        return picked

    def _monitor_trade(
        self,
        trade: dict,
        m5: pd.DataFrame,
        trailing_stop_enabled: bool = True,
        notify_trailing_telegram: bool = False,
    ):
        """Process every newly closed M5 candle with conservative exit ordering + Step R-Multiple Trailing Stop.

        For each closed M5 candle:
        1. Check stop hit using the stop price active BEFORE this candle (no intra-bar lookahead).
        2. Check fixed TP hit (if target > 0).
        3. If still OPEN and trailing_stop_enabled, advance stop_price along the Step R-Multiple ladder
           only in the profitable direction for subsequent candles.
        """
        if m5 is None or m5.empty:
            return
        last_seen_ms = int(trade.get("updated_ms") or trade["opened_ms"])
        new_bars = m5[pd.to_numeric(m5["close_time"], errors="coerce") > last_seen_ms]
        if new_bars.empty:
            return
        direction = trade["direction"]
        sign = 1 if direction == "LONG" else -1
        entry = float(trade["entry"])
        current_stop = float(trade["stop"])
        initial_stop = current_stop
        target = float(trade["target"] or 0.0)
        r_dist = initial_risk_distance(trade)
        mark = float(trade["mark_price"])
        exit_price = None
        status = "OPEN"
        updated_ms = last_seen_ms
        last_locked_r = None

        for _, candle in new_bars.iterrows():
            candle_close_ms = int(candle.close_time)
            if candle_close_ms <= int(trade["opened_ms"]):
                continue

            candle_high = float(candle.high)
            candle_low = float(candle.low)
            candle_close = float(candle.close)

            # 1. Conservative check against the stop active entering this candle.
            stop_hit = (candle_low <= current_stop) if direction == "LONG" else (candle_high >= current_stop)
            target_hit = (
                (candle_high >= target) if direction == "LONG" else (candle_low <= target)
            ) if target > 0 else False

            mark = candle_close
            updated_ms = candle_close_ms

            if stop_hit:
                exit_price = current_stop
                profit_offset = (current_stop - entry) * sign
                if profit_offset > 1e-12:
                    status = "CLOSED_TRAIL_TP"
                elif abs(profit_offset) <= 1e-12:
                    status = "CLOSED_BE"
                else:
                    status = "CLOSED_SL"
                break

            if target_hit:
                exit_price = target
                status = "CLOSED_TP"
                break

            # 2. Still OPEN -> evaluate Step-Based R-Multiple Trailing Stop for next candle(s).
            if trailing_stop_enabled and r_dist > 0:
                favorable_price = candle_high if direction == "LONG" else candle_low
                favorable_r = (favorable_price - entry) * sign / r_dist
                lock_r = compute_step_trailing_lock_r(favorable_r)
                if lock_r is not None:
                    candidate_stop = entry + sign * lock_r * r_dist
                    if direction == "LONG" and candidate_stop > current_stop:
                        current_stop = candidate_stop
                        last_locked_r = lock_r
                    elif direction == "SHORT" and candidate_stop < current_stop:
                        current_stop = candidate_stop
                        last_locked_r = lock_r

        trade["stop"] = current_stop
        if exit_price is not None:
            pnl = (exit_price - entry) * float(trade["quantity"]) * sign
            trade.update(status=status, closed_ms=updated_ms, exit_price=exit_price, pnl_usdt=pnl, mark_price=exit_price)
        else:
            trade.update(status="OPEN", pnl_usdt=0.0, mark_price=mark)
        trade["updated_ms"] = updated_ms
        self.store.update_trade(trade)

        if (
            notify_trailing_telegram
            and status == "OPEN"
            and last_locked_r is not None
            and abs(current_stop - initial_stop) > 1e-12
        ):
            self._send_trailing_update_telegram(trade, initial_stop, current_stop, last_locked_r)

    def scan(self, settings: ScanSettings | None = None, force: bool = False) -> dict:
        settings = settings or ScanSettings()
        saved_equity = self.store.set_initial_equity_if_missing(settings.initial_equity)
        settings = replace(settings, initial_equity=saved_equity)
        now_ms = int(time.time() * 1000)
        if not self.store.acquire_lock(self.owner_id, now_ms):
            return {"status": "BUSY", "note": "Scanner lain sedang berjalan pada database ini.", "scanned": 0, "signals": []}
        report = {"status": "RUNNING", "scanned": 0, "failed": 0, "signals": [], "errors": [], "started_ms": now_ms}
        try:
            universe = self.market.universe()
            kind = str(getattr(self.market, "instrument_kind", "USD_M_PERPETUAL"))
            provider = str(getattr(self.market, "market_label", "Futures provider"))
            if kind not in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL", "BITGET_USDT_PERPETUAL") or not getattr(self.market, "futures_available", True):
                raise MarketError("Data Futures Perpetual tidak tersedia. Scanner tidak menggunakan Spot sebagai pengganti.")
            server_ms = int(universe.get("now_ms") or int(time.time() * 1000))
            symbols = self._universe_symbols(universe, settings.symbols)
            for symbol in symbols:
                try:
                    m15 = self.market.closed_candles(symbol, "15m", server_ms, limit=80)
                    if str(getattr(self.market, "instrument_kind", kind)) != kind:
                        raise MarketError("Provider berubah saat scan; siklus dihentikan agar data tidak tercampur.")
                    m5 = self.market.closed_candles(symbol, "5m", server_ms, limit=80)
                    if str(getattr(self.market, "instrument_kind", kind)) != kind:
                        raise MarketError("Provider berubah saat scan; siklus dihentikan agar data tidak tercampur.")
                    active = next((t for t in self.store.active_trades() if t["symbol"] == symbol), None)
                    if active:
                        self._monitor_trade(
                            active,
                            m5,
                            trailing_stop_enabled=settings.trailing_stop_enabled,
                            notify_trailing_telegram=settings.send_telegram,
                        )
                    signal = evaluate_reversal(m15, m5, symbol=symbol, tp_multiple=settings.tp_multiple)
                    report["scanned"] += 1
                    if signal:
                        created, signal_id = self.store.add_signal(signal, provider, kind, settings)
                        if created:
                            if settings.send_telegram:
                                alert_status = self._send_telegram(signal_id, signal, provider, settings=settings)
                            else:
                                alert_status = "Telegram dinonaktifkan"
                                self.store.update_alert_status(signal_id, alert_status)
                            signal.update(signal_id=signal_id, alert_status=alert_status)
                            report["signals"].append(signal)
                except Exception as exc:
                    report["failed"] += 1
                    report["errors"].append(f"{symbol}: {type(exc).__name__}: {str(exc)[:150]}")
                    if "Provider berubah saat scan" in str(exc):
                        break
            report.update(
                status="COMPLETED" if report["failed"] == 0 else "COMPLETED_WITH_ERRORS",
                provider=provider, instrument_kind=kind, universe_count=len(symbols),
                completed_ms=int(time.time() * 1000),
            )
            self.store.record_run(provider, kind, report["scanned"], len(report["signals"]), report["failed"], report["status"],
                                  " | ".join(report["errors"][:5]))
        except Exception as exc:
            report.update(status="FAILED", error=f"{type(exc).__name__}: {str(exc)[:250]}")
            try:
                self.store.record_run(str(getattr(self.market, "market_label", "Unknown")),
                                      str(getattr(self.market, "instrument_kind", "UNKNOWN")),
                                      report["scanned"], len(report["signals"]), report["failed"], "FAILED", report["error"])
            except Exception:
                pass
        finally:
            self.store.release_lock(self.owner_id)
            self.last_report = report
        return report

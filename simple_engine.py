"""CoinAgentX Simple Reversal Engine.

Production signal logic uses ONLY Bollinger Bands on closed M15 candles and
RSI(14) plus a two-candle reversal confirmation on closed M5 candles.
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

from futures.db_backend import atomic_transaction, connect_database, configured_database_url
from futures.market import MarketError
from futures.provider_router import FuturesProviderRouter

APP_VERSION = "7.0.1-simple"
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0


@dataclass(frozen=True)
class ScanSettings:
    symbols: int = 50
    interval_seconds: int = 60
    initial_equity: float = 1000.0
    risk_pct: float = 0.5
    leverage: int = 2
    send_telegram: bool = True


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


def evaluate_reversal(m15: pd.DataFrame, m5: pd.DataFrame, symbol: str = "") -> dict | None:
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
        target = float(current.close) + 2.0 * risk
    else:
        candle_ok = (
            float(current.low) < float(previous.low)
            and float(current.close) < float(previous.close)
            and float(current.close) < float(current.open)
        )
        rsi_ok = rsi_prev >= RSI_OVERBOUGHT and rsi_now < rsi_prev
        stop = max(float(previous.high), float(current.high))
        risk = stop - float(current.close)
        target = float(current.close) - 2.0 * risk

    if not candle_ok or not rsi_ok or risk <= 0 or target <= 0:
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
    """Small durable store; uses PostgreSQL when DATABASE_URL is configured, else SQLite."""

    def __init__(self, db_path: str | Path, secret_getter=None):
        self.db_path = Path(db_path)
        self.secret_getter = secret_getter
        self.pg_url = configured_database_url(get_secret=secret_getter)
        self._init_schema()

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

    def get_initial_equity(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
        return float(row[0]) if row else None

    def set_initial_equity_if_missing(self, value: float) -> float:
        chosen = max(50.0, float(value))
        with closing(self.connect()) as conn:
            with atomic_transaction(conn):
                row = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
                if row:
                    return float(row[0])
                conn.execute(
                    "INSERT INTO simple_meta(key,value) VALUES('initial_equity',?) ON CONFLICT(key) DO NOTHING",
                    (str(chosen),),
                )
                saved = conn.execute("SELECT value FROM simple_meta WHERE key='initial_equity'").fetchone()
                return float(saved[0]) if saved else chosen

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
        return True, signal_id

    def update_alert_status(self, signal_id: str, status: str):
        with closing(self.connect()) as conn:
            conn.execute("UPDATE simple_signals SET alert_status=? WHERE signal_id=?", (str(status)[:220], signal_id))
            conn.commit()

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
                "UPDATE simple_paper_trades SET status=?,updated_ms=?,closed_ms=?,exit_price=?,pnl_usdt=?,mark_price=? WHERE signal_id=?",
                (trade["status"], trade["updated_ms"], trade.get("closed_ms"), trade.get("exit_price"),
                 trade.get("pnl_usdt", 0.0), trade["mark_price"], trade["signal_id"]),
            )
            conn.commit()

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

    def _send_telegram(self, signal_id: str, signal: dict, provider: str) -> str:
        token, chat_id = self._secret("TELEGRAM_BOT_TOKEN"), self._secret("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            status = "Telegram belum dikonfigurasi"
            self.store.update_alert_status(signal_id, status)
            return status
        arrow = "🟢 BUY / LONG" if signal["direction"] == "LONG" else "🔴 SELL / SHORT"
        text = (
            f"COINAGENT SIMPLE SIGNAL\n{arrow}\nPair: {signal['symbol']}\n"
            f"Market: {provider} · Futures · PAPER ONLY\n"
            f"M15: menyentuh Bollinger {signal['m15_touch_band']}\n"
            f"M5: candle reversal terkonfirmasi\n"
            f"RSI(14): {signal['rsi_previous']:.2f} → {signal['rsi_current']:.2f}\n"
            f"Entry: {signal['entry']:.8g}\nSL: {signal['stop']:.8g}\nTP (2R): {signal['target']:.8g}\n"
            f"Waktu sinyal: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            "Indikator strategi: Bollinger Bands + RSI saja."
        )
        try:
            response = self.telegram_session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
                timeout=(5, 10), allow_redirects=False,
            )
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            status = "Alert Telegram terkirim" if response.status_code == 200 and payload.get("ok") is True else f"Telegram gagal HTTP {response.status_code}"
        except requests.RequestException as exc:
            status = f"Telegram error: {type(exc).__name__}"
        self.store.update_alert_status(signal_id, status)
        return status

    def _universe_symbols(self, universe: dict, limit: int) -> list[str]:
        info = universe.get("info") or {}
        allowed = set()
        for item in info.get("symbols", []):
            symbol = str(item.get("symbol") or "").upper()
            status = str(item.get("status") or "TRADING").upper()
            contract = str(item.get("contractType") or "PERPETUAL").upper()
            quote = str(item.get("quoteAsset") or "USDT").upper()
            if symbol.endswith("USDT") and status in ("TRADING", "TRADING") and quote == "USDT" and contract in ("PERPETUAL", "LINEARPERPETUAL"):
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

    def _monitor_trade(self, trade: dict, m5: pd.DataFrame):
        """Process every newly closed M5 candle so a missed scan cannot skip an exit."""
        if m5 is None or m5.empty:
            return
        last_seen_ms = int(trade.get("updated_ms") or trade["opened_ms"])
        new_bars = m5[pd.to_numeric(m5["close_time"], errors="coerce") > last_seen_ms]
        if new_bars.empty:
            return
        direction = trade["direction"]
        sign = 1 if direction == "LONG" else -1
        mark = float(trade["mark_price"])
        exit_price = None
        status = "OPEN"
        updated_ms = last_seen_ms
        for _, candle in new_bars.iterrows():
            candle_close_ms = int(candle.close_time)
            if candle_close_ms <= int(trade["opened_ms"]):
                continue
            stop_hit = float(candle.low) <= float(trade["stop"]) if direction == "LONG" else float(candle.high) >= float(trade["stop"])
            target_hit = float(candle.high) >= float(trade["target"]) if direction == "LONG" else float(candle.low) <= float(trade["target"])
            mark = float(candle.close)
            updated_ms = candle_close_ms
            if stop_hit:  # Conservative ordering when a candle spans both stop and target.
                exit_price = float(trade["stop"])
                status = "CLOSED_SL"
                break
            if target_hit:
                exit_price = float(trade["target"])
                status = "CLOSED_TP"
                break
        if exit_price is not None:
            pnl = (exit_price - float(trade["entry"])) * float(trade["quantity"]) * sign
            trade.update(status=status, closed_ms=updated_ms, exit_price=exit_price, pnl_usdt=pnl, mark_price=exit_price)
        else:
            trade.update(status="OPEN", pnl_usdt=0.0, mark_price=mark)
        trade["updated_ms"] = updated_ms
        self.store.update_trade(trade)

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
            if kind not in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL") or not getattr(self.market, "futures_available", True):
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
                        self._monitor_trade(active, m5)
                    signal = evaluate_reversal(m15, m5, symbol=symbol)
                    report["scanned"] += 1
                    if signal:
                        created, signal_id = self.store.add_signal(signal, provider, kind, settings)
                        if created:
                            if settings.send_telegram:
                                alert_status = self._send_telegram(signal_id, signal, provider)
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

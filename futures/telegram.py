"""Approved BUY/SELL alerts with a persistent, atomic per-candle dedup ledger."""
import hashlib
import math
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from .groq_rotation import combine
from .models import TIMEFRAMES, SPOT_SIMULATION_CHECKS

LABELS = {"SCALPING": "SCALPER", "INTRADAY": "INTRADAY", "SWING": "SWING"}


def eligible(data, monotonic=time.monotonic):
    r, snap, ai = data["result"], data["snapshot"], data.get("ai")
    if data.get("status") != "VALID" or combine(r, ai) != "VALID" or not r.plan:
        return False
    p = r.plan
    if p.get("instrument_kind", "USD_M_PERPETUAL") != snap.instrument_kind:
        return False
    if r.direction not in ("LONG", "SHORT") or p.get("direction") != r.direction:
        return False
    if p.get("mode") not in LABELS or p.get("symbol") != snap.symbol or p["mode"] != snap.mode:
        return False
    def passed(check):
        allowed = {"Likuidasi sesuai rencana"}
        if r.meta.get("instrument_kind") == p.get("instrument_kind") == snap.instrument_kind == "SPOT_REFERENCE":
            allowed |= SPOT_SIMULATION_CHECKS
        return check.get("Status") == "LULUS" or (
            r.meta.get("scope") == p.get("scope") == "PAPER" and
            check.get("Kriteria") in allowed and check.get("Status") == "SIMULASI")
    if not r.checks or not all(passed(x) for x in r.checks):
        return False
    elapsed = monotonic() - data["monotonic"]
    if not math.isfinite(elapsed) or elapsed < 0:
        return False
    now = snap.now_ms + elapsed * 1000
    return (now < p["expires_ms"] and all(
        0 <= now - snap.quote_times.get(k, 0) <= 30000
        for k in snap.quote_keys))


def candle_key(plan):
    # Quantity/configuration changes on the same candle do not spam the chat.
    prefix = "PAPER:" if plan.get("scope") == "PAPER" else ""
    if plan.get("instrument_kind") == "SPOT_REFERENCE":
        prefix += "SPOT:"
    return prefix + ":".join(str(plan[k]) for k in ("symbol", "mode", "direction", "signal_close_ms"))


def format_alert(data):
    p, snap = data["result"].plan, data["snapshot"]
    n = lambda x: f"{x:,.8f}".rstrip("0").rstrip(".")
    stamp = lambda x: datetime.fromtimestamp(x / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB")
    side = "BUY / LONG" if p["direction"] == "LONG" else "SELL / SHORT"
    bias, execution = TIMEFRAMES[p["mode"]]
    reasons = [" ".join(x.split())[:150] for x in (data.get("ai") or {}).get("reasons", [])[:3]]
    paper = p.get("scope") == "PAPER"
    spot = p.get("instrument_kind") == "SPOT_REFERENCE"
    return "\n".join([
        ("PAPER | " if paper else "") + f"{side} | {LABELS[p['mode']]} | {p['symbol']}",
        "SETUP VALID — MENUNGGU TRIGGER (bukan order terisi)",
        ("Binance SPOT REFERENCE" if spot else "Binance USD-M perpetual") + f" · Bias {bias} / eksekusi {execution}",
        f"Data: {stamp(snap.now_ms)}",
        f"Trigger entry (Last): {n(p['entry_trigger'])}",
        f"Limit entry: {n(p['entry'])} · STOP-LIMIT",
        f"SL: {n(p['sl'])} · " + ("STOP simulasi / Last spot" if spot else "STOP-MARKET / Mark / reduce-only"),
        f"TP1: {n(p['tp1'])} · Q {n(p['tp1_qty'])} ({p['tp1_qty']/p['qty']:.1%})",
        f"TP2: {n(p['tp2'])} · Q {n(p['tp2_qty'])} ({p['tp2_qty']/p['qty']:.1%})",
        f"Runner: Q {n(p['runner_qty'])} · trailing 2 ATR setelah TP2",
        "SL ke estimasi BE termasuk biaya setelah TP1 terisi.",
        f"Q: {n(p['qty'])} · Leverage {p['leverage']}x isolated",
        f"Risiko termasuk biaya: {p['risk']:.2f} USDT ({p['risk']/p['equity']:.2%} ekuitas)",
        f"RR bersih TP1: {p['net_rr_tp1']:.2f}",
        f"Berlaku sampai: {stamp(p['expires_ms'])}",
        "Batalkan jika SL tersentuh sebelum fill, bias/filter berubah, atau kedaluwarsa.",
        "Jangan kejar harga jika trigger sudah terlewati. Partial fill harus dilindungi SL.",
        "Time-exit: 12 candle tanpa +1R; scalper/intraday berakhir paling lambat 00:00 UTC.",
        "AI: " + "; ".join(reasons),
        f"ID: {p['id']}",
        *( ["Dicatat PENDING di paper trading; belum dianggap fill.",
             "SIMULASI: biaya/fill dimodelkan; likuidasi tidak dimodelkan. Bukan order live."] if paper else []),
        *( ["Harga, volume dan depth SPOT. LONG/SHORT/leverage hanya simulasi; funding/Mark perpetual tidak tersedia."] if spot else []),
    ])


class TelegramAlerts:
    def __init__(self, token, chat_id, db, session=None, clock=time.time, monotonic=time.monotonic):
        self.token, self.chat_id, self.db = str(token or "").strip(), str(chat_id or "").strip(), Path(db)
        self.session, self.clock, self.monotonic = session or requests.Session(), clock, monotonic
        self.target = hashlib.sha256((self.token.split(":")[0] + ":" + self.chat_id).encode()).hexdigest()
        self.lock, self.cooldown_until = threading.Lock(), 0.0

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def connect(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=5)
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_alerts (
            target TEXT, signal TEXT, state TEXT, retry_at REAL, updated_at REAL,
            PRIMARY KEY(target, signal))""")
        conn.commit()
        return conn

    def reserve(self, signal):
        now = self.clock()
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state, retry_at FROM telegram_alerts WHERE target=? AND signal=?",
                               (self.target, signal)).fetchone()
            if row and (row[0] != "retry" or now < row[1]):
                return False
            conn.execute("INSERT OR REPLACE INTO telegram_alerts VALUES (?,?,?,?,?)",
                         (self.target, signal, "sending", 0, now))
            conn.execute("DELETE FROM telegram_alerts WHERE updated_at < ?", (now - 30 * 86400,))
        return True

    def record(self, signal, state, retry_at=0):
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE telegram_alerts SET state=?, retry_at=?, updated_at=? WHERE target=? AND signal=?",
                         (state, retry_at, self.clock(), self.target, signal))

    def send(self, data):
        if not eligible(data, self.monotonic):
            return "Diam · WAIT/NO TRADE atau data kedaluwarsa."
        if not self.configured:
            return "Telegram belum lengkap di Secrets (token dan chat ID)."
        with self.lock:
            if self.clock() < self.cooldown_until:
                return "Telegram cooldown; tunggu scan berikutnya."
            signal = candle_key(data["result"].plan)
            try:
                if not self.reserve(signal):
                    return "Alert candle ini sudah dikirim atau pernah dicoba; duplikat diblokir."
                # Recheck after waiting for the lock/SQLite transaction.
                if not eligible(data, self.monotonic):
                    self.record(signal, "retry")
                    return "Diam · snapshot kedaluwarsa sebelum pengiriman."
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": format_alert(data),
                          "link_preview_options": {"is_disabled": True}},
                    timeout=(5, 10), allow_redirects=False)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                code = body.get("error_code", response.status_code)
                if response.status_code == 200 and body.get("ok") is True:
                    self.record(signal, "sent")
                    return "Alert Telegram terkirim."
                if code == 429:
                    try:
                        delay = float((body.get("parameters") or {}).get("retry_after", 60))
                        if not math.isfinite(delay):
                            delay = 60
                    except (ValueError, TypeError, AttributeError):
                        delay = 60
                    self.cooldown_until = self.clock() + max(1, delay)
                    self.record(signal, "retry", self.cooldown_until)
                    return "Telegram rate limit; mengikuti retry_after tanpa mengirim ulang sekarang."
                if code in (400, 401, 403):
                    self.cooldown_until = self.clock() + 300
                    self.record(signal, "retry", self.cooldown_until)
                    return f"Telegram menolak alert (HTTP {code}). Periksa bot/chat ID dan izin bot; jeda 5 menit."
                self.record(signal, "uncertain")
                return "Status pengiriman Telegram tidak pasti; candle ini tidak diulang agar tidak duplikat."
            except requests.RequestException:
                # A timeout may happen AFTER Telegram accepted the message.
                try:
                    self.record(signal, "uncertain")
                except (OSError, sqlite3.Error):
                    pass  # The pre-send reservation still blocks duplicates.
                return "Koneksi Telegram terputus; pengiriman tidak pasti, candle ini tidak diulang."
            except (OSError, sqlite3.Error):
                return "Catatan deduplikasi Telegram tidak tersedia; alert dihentikan agar tidak berulang."

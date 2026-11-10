"""Approved BUY/SELL alerts, Reversal Watchlist, Market Scanner Report, and Signal Cancellation with persistent atomic dedup."""
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
    max_quote_age = 60000 if r.meta.get("relaxed_mode") else 30000
    return (now < p["expires_ms"] and all(
        0 <= now - snap.quote_times.get(k, 0) <= max_quote_age
        for k in snap.quote_keys))


def candle_key(plan):
    # Quantity/configuration changes on the same candle do not spam the chat.
    prefix = "PAPER:" if plan.get("scope") == "PAPER" else ""
    if plan.get("instrument_kind") == "SPOT_REFERENCE":
        prefix += "SPOT:"
    return prefix + ":".join(str(plan[k]) for k in ("symbol", "mode", "direction", "signal_close_ms"))


def format_alert(data):
    """Alert 2 — Entry signal (combines Master Prompt + Addendum Multi-Stage Scalping Signal format)."""
    p, snap = data["result"].plan, data["snapshot"]
    r = data["result"]
    n = lambda x: f"{x:,.8f}".rstrip("0").rstrip(".") if isinstance(x, (int, float)) else str(x)
    stamp = lambda x: datetime.fromtimestamp(x / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB")
    side = "BUY / LONG" if p["direction"] == "LONG" else "SELL / SHORT"
    bias, execution = TIMEFRAMES[p["mode"]]
    reasons = [" ".join(x.split())[:150] for x in (data.get("ai") or {}).get("reasons", [])[:4]]
    if not reasons:
        reasons = ["Trend & pullback terkonfirmasi", "CHoCH & Stoch RSI searah", "Volume >= 1.2x SMA20", "Risk/Reward bersih memenuhi syarat"]
    paper = p.get("scope") == "PAPER"
    spot = p.get("instrument_kind") == "SPOT_REFERENCE"
    conf_score = p.get("confidence_score", 90)
    conf_status = p.get("confidence_calibration_status", "UNCALIBRATED (MAX 3x)")
    max_lev = p.get("max_allowed_leverage", p["leverage"])
    sl_pct = p.get("sl_distance_pct", (p["distance"] / p["entry"] * 100) if p["entry"] else 0.0)
    margin_loss = p.get("estimated_margin_loss_pct", (p["risk"] / p["margin"] * 100) if p["margin"] else 0.0)
    tp3 = p.get("tp3", p["tp2"] + (p["tp2"] - p["tp1"]))
    est_fees = p.get("estimated_fees_usdt", p["qty"] * p.get("cost_sl_per_unit", 0))
    liq_price = n(p["liquidation"]) if p.get("liquidation") else "SIMULASI (Tidak dimodelkan pada Spot/Paper)"
    liq_check = p.get("liquidation_safety_check", "SIMULASI PAPER (SL < Jarak Likuidasi)")
    adx_val = r.metrics.get("adx", 22.0)
    regime = "TRENDING" if (isinstance(adx_val, (int, float)) and adx_val >= 25) else "RANGE / REVERSAL"
    return "\n".join([
        "🚨 BINANCE FUTURES SCALPING SIGNAL",
        ("PAPER | " if paper else "") + f"{side} | {LABELS[p['mode']]} | {p['symbol']}",
        "SETUP VALID — MENUNGGU TRIGGER (bukan order terisi)",
        ("Binance SPOT REFERENCE" if spot else "Binance USD-M perpetual") + f" · Bias {bias} / eksekusi {execution} (M15 + M5)",
        f"Data: {stamp(snap.now_ms)}",
        f"SYMBOL: {p['symbol']}",
        f"DIRECTION: {p['direction']}",
        f"FINAL CONFIDENCE: {conf_score}/100 (CONFIDENCE SCORE: {conf_score}/100)",
        f"CONFIDENCE CALIBRATION STATUS: {conf_status}",
        f"Leverage cap: {max_lev}x (MAXIMUM ALLOWED LEVERAGE: {max_lev}x | SELECTED LEVERAGE: {p['leverage']}x)",
        f"Market regime: {regime} · Setup: REVERSAL / PULLBACK",
        "ENTRY PLAN",
        f"Trigger entry (Last): {n(p['entry_trigger'])}",
        f"Entry zone / Limit entry (ENTRY PRICE): {n(p['entry'])} · STOP-LIMIT",
        f"Stop loss (TECHNICAL STOP LOSS / SL:): {n(p['sl'])} · " + ("STOP simulasi / Last spot" if spot else "STOP-MARKET / Mark / reduce-only"),
        f"STOP LOSS PRICE DISTANCE: {n(p['distance'])} ({sl_pct:.2f}%)",
        f"TP1: {n(p['tp1'])} · Q {n(p['tp1_qty'])} ({p['tp1_qty']/p['qty']:.1%})",
        f"TP2: {n(p['tp2'])} · Q {n(p['tp2_qty'])} ({p['tp2_qty']/p['qty']:.1%})",
        f"TP3: {n(tp3)} · Runner: Q {n(p['runner_qty'])} · trailing 2 ATR setelah TP2",
        "RISK MANAGEMENT",
        f"Risiko termasuk biaya (ACCOUNT RISK): {p['risk']:.2f} USDT ({p['risk']/p['equity']:.2%} ekuitas)",
        f"ESTIMATED MARGIN LOSS: {margin_loss:.2f}%",
        f"INITIAL MARGIN: {p['margin']:.2f} USDT",
        f"POSITION NOTIONAL: {p['notional']:.2f} USDT",
        f"LIQUIDATION PRICE: {liq_price}",
        f"Liquidation safety (LIQUIDATION SAFETY CHECK:): {liq_check}",
        f"ESTIMATED FEES: {est_fees:.4f} USDT",
        f"Estimated R:R / RR bersih TP1 (NET RISK/REWARD): {p['net_rr_tp1']:.2f}",
        f"FINAL DECISION: {p['direction']}",
        "SL ke estimasi BE termasuk biaya setelah TP1 terisi.",
        f"Q: {n(p['qty'])} · Leverage {p['leverage']}x isolated",
        f"Berlaku sampai: {stamp(p['expires_ms'])}",
        "TECHNICAL REASONS & AI",
        "AI: " + "; ".join(reasons),
        "INVALIDATION",
        "Batalkan jika SL tersentuh sebelum fill, bias/filter berubah, atau kedaluwarsa.",
        "Jangan kejar harga jika trigger sudah terlewati. Partial fill harus dilindungi SL.",
        "Time-exit: 12 candle tanpa +1R; scalper/intraday berakhir paling lambat 00:00 UTC.",
        f"Signal timestamp: {stamp(snap.now_ms)} · Candle status: CLOSED",
        f"Signal ID (ID:): {p['id']}",
        "Status: ENTRY VALID — BUKAN JAMINAN PROFIT.",
        *( ["Dicatat PENDING di paper trading; belum dianggap fill.",
             "SIMULASI: biaya/fill dimodelkan; likuidasi tidak dimodelkan. Bukan order live."] if paper else []),
        *( ["Harga, volume dan depth SPOT. LONG/SHORT/leverage hanya simulasi; funding/Mark perpetual tidak tersedia."] if spot else []),
    ])


def format_watchlist_alert(data, candidate_info=None):
    """Alert 3 — Setup watchlist (👀 REVERSAL WATCHLIST) per Section 8 of Addendum."""
    r, snap = data["result"], data["snapshot"]
    info = candidate_info or {}
    direction = r.direction or info.get("candidate_direction", "LONG")
    score = r.metrics.get("confidence_score") or info.get("m15_score", 75)
    m15_summary = info.get("m15_summary") or f"Bias & struktur {snap.symbol} ({LABELS.get(snap.mode, snap.mode)}) menunjukkan potensi setup {direction}."
    failed_checks = [c["Kriteria"] for c in r.checks if c.get("Status") != "LULUS" and c.get("Status") != "SIMULASI"]
    waiting_items = failed_checks[:4] if failed_checks else ["CHoCH M5", "Stoch RSI cross", "Volume >= 1.2x SMA20", "Retest area breakout/breakdown"]
    waiting_lines = "\n".join(f"- {item}" for item in waiting_items)
    return "\n".join([
        "👀 REVERSAL WATCHLIST",
        "",
        f"Symbol: {snap.symbol}",
        f"Direction: {direction}",
        f"Score: {score}/100",
        "",
        "M15 setup:",
        f"{m15_summary}",
        "",
        "Menunggu konfirmasi:",
        waiting_lines,
        "",
        "Status: WAITING FOR M5 CONFIRMATION",
    ])


def format_cancellation_alert(symbol, signal_id, reason):
    """Alert 4 — Signal cancellation (❌ SIGNAL CANCELLED) per Section 8 of Addendum."""
    return "\n".join([
        "❌ SIGNAL CANCELLED",
        "",
        f"Symbol: {symbol}",
        f"Signal ID: {signal_id}",
        f"Reason: {reason}",
    ])


def format_scan_summary(report):
    """Alert 1 & Section 10 — Market screening report (MARKET SCANNER REPORT)."""
    stamp = lambda x: datetime.fromtimestamp(x / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB") if x else "N/A"
    total_market = report.get("universe_count", 0)
    eligible_count = report.get("eligible_count", 0)
    long_wl = report.get("long_watchlist_count", len([x for x in report.get("shortlist", []) if x.get("watchlist_side") == "LONG"]))
    short_wl = report.get("short_watchlist_count", len([x for x in report.get("shortlist", []) if x.get("watchlist_side") == "SHORT"]))
    ranked = report.get("ranked") or report.get("selected") or report.get("shortlist", [])
    analyses = report.get("analyses", [])
    valid_count = sum(1 for d in analyses if d.get("status") == "VALID")

    top_lines = []
    for idx, item in enumerate(ranked[:3], 1):
        sym = item.get("symbol", "N/A")
        side = item.get("candidate_direction") or item.get("watchlist_side") or "WATCH"
        sc = item.get("m15_score") or round(float(item.get("screening_score", 0)) * 10, 1)
        top_lines.append(f"{idx}. {sym} — {side} — Score {sc}")
    if not top_lines:
        top_lines.append("- Belum ada kandidat yang lolos penyaringan tahap ini.")

    reason_lines = []
    for d in analyses:
        if d.get("status") != "VALID":
            sym = d["snapshot"].symbol
            rs = "; ".join((d["result"].reasons + (d.get("ai") or {}).get("reasons", []))[:2]) or "Menunggu konfirmasi M5"
            reason_lines.append(f"- {sym}: {rs[:140]}")
    for err in report.get("errors", [])[:3]:
        reason_lines.append(f"- {err[:140]}")

    status_label = "ADA KANDIDAT ENTRY VALID" if valid_count > 0 else ("ADA KANDIDAT WATCHLIST / BELUM ADA ENTRY VALID" if ranked else "BELUM ADA ENTRY VALID")
    lines = [
        "MARKET SCANNER REPORT",
        f"Tanggal/Jam: {stamp(report.get('now_ms') or report.get('started_at_ms'))}",
        "",
        f"Total market diperiksa: {total_market}",
        f"Lolos filter likuiditas: {eligible_count}",
        f"Kandidat LONG: {long_wl}",
        f"Kandidat SHORT: {short_wl}",
        f"Kandidat analisis mendalam: {len(analyses)}",
        f"Entry valid: {valid_count}",
        "",
        "Top candidates:",
        *top_lines,
        "",
        f"Status:\n{status_label}",
    ]
    if valid_count == 0 and reason_lines:
        lines.extend([
            "",
            "Alasan belum ada entry:",
            *reason_lines[:4],
            "",
            "Kesimpulan:\nBelum ada entry valid. Scanner akan memeriksa kembali pada siklus berikutnya.",
        ])
    return "\n".join(lines)


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

    def _post_text(self, text, dedup_key=None):
        """Internal helper to post text to Telegram with optional persistent deduplication."""
        if not self.configured:
            return False, "Telegram belum lengkap di Secrets (token dan chat ID)."
        with self.lock:
            if self.clock() < self.cooldown_until:
                return False, "Telegram cooldown; tunggu scan berikutnya."
            if dedup_key is not None:
                try:
                    if not self.reserve(dedup_key):
                        return False, "Alert ini sudah dikirim sebelumnya; duplikat diblokir."
                except (OSError, sqlite3.Error):
                    return False, "Catatan deduplikasi Telegram tidak tersedia."
            try:
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text,
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
                    if dedup_key is not None:
                        self.record(dedup_key, "sent")
                    return True, "Alert Telegram terkirim."
                if code == 429:
                    try:
                        delay = float((body.get("parameters") or {}).get("retry_after", 60))
                        if not math.isfinite(delay):
                            delay = 60
                    except (ValueError, TypeError, AttributeError):
                        delay = 60
                    self.cooldown_until = self.clock() + max(1, delay)
                    if dedup_key is not None:
                        self.record(dedup_key, "retry", self.cooldown_until)
                    return False, "Telegram rate limit; mengikuti retry_after tanpa mengirim ulang sekarang."
                if code in (400, 401, 403):
                    self.cooldown_until = self.clock() + 300
                    if dedup_key is not None:
                        self.record(dedup_key, "retry", self.cooldown_until)
                    return False, f"Telegram menolak alert (HTTP {code}). Periksa bot/chat ID dan izin bot; jeda 5 menit."
                if dedup_key is not None:
                    self.record(dedup_key, "uncertain")
                return False, "Status pengiriman Telegram tidak pasti; tidak diulang agar tidak duplikat."
            except requests.RequestException:
                if dedup_key is not None:
                    try:
                        self.record(dedup_key, "uncertain")
                    except (OSError, sqlite3.Error):
                        pass
                return False, "Koneksi Telegram terputus; pengiriman tidak pasti."

    def send_test_message(self, custom_text=None):
        """Send an immediate verification ping to Telegram to test token & chat_id."""
        if not self.configured:
            return False, "Telegram belum lengkap di Secrets (TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID)."
        now_wib = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB")
        text = custom_text or (
            "✅ TEST KONEKSI TELEGRAM BERHASIL — FUTURES DESK v5.8.4\n"
            f"Waktu server: {now_wib}\n"
            "Multi-Stage Market Scanner + AI Dynamic Leverage & Precision Stop Loss Engine aktif.\n"
            "Interval Auto-Scan tersedia: 1m (60s), 5m (300s), 15m (900s).\n"
            "Jenis Alert aktif: Entry Signal (🚨), Reversal Watchlist (👀), Signal Cancelled (❌), & Market Scanner Report."
        )
        ok, msg = self._post_text(text)
        if ok and custom_text is None:
            return True, "Pesan tes Telegram berhasil dikirim! Periksa aplikasi Telegram Anda."
        return ok, msg

    def send_summary(self, report):
        """Alert 1 — Send Market Scanner Report."""
        if not self.configured:
            return "Telegram belum lengkap di Secrets."
        scan_id = report.get("scan_id") or str(report.get("now_ms") or int(self.clock()))
        ok, msg = self._post_text(format_scan_summary(report), dedup_key=f"REPORT:{scan_id}")
        return "Laporan MARKET SCANNER REPORT dikirim ke Telegram." if ok else msg

    def send_watchlist(self, data, candidate_info=None):
        """Alert 3 — Send Reversal Watchlist alert when M15 setup is promising (>= 70) but waiting for M5."""
        if not self.configured:
            return "Telegram belum lengkap di Secrets."
        r, snap = data["result"], data["snapshot"]
        close_ms = int(snap.bias.close_time.iloc[-1]) if len(snap.bias) else snap.now_ms
        key = f"WATCHLIST:{snap.symbol}:{snap.mode}:{close_ms}"
        ok, msg = self._post_text(format_watchlist_alert(data, candidate_info), dedup_key=key)
        return "Alert REVERSAL WATCHLIST terkirim." if ok else msg

    def send_cancellation(self, symbol, signal_id, reason):
        """Alert 4 — Send Signal Cancellation alert when a pending setup becomes invalid before fill."""
        if not self.configured:
            return "Telegram belum lengkap di Secrets."
        key = f"CANCEL:{signal_id}"
        ok, msg = self._post_text(format_cancellation_alert(symbol, signal_id, reason), dedup_key=key)
        return "Alert SIGNAL CANCELLED terkirim." if ok else msg

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

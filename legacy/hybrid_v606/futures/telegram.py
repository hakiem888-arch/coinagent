"""Approved BUY/SELL alerts, Stage-Based Watchlist (`EARLY WATCH` & `SETUP FORMING`), Market Scanner Report,
and Signal Cancellation with persistent atomic stage-aware dedup (`BUG-46`), Advisor-Only contextual AI support (`BUG-50` & `P0-2`),
Strategy F Hybrid Adaptive metadata (`P0-1` & `P0-5`), server-side RBAC authorization (`BUG-16`), and single `APP_VERSION` (`BUG-21`)."""
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
from .db_backend import connect_database
from .groq_rotation import combine
from .models import TIMEFRAMES, SPOT_SIMULATION_CHECKS
from .security import GLOBAL_MONITOR
from .version import APP_VERSION

LABELS = {"SCALPING": "SCALPER", "INTRADAY": "INTRADAY", "SWING": "SWING"}


def eligible(data, monotonic=time.monotonic):
    r, snap, ai = data["result"], data["snapshot"], data.get("ai")
    is_hybrid_or_advisor = (
        r.meta.get("strategy_engine") == "HYBRID_ADAPTIVE_V1"
        or bool((r.plan or {}).get("hybrid_adaptive_v1"))
        or bool(data.get("advisor_only"))
        or (
            r.status == "VALID"
            and data.get("rule_status") == "RULE_BASED_VALID"
            and bool(data.get("allow_paper_on_ai_unavailable", True))
        )
    )
    ai_ok_or_contextual = (
        combine(r, ai) == "VALID"
        or (r.status == "VALID" and is_hybrid_or_advisor)
    )
    if data.get("status") != "VALID" or not ai_ok_or_contextual or not r.plan:
        return False
    p = r.plan
    if p.get("instrument_kind", "USD_M_PERPETUAL") != snap.instrument_kind:
        return False
    if r.direction not in ("LONG", "SHORT") or p.get("direction") != r.direction:
        return False
    if p.get("mode") not in LABELS or p.get("symbol") != snap.symbol or p["mode"] != snap.mode:
        return False

    def passed(check):
        allowed = {"Likuidasi sesuai rencana", "Keamanan likuidasi (Isolated Margin)"}
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
    prefix = "PAPER:" if plan.get("scope") == "PAPER" else ""
    if plan.get("instrument_kind") == "SPOT_REFERENCE":
        prefix += "SPOT:"
    elif plan.get("instrument_kind") and plan.get("instrument_kind") != "USD_M_PERPETUAL":
        prefix += f"{plan['instrument_kind']}:"
    return prefix + ":".join(str(plan[k]) for k in ("symbol", "mode", "direction", "signal_close_ms"))


def watchlist_dedup_key(symbol, timeframe, candle_close_ms, direction, stage="EARLY_WATCH", strategy="F"):
    """BUG-46 & Section 15.2: Stage-aware deduplication key (`symbol + timeframe + candle_close + strategy + signal_direction + stage`).
    Allows a symbol to send an upgrade alert when transitioning from EARLY_WATCH -> SETUP_FORMING -> PAPER_ENTRY,
    while blocking duplicate messages for the same stage on the same candle.
    """
    st_norm = str(stage or "EARLY_WATCH").strip().upper().replace(" ", "_")
    sym_norm = str(symbol or "").strip().upper()
    tf_norm = str(timeframe or "15m").strip()
    dir_norm = str(direction or "LONG").strip().upper()
    strat_norm = str(strategy or "F").strip().upper()
    return f"WATCHLIST:{st_norm}:{sym_norm}:{tf_norm}:{int(candle_close_ms or 0)}:{strat_norm}:{dir_norm}"


def classify_watchlist_stage(m15_score, m5_confirmations=0, direction=None):
    """BUG-46 & Section 10.2: Determine whether a candidate qualifies for SETUP_FORMING or EARLY_WATCH."""
    score = float(m15_score or 0)
    m5_cnt = int(m5_confirmations or 0)
    if score >= 65 and direction in ("LONG", "SHORT") and m5_cnt >= 3:
        return "SETUP_FORMING"
    if score >= 55:
        return "EARLY_WATCH"
    return "NONE"


def format_alert(data):
    """Alert 2 (PAPER ENTRY) — Clean, emoji-organized Entry Signal for Telegram supporting Strategy F (1R/2R/3R 40/35/25)."""
    p, snap = data["result"].plan, data["snapshot"]
    n = lambda x: f"{x:,.8f}".rstrip("0").rstrip(".") if isinstance(x, (int, float)) else str(x)
    stamp = lambda x: datetime.fromtimestamp(x / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M WIB")
    side = "BUY / LONG" if p["direction"] == "LONG" else "SELL / SHORT"
    side_emoji = "🟢" if p["direction"] == "LONG" else "🔴"
    bias, execution = TIMEFRAMES[p["mode"]]
    reasons = [" ".join(x.split())[:110] for x in (data.get("ai") or {}).get("reasons", [])[:2]]
    if not reasons:
        reasons = ["Setup teknikal & R:R valid"]
    paper = p.get("scope") == "PAPER"
    kind = p.get("instrument_kind", "USD_M_PERPETUAL")
    spot = kind == "SPOT_REFERENCE"
    conf_score = p.get("setup_quality_score", p.get("confidence_score", 90))
    profile_tag = p.get("strategy_profile", "BALANCED")
    strat_id = p.get("strategy") or data["result"].meta.get("routed_strategy") or "F"
    strat_name = p.get("strategy_name") or data["result"].meta.get("routed_strategy_name") or "Hybrid Adaptive"
    regime_tag = p.get("m15_regime") or data["result"].meta.get("m15_regime") or "TREND"
    ai_status = data.get("advisory_status") or data.get("ai_status") or (data.get("ai") or {}).get("ai_status") or "AI_ADVISORY_OK"
    sl_pct = p.get("sl_distance_pct", (p["distance"] / p["entry"] * 100) if p["entry"] else 0.0)
    tp3 = p.get("tp3", p["tp2"] + (p["tp2"] - p["tp1"]))
    is_hybrid = bool(p.get("hybrid_adaptive_v1"))
    tp1_label = "+1R · 40%" if is_hybrid else "50%"
    tp2_label = "+2R · 35%" if is_hybrid else "25%"
    tp3_label = "+3R · 25% Runner" if is_hybrid else "4R Target Otomatis · Runner"
    provider_tag = (
        "Binance SPOT REFERENCE (SIMULASI SPOT — BUKAN FUTURES PERPETUAL)"
        if spot else (
            "Bybit USDT Perpetual (SIMULASI PAPER)"
            if kind == "BYBIT_LINEAR_PERPETUAL" else "Binance Futures USD-M (SIMULASI PAPER)"
        )
    )
    lines = [
        f"{side_emoji} " + ("PAPER | " if paper else "") + f"{side} | {LABELS[p['mode']]} | {p['symbol']} [PAPER ENTRY]",
        f"⭐ Confidence: {conf_score}/100 ({profile_tag}) · 🧭 Strategi {strat_id} ({strat_name}) · Regime M15: {regime_tag}",
        f"⚡ Lev (Simulasi): {p['leverage']}x · ⏱️ TF: {bias}/{execution}",
        "",
        "🎯 LEVEL HARGA",
        f"• Trigger entry: {n(p['entry_trigger'])}",
        f"• Limit entry: {n(p['entry'])}",
        f"• 🛑 SL: {n(p['sl'])} (-{sl_pct:.2f}% · " + ("Last spot" if spot else "Mark") + ")",
        f"• 💰 TP1: {n(p['tp1'])} ({tp1_label} · Q {n(p['tp1_qty'])})",
        f"• 💰 TP2: {n(p['tp2'])} ({tp2_label} · Q {n(p['tp2_qty'])})",
        f"• 🚀 TP3: {n(tp3)} ({tp3_label}: Q {n(p['runner_qty'])})",
        "",
        "💼 POSISI & RISIKO (SIMULASI)",
        f"• Qty: {n(p['qty'])} ({p['notional']:.1f} USDT) · Margin: {p['margin']:.1f} USDT",
        f"• Risiko: {p['risk']:.2f} USDT ({p['risk']/p['equity']:.2%}) · R:R 1:{p['net_rr_tp1']:.2f}",
        "",
        f"🤖 Catatan Advisor AI ({ai_status}): " + " | ".join(reasons),
        f"⏳ Exp: {stamp(p['expires_ms'])} · MENUNGGU TRIGGER",
        f"📡 {provider_tag} · v{APP_VERSION} · 🆔 {p['id'][:10]}",
    ]
    if spot:
        lines.append(
            "⚠️ Catatan Spot Reference: Harga spot tidak identik dengan Mark Price Futures; "
            "funding & leverage hanya asumsi simulasi; PnL tidak disamakan dengan kontrak perpetual."
        )
    return "\n".join(lines)


def format_watchlist_alert(data=None, candidate_info=None, stage=None):
    """Alert 3 — Stage-separated Watchlist Alert (`EARLY WATCH` vs `SETUP FORMING`, BUG-46 & Section 15.1)."""
    info = dict(candidate_info or {})
    r = data.get("result") if isinstance(data, dict) else None
    snap = data.get("snapshot") if isinstance(data, dict) else None
    symbol = (snap.symbol if snap is not None else info.get("symbol")) or "UNKNOWN"
    mode_label = LABELS.get(snap.mode, snap.mode) if snap is not None else info.get("screening_timeframe", "15m")
    inst_kind = (snap.instrument_kind if snap is not None else info.get("instrument_kind")) or "USD_M_PERPETUAL"
    direction = (r.direction if (r is not None and r.direction) else info.get("candidate_direction")) or "LONG"
    side_emoji = "🟢" if direction == "LONG" else "🔴"
    m15_score = int(info.get("m15_score") or (r.metrics.get("setup_quality_score", r.metrics.get("confidence_score", 60)) if r is not None else 60))
    m5_conf = int(
        (r.metrics.get("m5_confirmations_passed", 0) if r is not None else 0)
        or info.get("m5_confirmations_passed", 0)
    )
    regime = (r.meta.get("m15_regime") if r is not None else None) or info.get("m15_regime") or info.get("regime") or "TREND"
    strat_id = (r.meta.get("routed_strategy") if r is not None else None) or info.get("strategy_id") or "F"
    eff_stage = str(stage or classify_watchlist_stage(m15_score, m5_confirmations=m5_conf, direction=direction)).upper()
    if eff_stage not in ("EARLY_WATCH", "SETUP_FORMING"):
        eff_stage = "SETUP_FORMING" if (m15_score >= 65 and m5_conf >= 3) else "EARLY_WATCH"

    failed_checks = [c["Kriteria"] for c in (r.checks if r is not None else []) if c.get("Status") not in ("LULUS", "SIMULASI")]
    waiting_items = failed_checks[:3] if failed_checks else ["Trigger M5", "Konfirmasi Volume", "Retest Struktur"]

    if eff_stage == "SETUP_FORMING":
        return "\n".join([
            f"👀 REVERSAL WATCHLIST [SETUP FORMING] · {side_emoji} {symbol} ({direction})",
            f"⭐ Score: {m15_score}/100 · Regime M15: {regime} · Strategi: {strat_id} · Konfirmasi M5: {m5_conf}/5 · TF: {mode_label} · Sumber: {inst_kind}",
            f"⏳ Menunggu: {', '.join(waiting_items)}",
            "📌 Status: SETUP FORMING · WAITING FOR M5 CONFIRMATION (Arah teridentifikasi & minimal 3/5 konfirmasi M5 terpenuhi)",
        ])
    return "\n".join([
        f"👀 REVERSAL WATCHLIST [EARLY WATCH] · {side_emoji} {symbol} ({direction})",
        f"⭐ Score: {m15_score}/100 · Regime M15: {regime} · Strategi: {strat_id} · TF: {mode_label} · Sumber: {inst_kind}",
        f"⏳ Menunggu: {', '.join(waiting_items)}",
        "📌 Status: EARLY WATCH · WAITING FOR M5 CONFIRMATION (Setup awal M15 — Bukan instruksi entry)",
    ])


def format_cancellation_alert(symbol, signal_id, reason):
    """Alert 4 — Clean Signal Cancellation (❌ SIGNAL CANCELLED)."""
    return "\n".join([
        "❌ SIGNAL CANCELLED",
        f"• Symbol: {symbol}",
        f"• Signal ID: {signal_id}",
        f"• Alasan: {reason}",
    ])


def format_scan_summary(report):
    """Alert 1 & Section 10 — Clean, emoji-organized Market Scanner Report."""
    stamp = lambda x: datetime.fromtimestamp(x / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB") if x else "N/A"
    total_market = report.get("universe_count", 0)
    eligible_count = report.get("eligible_count", 0)
    long_wl = report.get("long_watchlist_count", len([x for x in report.get("shortlist", []) if x.get("watchlist_side") == "LONG"]))
    short_wl = report.get("short_watchlist_count", len([x for x in report.get("shortlist", []) if x.get("watchlist_side") == "SHORT"]))
    ranked = report.get("ranked") or report.get("selected") or report.get("shortlist", [])
    analyses = report.get("analyses", [])
    valid_count = sum(1 for d in analyses if d.get("status") == "VALID")
    kind = report.get("instrument_kind", "USD_M_PERPETUAL")
    eff_prof = report.get("effective_strategy_profile") or report.get("strategy_profile") or "BALANCED"

    top_lines = []
    for idx, item in enumerate(ranked[:5], 1):
        sym = item.get("symbol", "N/A")
        side = item.get("candidate_direction") or item.get("watchlist_side") or "WATCH"
        icon = "🟢" if side == "LONG" else ("🔴" if side == "SHORT" else "🔹")
        sc = item.get("m15_score") or round(float(item.get("screening_score", 0)) * 10, 1)
        reg = item.get("m15_regime") or item.get("regime") or ""
        strat_tag = item.get("strategy_id") or ""
        st_tag = item.get("watchlist_stage", "")
        meta_parts = [p for p in (reg, f"Strat {strat_tag}" if strat_tag else "", st_tag if st_tag and st_tag != "NONE" else "") if p]
        st_suffix = f" ({' · '.join(meta_parts)})" if meta_parts else ""
        top_lines.append(f"{idx}. {icon} {sym} — {side} — Score {sc}/100{st_suffix}")
    if not top_lines:
        top_lines.append("• Belum ada kandidat yang lolos penyaringan tahap ini.")

    reason_lines = []
    for d in analyses:
        if d.get("status") != "VALID":
            sym = d["snapshot"].symbol
            rs = "; ".join((d["result"].reasons + (d.get("ai") or {}).get("reasons", []))[:2]) or "Menunggu konfirmasi M5"
            reason_lines.append(f"• {sym}: {rs[:140]}")
    for err in report.get("errors", [])[:3]:
        reason_lines.append(f"• {err[:140]}")

    status_label = "✅ ADA KANDIDAT ENTRY VALID" if valid_count > 0 else ("👀 ADA WATCHLIST / BELUM ADA ENTRY VALID" if ranked else "⏳ BELUM ADA ENTRY VALID")
    lines = [
        f"📡 MARKET SCANNER REPORT v{APP_VERSION} ({kind} · {eff_prof})",
        f"🕒 Waktu: {stamp(report.get('now_ms') or report.get('started_at_ms'))}",
        "",
        "📊 Statistik Market:",
        f"• Total market diperiksa: {total_market}",
        f"• Lolos filter likuiditas: {eligible_count}",
        f"• Kandidat LONG: {long_wl} | Kandidat SHORT: {short_wl}",
        f"• Dianalisis mendalam: {len(analyses)} | Entry valid: {valid_count}",
        "",
        "🏆 Top Candidates:",
        *top_lines,
        "",
        f"📌 Status: {status_label}",
    ]
    if valid_count == 0 and reason_lines:
        lines.extend([
            "",
            "📝 Catatan Evaluasi:",
            *reason_lines[:4],
        ])
    return "\n".join(lines)


class TelegramAlerts:
    def __init__(self, token, chat_id, db, session=None, clock=time.time, monotonic=time.monotonic, security=None, pg_url=None, secret_getter=None):
        self.token, self.chat_id, self.db = str(token or "").strip(), str(chat_id or "").strip(), Path(db)
        self.session, self.clock, self.monotonic = session or requests.Session(), clock, monotonic
        self.target = hashlib.sha256((self.token.split(":")[0] + ":" + self.chat_id).encode()).hexdigest()
        self.lock, self.cooldown_until = threading.Lock(), 0.0
        self.telemetry = {
            "requests": 0,
            "success": 0,
            "errors": 0,
            "error_classes": {
                "HTTP_429": 0,
                "HTTP_4XX": 0,
                "HTTP_5XX": 0,
                "TIMEOUT": 0,
                "NETWORK_ERROR": 0,
                "AUTH_BLOCKED": 0,
            },
            "retry_count": 0,
            "duplicate_suppressed_count": 0,
            "last_delivery_status": "IDLE",
            "last_delivery_record": {
                "outbox_id": None,
                "http_status": None,
                "delivery_timestamp_ms": None,
                "retry_state": "NONE",
                "last_error": "",
            },
        }
        self.security = security
        self.session_token = None
        self.pg_url = pg_url
        self.secret_getter = secret_getter

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def _authorize(self, action="TELEGRAM_ALERT", security=None, session_token=None):
        """BUG-16 #2: Server-side authorization check for all outbound Telegram messages."""
        sec = security or self.security
        eff_token = session_token if session_token is not None else self.session_token
        if sec is not None:
            return sec.authorize_action(action, session_token=eff_token)
        return True, "ALLOWED"

    def connect(self):
        conn = connect_database(self.db, pg_url=self.pg_url, get_secret=self.secret_getter)
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

    def _post_text(self, text, dedup_key=None, action="TELEGRAM_ALERT", security=None, session_token=None):
        """Internal helper to post text to Telegram with server-side RBAC and persistent deduplication."""
        allowed, reason = self._authorize(action=action, security=security, session_token=session_token)
        if not allowed:
            return False, reason
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
            GLOBAL_MONITOR.increment("telegram_requests")
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
                    GLOBAL_MONITOR.increment("telegram_success")
                    if dedup_key is not None:
                        self.record(dedup_key, "sent")
                    return True, "Alert Telegram terkirim."
                GLOBAL_MONITOR.increment("telegram_errors")
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
                GLOBAL_MONITOR.increment("telegram_errors")
                if dedup_key is not None:
                    try:
                        self.record(dedup_key, "uncertain")
                    except (OSError, sqlite3.Error):
                        pass
                return False, "Koneksi Telegram terputus; pengiriman tidak pasti."

    def send_test_message(self, custom_text=None, security=None, session_token=None):
        """Send an immediate verification ping to Telegram (restricted to ADMIN)."""
        now_wib = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%d/%m/%Y %H:%M:%S WIB")
        text = custom_text or (
            f"✅ TEST KONEKSI TELEGRAM BERHASIL — FUTURES DESK v{APP_VERSION} (AUDIT COMPLIANT)\n"
            f"🕒 Waktu server: {now_wib}\n"
            "📡 Multi-Stage Market Scanner + Hybrid Adaptive Strategy F aktif.\n"
            "🛡️ Simulasi PAPER menggunakan validasi harga limit, depth, freshness 30s, dan RBAC Server-Side."
        )
        ok, msg = self._post_text(
            text,
            action="TELEGRAM_TEST",
            security=security,
            session_token=session_token,
        )
        if ok and custom_text is None:
            return True, "Pesan tes Telegram berhasil dikirim! Periksa aplikasi Telegram Anda."
        return ok, msg

    def send_summary(self, report, security=None, session_token=None):
        """Alert 1 — Send Market Scanner Report (BUG-31: single server-side authorization via _post_text)."""
        scan_id = report.get("scan_id") or str(report.get("now_ms") or int(self.clock()))
        ok, msg = self._post_text(
            format_scan_summary(report),
            dedup_key=f"REPORT:{scan_id}",
            action="TELEGRAM_ALERT",
            security=security,
            session_token=session_token,
        )
        return "Laporan MARKET SCANNER REPORT dikirim ke Telegram." if ok else msg

    def send_watchlist(self, data=None, candidate_info=None, stage=None, security=None, session_token=None):
        """Alert 3 — Send Stage-Aware Watchlist alert (`EARLY_WATCH` / `SETUP_FORMING`, BUG-46)."""
        info = dict(candidate_info or {})
        r = data.get("result") if isinstance(data, dict) else None
        snap = data.get("snapshot") if isinstance(data, dict) else None
        symbol = (snap.symbol if snap is not None else info.get("symbol")) or "UNKNOWN"
        timeframe = (TIMEFRAMES.get(snap.mode, ("15m", "5m"))[0] if snap is not None else info.get("screening_timeframe")) or "15m"
        close_ms = int(
            (snap.bias.close_time.iloc[-1] if (snap is not None and len(snap.bias)) else 0)
            or info.get("screen_time_ms")
            or (snap.now_ms if snap is not None else 0)
        )
        direction = (r.direction if (r is not None and r.direction) else info.get("candidate_direction")) or "LONG"
        strat_id = (r.meta.get("routed_strategy") if r is not None else None) or info.get("strategy_id") or "F"
        m15_score = int(info.get("m15_score") or (r.metrics.get("setup_quality_score", r.metrics.get("confidence_score", 60)) if r is not None else 60))
        m5_conf = int(
            (r.metrics.get("m5_confirmations_passed", 0) if r is not None else 0)
            or info.get("m5_confirmations_passed", 0)
        )
        eff_stage = str(stage or classify_watchlist_stage(m15_score, m5_confirmations=m5_conf, direction=direction)).upper()
        if eff_stage == "NONE":
            return "Diam · Skor M15 di bawah ambang EARLY WATCH (55)."
        key = watchlist_dedup_key(symbol, timeframe, close_ms, direction, stage=eff_stage, strategy=strat_id)
        ok, msg = self._post_text(
            format_watchlist_alert(data, info, stage=eff_stage),
            dedup_key=key,
            action="TELEGRAM_ALERT",
            security=security,
            session_token=session_token,
        )
        return f"Alert REVERSAL WATCHLIST ({eff_stage}) terkirim." if ok else msg

    def send_cancellation(self, symbol, signal_id, reason, security=None, session_token=None):
        """Alert 4 — Send Signal Cancellation alert (BUG-31: single server-side authorization via _post_text)."""
        key = f"CANCEL:{signal_id}"
        ok, msg = self._post_text(
            format_cancellation_alert(symbol, signal_id, reason),
            dedup_key=key,
            action="TELEGRAM_ALERT",
            security=security,
            session_token=session_token,
        )
        return "Alert SIGNAL CANCELLED terkirim." if ok else msg

    def send(self, data, security=None, session_token=None):
        allowed, auth_reason = self._authorize("TELEGRAM_ALERT", security=security, session_token=session_token)
        if not allowed:
            return auth_reason
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
                if not eligible(data, self.monotonic):
                    self.record(signal, "retry")
                    return "Diam · snapshot kedaluwarsa sebelum pengiriman."
                GLOBAL_MONITOR.increment("telegram_requests")
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
                    GLOBAL_MONITOR.increment("telegram_success")
                    self.record(signal, "sent")
                    return "Alert Telegram terkirim."
                GLOBAL_MONITOR.increment("telegram_errors")
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
                return "Status pengiriman Telegram tidak pasti; tidak diulang agar tidak duplikat."
            except requests.RequestException:
                GLOBAL_MONITOR.increment("telegram_errors")
                try:
                    self.record(signal, "uncertain")
                except (OSError, sqlite3.Error):
                    pass
                return "Koneksi Telegram terputus; pengiriman tidak pasti."



    def record_delivery_telemetry(self, status_text, ok=None):
        """P1-07: Single source of truth updating cumulative Telegram telemetry across all alert types."""
        txt = str(status_text or "")
        low = txt.lower()
        if "duplikat" in low:
            self.telemetry["duplicate_suppressed_count"] += 1
            self.telemetry["last_delivery_status"] = "DEDUPLICATED"
            return
        if "diam" in low or "belum lengkap" in low or "dinonaktifkan" in low:
            return
        self.telemetry["requests"] += 1
        if ok is True or "terkirim" in low:
            self.telemetry["success"] += 1
            self.telemetry["last_delivery_status"] = "SENT"
        else:
            self.telemetry["errors"] += 1
            if "429" in low or "rate limit" in low:
                self.telemetry["error_classes"]["HTTP_429"] += 1
                self.telemetry["retry_count"] += 1
            elif "koneksi" in low or "terputus" in low:
                self.telemetry["error_classes"]["NETWORK_ERROR"] += 1
                self.telemetry["retry_count"] += 1
            elif "401" in low or "403" in low or "400" in low:
                self.telemetry["error_classes"]["HTTP_4XX"] += 1
            else:
                self.telemetry["error_classes"]["HTTP_5XX"] += 1
            self.telemetry["last_delivery_status"] = redact_telegram_secrets(txt[:80], self.token, self.chat_id)

    def telegram_status(self, repo=None):
        """P1-07: Return structured Telegram reliability & outbox telemetry (zero secret leakage)."""
        outbox = {"pending": 0, "retry": 0, "sent": 0, "failed": 0}
        if repo is not None:
            try:
                from contextlib import closing as _closing
                with _closing(repo._connect()) as conn:
                    for st_name, cnt in conn.execute(
                        "SELECT delivery_state, COUNT(*) FROM predictive_alert_outbox GROUP BY delivery_state"
                    ).fetchall():
                        k = str(st_name or "").lower()
                        if k in outbox:
                            outbox[k] = int(cnt)
            except Exception:
                pass
        return {
            "configured": bool(self.configured),
            "requests": int(self.telemetry["requests"]),
            "success": int(self.telemetry["success"]),
            "errors": int(self.telemetry["errors"]),
            "error_classes": dict(self.telemetry["error_classes"]),
            "retry_count": int(self.telemetry["retry_count"]),
            "duplicate_suppressed_count": int(self.telemetry["duplicate_suppressed_count"]),
            "last_delivery_status": redact_telegram_secrets(self.telemetry["last_delivery_status"], self.token, self.chat_id),
            "outbox_status": outbox,
            "last_delivery_record": dict(self.telemetry.get("last_delivery_record") or {}),
        }

    def send_predictive_alert(self, signal, event_type=None, confirmed_plan=None, dedup_key=None, security=None, session_token=None):
        """Send a deduplicated Predictive Watchlist alert with safe retry and secret redaction."""
        allowed, auth_reason = self._authorize("TELEGRAM_ALERT", security=security, session_token=session_token)
        if not allowed:
            return {"ok": False, "status": "BLOCKED", "detail": redact_telegram_secrets(auth_reason, self.token, self.chat_id)}
        if not self.configured:
            return {"ok": False, "status": "UNCONFIGURED", "detail": "Telegram belum dikonfigurasi."}
        sig = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal or {})
        ev = str(event_type or sig.get("stage") or "PRE_WATCH").upper()
        key = dedup_key or f"PRED:{ev}:{sig.get('signal_id', 'unknown')}:{sig.get('symbol', '')}:{sig.get('direction', '')}"
        text = format_predictive_alert(sig, event_type=ev, confirmed_plan=confirmed_plan)
        with self.lock:
            if self.clock() < self.cooldown_until:
                return {"ok": False, "status": "RETRY", "detail": "Telegram cooldown aktif."}
            if not self.reserve(key):
                self.telemetry["duplicate_suppressed_count"] += 1
                self.telemetry["last_delivery_status"] = "DEDUPLICATED"
                return {"ok": False, "status": "DEDUPLICATED", "detail": "Alert duplikat diblokir."}
            try:
                GLOBAL_MONITOR.increment("telegram_requests")
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
                    timeout=(5, 10),
                    allow_redirects=False,
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
                    GLOBAL_MONITOR.increment("telegram_success")
                    self.telemetry["requests"] += 1
                    self.telemetry["success"] += 1
                    self.telemetry["last_delivery_status"] = "SENT"
                    self.record(key, "sent")
                    return {"ok": True, "status": "SENT", "detail": "Alert predictive terkirim.", "dedup_key": key}
                GLOBAL_MONITOR.increment("telegram_errors")
                self.telemetry["requests"] += 1
                self.telemetry["errors"] += 1
                self.telemetry["retry_count"] += 1
                code = body.get("error_code", response.status_code) if isinstance(body, dict) else response.status_code
                err_cls = "HTTP_429" if code == 429 else ("HTTP_4XX" if 400 <= int(code or 0) < 500 else "HTTP_5XX")
                self.telemetry["error_classes"][err_cls] = self.telemetry["error_classes"].get(err_cls, 0) + 1
                self.telemetry["last_delivery_status"] = f"RETRY:{err_cls}" 
                retry_after = 60.0
                if code == 429 and isinstance(body, dict):
                    try:
                        retry_after = max(1.0, float((body.get("parameters") or {}).get("retry_after", 60)))
                    except (ValueError, TypeError):
                        retry_after = 60.0
                self.cooldown_until = self.clock() + retry_after
                self.record(key, "retry", self.cooldown_until)
                return {
                    "ok": False,
                    "status": "RETRY",
                    "detail": redact_telegram_secrets(f"Telegram HTTP {code}; dijadwalkan retry.", self.token, self.chat_id),
                }
            except Exception as exc:
                GLOBAL_MONITOR.increment("telegram_errors")
                try:
                    self.record(key, "retry", self.clock() + 30.0)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "status": "RETRY",
                    "detail": redact_telegram_secrets(f"Koneksi Telegram gagal: {exc}", self.token, self.chat_id),
                }

    def flush_predictive_outbox(self, repo, max_items=10, now_ms=None, security=None, session_token=None):
        """Deliver pending deduplicated alerts from `predictive_alert_outbox` with safe retry."""
        stamp = int(now_ms if now_ms is not None else int(time.time() * 1000))
        items = repo.pending_outbox(now_ms=stamp, limit=max_items)
        sent = retried = skipped = 0
        for item in items:
            if not self.configured:
                skipped += 1
                continue
            with self.lock:
                if self.clock() < self.cooldown_until:
                    skipped += 1
                    continue
                try:
                    GLOBAL_MONITOR.increment("telegram_requests")
                    response = self.session.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": redact_telegram_secrets(item["message_text"], self.token, self.chat_id),
                            "link_preview_options": {"is_disabled": True},
                        },
                        timeout=(5, 10),
                        allow_redirects=False,
                    )
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
                        GLOBAL_MONITOR.increment("telegram_success")
                        repo.mark_outbox_sent(item["outbox_id"], now_ms=stamp)
                        self.telemetry["requests"] += 1
                        self.telemetry["success"] += 1
                        self.telemetry["last_delivery_status"] = "SENT"
                        self.telemetry["last_delivery_record"] = {
                            "outbox_id": item["outbox_id"],
                            "http_status": int(response.status_code),
                            "delivery_timestamp_ms": stamp,
                            "retry_state": "DELIVERED",
                            "last_error": "",
                        }
                        sent += 1
                    else:
                        GLOBAL_MONITOR.increment("telegram_errors")
                        err = redact_telegram_secrets(f"HTTP {response.status_code}", self.token, self.chat_id)
                        repo.mark_outbox_retry(item["outbox_id"], err, now_ms=stamp)
                        self.telemetry["requests"] += 1
                        self.telemetry["errors"] += 1
                        self.telemetry["retry_count"] += 1
                        self.telemetry["last_delivery_status"] = f"RETRY:{err}"
                        self.telemetry["last_delivery_record"] = {
                            "outbox_id": item["outbox_id"],
                            "http_status": int(getattr(response, "status_code", 0) or 0) if "response" in locals() else None,
                            "delivery_timestamp_ms": stamp,
                            "retry_state": "RETRY_SCHEDULED",
                            "last_error": err,
                        }
                        retried += 1
                except Exception as exc:
                    GLOBAL_MONITOR.increment("telegram_errors")
                    err = redact_telegram_secrets(str(exc), self.token, self.chat_id)
                    repo.mark_outbox_retry(item["outbox_id"], err, now_ms=stamp)
                    retried += 1
        return {"processed": len(items), "sent": sent, "retried": retried, "skipped": skipped}



def redact_telegram_secrets(text, token="", chat_id=""):
    """Ensure bot token, chat ID, API secrets, or bearer tokens are never leaked in logs or error strings."""
    out = str(text or "")
    import re as _re
    if token and len(str(token)) >= 6:
        out = out.replace(str(token), "[REDACTED_BOT_TOKEN]")
    if chat_id and len(str(chat_id)) >= 4:
        out = out.replace(str(chat_id), "[REDACTED_CHAT_ID]")
    out = _re.sub(r"bot[0-9]{6,15}:[A-Za-z0-9_-]{10,}", "bot[REDACTED_BOT_TOKEN]", out)
    out = _re.sub(r"gsk_[A-Za-z0-9]{10,}", "[REDACTED_GROQ_KEY]", out)
    return out


def format_predictive_alert(signal, event_type=None, confirmed_plan=None):
    """Format all 11 Predictive Watchlist v1.0 Telegram alert types (Section G.5)."""
    sig = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal or {})
    ev = str(event_type or sig.get("stage") or "PRE_WATCH").upper()
    direction = str(sig.get("direction") or "LONG").upper()
    side_word = "BUY" if direction == "LONG" else "SELL"
    symbol = str(sig.get("symbol") or "UNKNOWN").upper()
    stage_label = str(sig.get("stage") or ev).replace("_", " ")
    ev_label = ev.replace("_", " ")
    n = lambda x: f"{float(x):,.8f}".rstrip("0").rstrip(".") if isinstance(x, (int, float)) else str(x)

    header_map = {
        "PRE_WATCH": f"COINAGENTX — PRE-WATCH {side_word}",
        "EARLY_WATCH": f"COINAGENTX — EARLY WATCH {side_word}",
        "SETUP_FORMING": f"COINAGENTX — SETUP FORMING {side_word}",
        "CONFIRMED": f"COINAGENTX — CONFIRMED {side_word} (PAPER ONLY)",
        "PAPER_ENTRY": f"COINAGENTX — PAPER ENTRY {side_word}",
        "SIGNAL_STRENGTHENING": f"COINAGENTX — SIGNAL STRENGTHENING ({side_word})",
        "SIGNAL_WEAKENING": f"COINAGENTX — SIGNAL WEAKENING ({side_word})",
        "DIRECTION_CHANGED": f"COINAGENTX — DIRECTION CHANGED ({symbol})",
        "SIGNAL_INVALIDATED": f"COINAGENTX — SIGNAL INVALIDATED ({side_word})",
        "MISSED_ENTRY": f"COINAGENTX — MISSED ENTRY / ANTI-CHASE ({side_word})",
        "EXPIRED": f"COINAGENTX — SIGNAL EXPIRED ({side_word})",
    }
    header = header_map.get(ev, f"COINAGENTX — {ev_label} {side_word}")
    reasons = sig.get("reason_codes") or ["Setup teknikal awal terdeteksi"]
    reason_lines = [f"- {r}" for r in reasons[:5]]
    age_s = float(sig.get("signal_age_seconds") or 0.0)
    dist_pct = float(sig.get("distance_to_trigger_pct") or 0.0)
    tfs = "/".join(sig.get("source_timeframes") or ["1m", "5m", "15m"]).upper()

    lines = [
        header,
        "",
        f"Symbol: {symbol}",
        f"Direction: {direction}",
        f"Stage: {stage_label}",
        f"Early Score: {float(sig.get('early_score') or 0.0):.1f}/100 (Kualitas setup teknikal, bukan probabilitas profit)",
    ]
    if sig.get("setup_quality_score") is not None:
        lines.append(f"Setup Quality Score (Closed): {float(sig['setup_quality_score']):.1f}/100")
    lines.extend([
        f"M15 Regime: {sig.get('m15_regime', 'UNKNOWN')}",
        f"Strategy: {sig.get('strategy_id') or '-'}",
        f"Monitoring: {tfs}",
        f"Provider: {sig.get('provider', 'Binance Futures')} ({sig.get('instrument_kind', 'USD_M_PERPETUAL')})",
        f"Detected Price: {n(sig.get('detected_price', 0))}",
        f"Current Price: {n(sig.get('current_price', 0))}",
        f"Trigger Price: {n(sig.get('trigger_price', 0))}",
        f"Invalidation Price: {n(sig.get('invalidation_price', 0))}",
        f"Distance to Trigger: {dist_pct:.3f}%",
        f"Signal Age: {age_s:.0f}s",
        "Reasons:",
        *reason_lines,
        "",
    ])

    plan = confirmed_plan or sig.get("confirmed_plan")
    if ev in ("CONFIRMED", "PAPER_ENTRY") and isinstance(plan, dict) and plan:
        tp3 = plan.get("tp3", plan.get("tp2", 0))
        lines.extend([
            "RENCANA PAPER ENTRY (SIMULASI PAPER ONLY):",
            f"- Entry Trigger: {n(plan.get('entry_trigger', plan.get('entry', 0)))} | Limit: {n(plan.get('entry', 0))}",
            f"- Stop Loss: {n(plan.get('sl', 0))}",
            f"- TP1 (+1R 40%): {n(plan.get('tp1', 0))} | TP2 (+2R 35%): {n(plan.get('tp2', 0))} | TP3 (+3R 25%): {n(tp3)}",
            f"- Risk (Simulasi): {float(plan.get('risk', 0)):.2f} USDT | Leverage Simulasi: {plan.get('leverage', 2)}x",
            f"- Net R:R setelah biaya: 1:{float(plan.get('net_rr_tp1', 0)):.2f}",
            f"- Hard Gates: {'ALL_PASSED' if sig.get('hard_gates_passed', True) else 'CHECK_REQUIRED'}",
            "",
        ])

    status_notes = {
        "PRE_WATCH": "Peringatan dini saja. Belum ada konfirmasi entry. Jangan entry berdasarkan alert ini.",
        "EARLY_WATCH": "Peringatan dini saja (struktur awal). Belum ada konfirmasi entry. Jangan entry berdasarkan alert ini.",
        "SETUP_FORMING": "Setup M5 sedang terbentuk. Belum boleh entry sampai seluruh hard gates lolos.",
        "CONFIRMED": "Seluruh hard gates Hybrid Adaptive v1.1 lolos. Masuk jalur simulasi PAPER ONLY (bukan live order).",
        "PAPER_ENTRY": "Order simulasi PAPER telah dicatat pada PaperLedger. Tidak ada live order ke bursa.",
        "SIGNAL_STRENGTHENING": "Kualitas setup awal menguat. Tetap tunggu konfirmasi penuh sebelum PAPER_ENTRY.",
        "SIGNAL_WEAKENING": "Kualitas setup awal melemah. Pantau potensi invalidasi.",
        "DIRECTION_CHANGED": "Arah sinyal berubah; sinyal sebelumnya dibatalkan dan riwayat tetap disimpan.",
        "SIGNAL_INVALIDATED": "Level invalidasi tersentuh; sinyal dibatalkan.",
        "MISSED_ENTRY": "Harga sudah melewati toleransi entry (anti-chase). Dilarang mengejar harga.",
        "EXPIRED": "Masa berlaku (TTL) sinyal berakhir tanpa konfirmasi entry.",
    }
    lines.append("STATUS:")
    lines.append(status_notes.get(ev, "Peringatan status Predictive Watchlist (PAPER ONLY)."))
    return redact_telegram_secrets("\n".join(lines))

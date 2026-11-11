"""Futures Desk v5.8.5: Multi-Stage Market Scanner + AI Dynamic Leverage + Relaxed Mode (Self-Contained Fallback)."""
import hashlib
import inspect
import json
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.spot_market import SpotReferenceMarket, paper_db_path
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
import futures.scanner as _scanner_mod
from futures.scanner import ScanSchedule
from futures.telegram import TelegramAlerts, LABELS
from futures import history

SCAN_INTERVALS = (60, 300, 900)
SCAN_INTERVAL_LABELS = {
    60: "1m (60 detik)",
    300: "5m (300 detik)",
    900: "15m (900 detik)",
    30: "30 detik",
    45: "45 detik",
    90: "90 detik",
}

# Runtime compatibility patch if futures/scanner.py in GitHub is still the older version
if hasattr(_scanner_mod, "SCAN_INTERVALS") and 300 not in getattr(_scanner_mod, "SCAN_INTERVALS", ()):
    _scanner_mod.SCAN_INTERVALS = (30, 45, 60, 90, 300, 900)

ROOT = Path(__file__).resolve().parent
DB = paper_db_path(ROOT)
WIB = ZoneInfo("Asia/Jakarta")


def secret(name):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, WIB).strftime("%d/%m/%Y %H:%M:%S WIB") if ms else "Belum scan"


def send_direct_telegram(token, chat_id, text):
    """Direct Telegram sender inside app.py so it works even if futures/telegram.py on GitHub wasn't updated."""
    token = str(token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False, "Telegram belum lengkap di Secrets (TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID)."
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=(5, 10),
            allow_redirects=False,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code == 200 and body.get("ok") is True:
            return True, "Pesan berhasil dikirim ke Telegram! Silakan cek aplikasi Telegram Anda."
        desc = str(body.get("description") or f"HTTP {response.status_code}")
        return False, f"Telegram menolak pesan ({desc}). Pastikan Anda sudah klik Start pada bot dan TELEGRAM_CHAT_ID benar."
    except requests.RequestException:
        return False, "Koneksi ke server api.telegram.org gagal atau timeout."


def build_fallback_scanner_report(report):
    now_str = stamp(report.get("now_ms") or report.get("started_at_ms"))
    total_market = report.get("universe_count", 0)
    eligible_count = report.get("eligible_count", 0)
    shortlist_rows = report.get("shortlist", [])
    ranked = report.get("ranked") or report.get("selected") or shortlist_rows
    analyses = report.get("analyses", [])
    valid_count = sum(1 for d in analyses if d.get("status") == "VALID")

    top_lines = []
    for idx, item in enumerate(ranked[:3], 1):
        sym = item.get("symbol", "N/A")
        side = item.get("candidate_direction") or item.get("watchlist_side") or ("LONG" if float(item.get("change_pct", 0)) <= 0 else "SHORT")
        sc = item.get("m15_score") or round(float(item.get("screening_score", item.get("liquidity_score", 7.5))) * 10, 1)
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
        f"Tanggal/Jam: {now_str}",
        "",
        f"Total market diperiksa: {total_market}",
        f"Lolos filter likuiditas: {eligible_count}",
        f"Shortlist teratas: {len(shortlist_rows)}",
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


def build_fallback_watchlist_alert(data):
    r, snap = data["result"], data["snapshot"]
    direction = r.direction or "LONG"
    score = r.metrics.get("confidence_score", 78)
    failed_checks = [c["Kriteria"] for c in r.checks if c.get("Status") not in ("LULUS", "SIMULASI")]
    waiting_items = failed_checks[:4] if failed_checks else ["CHoCH M5", "Stoch RSI cross", "Volume >= 1.2x SMA20", "Retest area struktur"]
    waiting_lines = "\n".join(f"- {item}" for item in waiting_items)
    return "\n".join([
        "👀 REVERSAL WATCHLIST",
        "",
        f"Symbol: {snap.symbol}",
        f"Direction: {direction}",
        f"Score: {score}/100",
        "",
        "M15 setup:",
        f"Potensi setup {direction} pada {snap.symbol} ({LABELS.get(snap.mode, snap.mode)}) · RSI {r.metrics.get('rsi', 50):.1f} · ADX {r.metrics.get('adx', 20):.1f}",
        "",
        "Menunggu konfirmasi:",
        waiting_lines,
        "",
        "Status: WAITING FOR M5 CONFIRMATION",
    ])


@st.cache_resource(show_spinner=False)
def resources(signature, _keys, _token, _chat, _model, db, initial_equity):
    market, pool, calendar = SpotReferenceMarket(), GroqRotation(_keys), NewsCalendar()
    ledger = PaperLedger(db, initial_equity)
    alerts = TelegramAlerts(_token, _chat, db)
    return MarketScanner(market, pool, calendar, ledger, alerts, _model)


def configuration():
    keys = []
    try:
        keys = load_keys(secret)
    except ValueError as exc:
        st.sidebar.error(str(exc))
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    model = str(secret("GROQ_MODEL") or DEFAULT_MODEL)
    try:
        initial = float(secret("PAPER_INITIAL_EQUITY") or 1000)
    except (TypeError, ValueError):
        initial = 1000.0
    signature = hashlib.sha256(json.dumps([keys, str(token), str(chat), model]).encode()).hexdigest()
    engine = resources(signature, tuple(keys), token, chat, model, str(DB), initial)
    with st.sidebar:
        st.subheader("Scanner otomatis")
        auto = st.toggle("Auto-scan seluruh market", value=True)
        relaxed = st.toggle(
            "Relaxed Mode (Sinyal Adaptif & Reversal)",
            value=True,
            help="Melonggarkan filter kaku (menerima setup Reversal M15/M5 maupun Trend, rentang RSI 38–74 / 26–62, Volume >= 0,65x, jarak SL 0,5–4,5 ATR, toleransi trigger, dan fallback kalender berita otomatis) agar sinyal lebih aktif muncul.",
        )
        interval = st.selectbox(
            "Interval scan (detik)",
            SCAN_INTERVALS,
            index=0,
            format_func=lambda x: SCAN_INTERVAL_LABELS.get(x, f"{x} detik"),
        )
        candidates = st.selectbox("Kandidat analisis mendalam", (1, 2), index=1)
        alert = st.toggle("Alert Telegram PAPER BUY/SELL", value=True)
        watchlist_alert = st.toggle("Alert Telegram REVERSAL WATCHLIST", value=True,
                                    help="Kirim alert 👀 REVERSAL WATCHLIST jika kandidat memiliki skor M15 potensial tetapi masih menunggu konfirmasi entry M5.")
        summary_alert = st.toggle("Kirim MARKET SCANNER REPORT setiap siklus", value=False,
                                  help="Aktifkan jika ingin menerima laporan MARKET SCANNER REPORT pada setiap putaran scan meskipun belum ada entry valid.")
        st.session_state["ui_relaxed_mode"] = relaxed
        st.session_state["ui_watchlist_alert"] = watchlist_alert
        st.session_state["ui_summary_alert"] = summary_alert
        st.caption("Alur Multi-Stage: Screen All Markets → Filter → Rank (0–100) → Top 1–2 → Deep Analysis → M5 Confirmation → Risk Validation → Telegram Alert.")
        with st.expander("Asumsi simulasi dan filter awal"):
            volume = st.number_input("Minimum volume 24 jam (USDT)", min_value=1_000_000., value=10_000_000., step=1_000_000.)
            leverage = st.select_slider("Leverage paper", options=[1, 2, 3], value=2)
            risk = st.number_input("Risiko per paper trade (%)", min_value=.05, max_value=.5, value=.5, step=.05)
            fee = st.number_input("Asumsi fee taker (%)", min_value=0., max_value=1., value=.05, step=.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0., value=2., step=.5)
            fund = st.number_input("Cadangan biaya funding model (%)", min_value=0., value=.03, step=.01, format="%.4f")
            st.caption("Biaya simulasi, bukan fee aktual akun. Cadangan funding dibebankan saat entry. Likuidasi bursa tidak dimodelkan.")
        st.divider()
        st.caption(f"Groq: {len(keys)}/7 key dari Secrets · {model}")
        st.caption("Telegram: " + ("terkonfigurasi dari Secrets" if engine.alerts.configured else "token/chat ID belum lengkap di Secrets"))
        if st.button("Kirim Tes Alert Telegram", key="test_telegram_btn",
                     help="Mengirim pesan uji coba langsung ke chat Telegram Anda untuk memastikan Bot Token & Chat ID bekerja."):
            if hasattr(engine.alerts, "send_test_message"):
                ok, msg = engine.alerts.send_test_message()
            else:
                now_wib = datetime.now(WIB).strftime("%d/%m/%Y %H:%M:%S WIB")
                test_text = (
                    "✅ TEST KONEKSI TELEGRAM BERHASIL — FUTURES DESK v5.8.5\n"
                    f"Waktu server: {now_wib}\n"
                    "Multi-Stage Market Scanner + Relaxed Mode aktif.\n"
                    "Bot siap mengirimkan sinyal ke chat Telegram ini."
                )
                ok, msg = send_direct_telegram(token, chat, test_text)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
        if not keys:
            st.info("Tambahkan GROQ_API_KEY di Secrets; tanpa AI tidak ada sinyal yang disetujui.")
        with st.expander("Status Groq"):
            st.dataframe(pd.DataFrame(engine.pool.status()), hide_index=True)
        st.caption("Auto-scan memerlukan sesi aktif. Untuk 24/7 gunakan worker.py di server yang selalu hidup. Jangan jalankan dua scanner pada DB yang berbeda untuk chat yang sama.")
    cfg_kwargs = dict(candidates=candidates, min_volume=volume, fee=fee/100,
                      slippage_bps=slip, funding=fund/100, leverage=leverage,
                      risk_pct=risk, send_alerts=alert,
                      send_watchlist_alerts=watchlist_alert,
                      send_scan_summary=summary_alert,
                      relaxed_mode=relaxed,
                      interval=interval)
    accepted_fields = set(inspect.signature(ScannerConfig).parameters)
    if "relaxed_mode" not in accepted_fields and interval not in (30, 45, 60, 90):
        # Older market_scanner.py only allowed (30, 45, 60, 90)
        cfg_kwargs["interval"] = 60
    config = ScannerConfig(**{k: v for k, v in cfg_kwargs.items() if k in accepted_fields})
    return engine, config, auto


def freshness(report):
    started = report.get("started_at_ms") or report.get("now_ms", 0)
    if not started:
        st.caption("Belum ada percobaan scan.")
        return
    status = {"RUNNING": "Sedang berjalan", "FAILED": "Gagal mengambil/memproses data",
              "COMPLETED": "Selesai", "COMPLETED_WITH_NOTES": "Selesai dengan catatan"}.get(
                  report.get("scan_status"), "Selesai")
    st.caption(f"Percobaan scan terakhir: {stamp(started)} · {status} · "
               f"durasi {report.get('duration_seconds', 0):.2f} detik")
    if report.get("scan_id"):
        st.caption(f"ID scan: {report['scan_id']} · pemicu: {report.get('trigger', 'AUTO')}")
    if not report.get("now_ms"):
        st.info("Belum ada data pasar yang berhasil diambil pada percobaan ini.")
        return
    if report.get("universe_loaded") is False:
        st.info("Waktu bursa diterima, tetapi data seluruh market belum berhasil dimuat.")
        return
    captured = report.get("display_monotonic")
    age = report.get("display_server_age", 0) + time.monotonic() - captured if captured is not None else (time.time()*1000 - report.get("now_ms", 0))/1000
    st.caption(f"Waktu data pasar: {stamp(report['now_ms'])}")
    if age > 30:
        st.info("Snapshot hasil scan sudah lebih dari 30 detik. Tunggu scan baru; tabel ini merupakan catatan analisis.")


def paper_table(ledger, now_ms):
    s = ledger.summary(now_ms)
    cols = st.columns(4)
    for col, label, value in zip(cols, ["Ekuitas virtual", "PnL terealisasi", "PnL mengambang estimasi", "Posisi + pending"],
                                [f"{s['equity']:,.2f} USDT", f"{s['realized']:,.2f} USDT", f"{s['unrealized']:,.2f} USDT", f"{s['active']}/2"]):
        col.metric(label, value)
    st.caption("Jurnal default spot terpisah dari jurnal perpetual lama. Sumber setiap trade tercantum pada tabel.")
    trades = ledger.trades()
    rows = []
    for t in trades:
        p = t["plan"]
        rows.append({"Pair": p["symbol"], "Jenis": LABELS[p["mode"]], "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
                     "Confidence": p.get("confidence_score", 90), "Leverage": f"{p['leverage']}x",
                     "Sumber": p.get("instrument_kind", "USD_M_PERPETUAL"), "Status": t["status"], "Entry": p["entry"], "SL sekarang": t["stop"], "TP1": p["tp1"], "TP2": p["tp2"], "TP3": p.get("tp3", "-"),
                     "Q rencana": p["qty"], "Q terbuka": t["remaining_qty"], "PnL bersih virtual": round(t["realized_pnl"], 6),
                     "Fill virtual": stamp(p.get("fill_ms")), "TP1 terisi": bool(t["tp1_ms"]), "TP2 terisi": bool(t["tp2_ms"]),
                     "Waktu": stamp(t["updated_ms"]), "Kualitas": t["quality"], "Catatan": t["note"], "Telegram": t["telegram"]})
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(frame, hide_index=True, use_container_width=True)
        st.download_button("Unduh jurnal CSV", frame.to_csv(index=False).encode("utf-8-sig"), "paper_trades.csv", "text/csv")
    else:
        st.info("Belum ada sinyal PAPER yang lolos. WAIT/NO TRADE tidak membuka trade.")
    st.download_button("Unduh cadangan jurnal lengkap", ledger.export_json(), "paper_trading_backup.json", "application/json")
    st.caption("Tabel/CSV memuat 500 trade terbaru. Cadangan JSON memuat seluruh trade dan peristiwa jurnal.")
    st.caption("PENDING belum dianggap fill. Fill memakai quote yang diamati dan depth; tidak menyimpulkan fill dari sentuhan candle. Pemantauan polling dapat melewatkan pergerakan di antara scan.")
    st.caption("Gap data panjang: pending dibatalkan; posisi ditutup pada quote baru sebagai DATA_GAP_EXIT dan ditandai tidak kontinu. PnL virtual bukan hasil transaksi riil. SQLite dapat hilang saat disk hosting di-reset; simpan cadangan atau gunakan volume persisten.")


def dashboard(engine, config, auto):
    manual = st.button("Scan sekarang", type="primary", key="market_scan_now",
                       help="Memulai percobaan baru tanpa menunggu interval. Akses HTTP 451 dapat dicoba ulang secara manual.")
    schedule = st.session_state.setdefault("market_schedule", ScanSchedule())
    context = json.dumps(asdict(config), sort_keys=True)
    previous_report = st.session_state.get("market_report") or engine.last or {}
    if engine.last and engine.last.get("started_at_ms", 0) > previous_report.get("started_at_ms", 0):
        previous_report = engine.last
        st.session_state["market_report"] = previous_report
    paused = previous_report.get("market_error", {}).get("access_blocked", False)
    if (manual or not paused) and schedule.claim(time.monotonic(), auto, config.interval, context, manual):
        try:
            with st.spinner("Memantau paper trade, menyaring seluruh market (Multi-Stage), lalu menganalisis kandidat..."):
                previous = engine.last
                report = engine.run(config, manual=manual)
                if report is not previous and report.get("now_ms"):
                    report["display_monotonic"] = time.monotonic()
                    report["display_server_age"] = max(0, (time.time()*1000-report.get("now_ms", 0))/1000)
                    # Fallback summary/watchlist sender if older futures/market_scanner.py is still on server
                    if not hasattr(engine.alerts, "send_summary"):
                        token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
                        if st.session_state.get("ui_watchlist_alert"):
                            for d in report.get("analyses", []):
                                if d.get("status") != "VALID":
                                    ok_wl, msg_wl = send_direct_telegram(token, chat, build_fallback_watchlist_alert(d))
                                    d["telegram"] = "Alert REVERSAL WATCHLIST terkirim." if ok_wl else msg_wl
                        if st.session_state.get("ui_summary_alert"):
                            ok_sum, msg_sum = send_direct_telegram(token, chat, build_fallback_scanner_report(report))
                            report["summary_telegram"] = "Laporan MARKET SCANNER REPORT dikirim ke Telegram." if ok_sum else msg_sum
                st.session_state["market_report"] = report
        except (ValueError, sqlite3.Error, OSError) as exc:
            st.error("WAIT · siklus belum selesai: " + engine.pool.redact(str(exc))[:250])
        finally:
            schedule.finish(time.monotonic(), config.interval)
    report = st.session_state.get("market_report") or engine.last or {}
    paused = report.get("market_error", {}).get("access_blocked", False)
    if report.get("scan_notice"):
        st.info(report["scan_notice"])
    if report.get("summary_telegram"):
        st.caption("Telegram Ringkasan: " + str(report["summary_telegram"]))
    scanner_tab, paper_tab, history_tab, rules_tab = st.tabs(["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"])
    with scanner_tab:
        st.write("**SCREEN ALL MARKETS → FILTER → RANK (M15 0–100) → SELECT TOP 1–2 → DEEP ANALYSIS & M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT**")
        cols = st.columns(4)
        for col, label, value in zip(cols, ["Market aktif", "Lolos filter awal", "Shortlist", "Kandidat mendalam"],
                                    ([report.get("universe_count", 0), report.get("eligible_count", 0), len(report.get("shortlist", [])), len(report.get("selected", []))]
                                     if report and report.get("universe_loaded") is not False else ["N/A"] * 4)):
            col.metric(label, value)
        if report:
            freshness(report)
        interval_label = SCAN_INTERVAL_LABELS.get(config.interval, f"{config.interval} detik")
        mode_tag = "RELAXED MODE" if st.session_state.get("ui_relaxed_mode", True) else "STRICT MODE"
        auto_label = "DIJEDA (HTTP 451)" if auto and paused else (f"ON / {interval_label} ({mode_tag})" if auto else f"OFF ({mode_tag})")
        st.caption("Sesi aktif · auto-scan " + auto_label + ". Jadwal melambat jika API lambat; tidak ada penumpukan scan.")
        if paused:
            st.error("Akses data Binance Spot ditolak (HTTP 451). Percobaan scan telah dijalankan, tetapi server belum memperoleh data yang diperlukan.")
            st.info("Permintaan Binance berasal dari server tempat Streamlit berjalan. Periksa kelayakan akses wilayah/IP server "
                    "dengan Binance atau penyedia hosting. Mengganti key Groq/Telegram tidak memperbaiki akses ini. "
                    "Setelah akses diperbaiki, klik Scan sekarang. Auto-scan akan dilanjutkan bila toggle tetap ON.")
        for message in report.get("errors", [])[:8]:
            st.warning(message)
        with st.expander("Diagnostik scan dan koneksi"):
            st.caption("Sumber: Binance Spot · https://data-api.binance.vision · permintaan berasal dari server aplikasi.")
            diagnostic = {k: report.get(k) for k in ("scan_id", "scan_status", "trigger", "started_at_ms", "completed_at_ms", "now_ms", "duration_seconds", "universe_loaded", "instrument_kind", "market_error")}
            st.json(diagnostic)
            st.caption("Pada mesin yang menjalankan aplikasi, gunakan: python check_market_access.py. "
                       "Uji ini tidak memanggil AI, mengubah jurnal, atau mengirim Telegram.")
        if report.get("shortlist"):
            st.markdown("**Tahap 1 — Shortlist Seluruh Market (LONG & SHORT Watchlist)**")
            st.dataframe(pd.DataFrame(report["shortlist"]).rename(columns={"symbol":"Pair", "quote_volume":"Volume 24h (USDT)",
                "spread_bps":"Spread bps", "liquidity_score":"Skor likuiditas", "watchlist_side":"Watchlist"}), hide_index=True, use_container_width=True)
        if report.get("selected"):
            st.markdown("**Tahap 2 & 3 — Kandidat Terpilih untuk Analisis Mendalam**")
            sel_df = pd.DataFrame(report["selected"])
            cols_to_show = [c for c in ["symbol", "candidate_direction", "m15_score", "category", "screening_score", "activity", "momentum_atr_proxy"] if c in sel_df.columns]
            st.dataframe(sel_df[cols_to_show], hide_index=True, use_container_width=True)
        st.caption("Skor adalah kualitas setup teknikal berdasarkan aturan Multi-Stage Scanner (0–100), bukan jaminan probabilitas profit.")
        if report.get("worker_analyses"):
            st.dataframe(pd.DataFrame(report["worker_analyses"]), hide_index=True, use_container_width=True)
        for i, data in enumerate(report.get("analyses", [])):
            r, snap = data["result"], data["snapshot"]
            side = {"LONG":"BUY/LONG", "SHORT":"SELL/SHORT"}.get(r.direction, "WAIT")
            conf = r.metrics.get("confidence_score") or (data.get("candidate_info") or {}).get("m15_score", "-")
            stage_status = data.get("stage_status", data["status"])
            st.markdown(f"**{snap.symbol} · {LABELS[snap.mode]} · {side} · {data['status']} ({stage_status}) · Score/Conf: {conf}/100**")
            st.write(data.get("paper", ""))
            st.caption(data.get("telegram", ""))
            ai = data.get("ai") or {}
            for reason in (r.reasons + ai.get("reasons", []))[:5]:
                st.write("• " + reason)
            with st.expander(f"Perbandingan mode dan indikator — {snap.symbol}"):
                st.dataframe(pd.DataFrame(r.meta.get("mode_comparison", [])), hide_index=True, use_container_width=True)
                st.dataframe(pd.DataFrame(r.checks), hide_index=True, use_container_width=True)
                if r.plan:
                    p = r.plan
                    st.dataframe(pd.DataFrame([{
                        "SYMBOL": p["symbol"],
                        "DIRECTION": p["direction"],
                        "CONFIDENCE": f"{p.get('confidence_score', 90)}/100 ({p.get('confidence_calibration_status', 'UNCALIBRATED')})",
                        "MAX LEVERAGE": f"{p.get('max_allowed_leverage', p['leverage'])}x",
                        "SELECTED LEVERAGE": f"{p['leverage']}x",
                        "Trigger": p["entry_trigger"],
                        "ENTRY PRICE": p["entry"],
                        "TECHNICAL SL": p["sl"],
                        "SL DISTANCE": f"{p['distance']:.6g} ({p.get('sl_distance_pct', 0):.2f}%)",
                        "EST. MARGIN LOSS": f"{p.get('estimated_margin_loss_pct', 0):.2f}%",
                        "ACCOUNT RISK": f"{p['risk']:.2f} USDT",
                        "INITIAL MARGIN": f"{p['margin']:.2f} USDT",
                        "NOTIONAL": f"{p['notional']:.2f} USDT",
                        "TP1": p["tp1"],
                        "TP2": p["tp2"],
                        "TP3": p.get("tp3", "-"),
                        "EST. FEES": f"{p.get('estimated_fees_usdt', 0):.4f} USDT",
                        "NET RR TP1": f"{p['net_rr_tp1']:.2f}",
                        "Berlaku sampai": stamp(p["expires_ms"]),
                    }]), hide_index=True, use_container_width=True)
                if st.checkbox("Tampilkan grafik kandidat", key=f"chart_{snap.symbol}_{snap.mode}"):
                    from manual_app import draw_chart
                    draw_chart(snap, r.plan)
            st.download_button("Unduh analisis " + snap.symbol, json.dumps({"baseline":asdict(r), "ai":ai, "status":data["status"]}, ensure_ascii=False, indent=2),
                               f"{snap.symbol}_{snap.mode}_paper_analysis.json", "application/json", key=f"report_{i}")
    with paper_tab:
        paper_table(engine.ledger, report.get("now_ms") or int(time.time()*1000))
    with history_tab:
        records = history.load(DB)
        if records:
            st.dataframe(pd.DataFrame([{"Waktu":stamp(x["baseline"]["meta"]["time_ms"]), "Pair":x["baseline"]["meta"]["symbol"],
                "Mode":x["baseline"]["meta"]["mode"], "Status":x["final_status"], "Scope":x["baseline"]["meta"].get("scope", "MANUAL")} for x in records]), hide_index=True)
        else:
            st.info("Belum ada analisis kandidat tersimpan.")
    with rules_tab:
        st.markdown("**PRINSIP UTAMA:** `SCREEN ALL MARKETS → FILTER → RANK → SELECT TOP 1–2 → DEEP ANALYSIS → M5 CONFIRMATION → RISK VALIDATION → TELEGRAM ALERT`")
        st.markdown("**SCALPER:** bias 1H / eksekusi 5M · **INTRADAY:** 4H / 15M · **SWING:** 1D / 4H.")
        st.write("Ketiga mode dibandingkan dengan EMA, ADX, pullback, RSI, ATR, volume relatif, VWAP UTC, Bollinger Bands (20, 2), dan Stoch RSI (14, 14, 3, 3). Mode SWING tidak memakai syarat VWAP harian. AI dapat menolak; tidak boleh meloloskan aturan yang gagal.")
        st.write("Dilengkapi **Relaxed Mode (Sinyal Adaptif & Reversal)** yang dapat dinyalakan/dimatikan dari sidebar: mendukung baik setup Trend-Following maupun Reversal M15/M5, melonggarkan jendela RSI (38–74 LONG / 26–62 SHORT), Volume Relatif (≥0,65x), jarak SL struktural (0,5–4,5 ATR), toleransi trigger harga berjalan, serta fallback otomatis bila kalender CPI/FOMC lambat diakses.")
        st.write("Mendukung 4 jenis Alert Telegram: **1. Market Scanner Report**, **2. Entry Signal (🚨 BINANCE FUTURES SCALPING SIGNAL)**, **3. Setup Watchlist (👀 REVERSAL WATCHLIST)**, dan **4. Signal Cancellation (❌ SIGNAL CANCELLED)**.")
        st.download_button("Unduh masterprompt lengkap", (ROOT/"STRATEGY_RULES.txt").read_text(), "STRATEGY_RULES.txt", "text/plain")


def main():
    st.set_page_config(page_title="Futures Desk · v5.8.5", page_icon="📡", layout="wide")
    st.markdown("<style>.block-container{max-width:1280px;padding-top:2rem}[data-testid=stMetric]{border:1px solid #e4e8ef;border-radius:12px;padding:12px}</style>", unsafe_allow_html=True)
    st.caption("FUTURES DESK / 5.8.5 / PAPER / SPOT / MULTI-STAGE SCANNER & RELAXED MODE")
    st.title("Market scanner otomatis")
    st.caption("Tanpa input pair · multi-stage screening · relaxed mode adaptif · jurnal paper trading · interval 1m / 5m / 15m")
    st.info("SCANNER SPOT: harga, candle dan volume memakai sumber awal Binance Spot. Entry/SL/PnL adalah simulasi berbasis spot; Mark Price dan funding perpetual tidak tersedia.")
    engine, config, auto = configuration()
    st.fragment(run_every="5s")(dashboard)(engine, config, auto)


if __name__ == "__main__":
    main()

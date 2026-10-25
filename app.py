"""Futures Desk v5.8.2: original spot data source with reference-price PAPER."""
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.spot_market import SpotReferenceMarket, paper_db_path
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.scanner import SCAN_INTERVALS, ScanSchedule
from futures.telegram import TelegramAlerts, LABELS
from futures import history

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
        interval = st.selectbox("Interval scan (detik)", SCAN_INTERVALS, index=3)
        candidates = st.selectbox("Kandidat analisis mendalam", (1, 2), index=1)
        alert = st.toggle("Alert Telegram PAPER BUY/SELL", value=True)
        st.caption("Pair dan mode dipilih otomatis. Maksimal 10 koin di shortlist, 1–2 kandidat dianalisis pada tiga mode.")
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
        if not keys:
            st.info("Tambahkan GROQ_API_KEY di Secrets; tanpa AI tidak ada sinyal yang disetujui.")
        with st.expander("Status Groq"):
            st.dataframe(pd.DataFrame(engine.pool.status()), hide_index=True)
        st.caption("Auto-scan memerlukan sesi aktif. Untuk 24/7 gunakan worker.py di server yang selalu hidup. Jangan jalankan dua scanner pada DB yang berbeda untuk chat yang sama.")
    config = ScannerConfig(candidates=candidates, min_volume=volume, fee=fee/100,
                           slippage_bps=slip, funding=fund/100, leverage=leverage,
                           risk_pct=risk, send_alerts=alert, interval=interval)
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
                     "Sumber": p.get("instrument_kind", "USD_M_PERPETUAL"), "Status": t["status"], "Entry": p["entry"], "SL sekarang": t["stop"], "TP1": p["tp1"], "TP2": p["tp2"],
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
    # Another session may have restored access with a manual scan.
    if engine.last and engine.last.get("started_at_ms", 0) > previous_report.get("started_at_ms", 0):
        previous_report = engine.last
        st.session_state["market_report"] = previous_report
    paused = previous_report.get("market_error", {}).get("access_blocked", False)
    if (manual or not paused) and schedule.claim(time.monotonic(), auto, config.interval, context, manual):
        try:
            with st.spinner("Memantau paper trade, menyaring seluruh market, lalu menganalisis kandidat..."):
                previous = engine.last
                report = engine.run(config, manual=manual)
                if report is not previous and report.get("now_ms"):
                    report["display_monotonic"] = time.monotonic()
                    report["display_server_age"] = max(0, (time.time()*1000-report.get("now_ms", 0))/1000)
                st.session_state["market_report"] = report
        except (ValueError, sqlite3.Error, OSError) as exc:
            st.error("WAIT · siklus belum selesai: " + engine.pool.redact(str(exc))[:250])
        finally:
            schedule.finish(time.monotonic(), config.interval)
    report = st.session_state.get("market_report") or engine.last or {}
    paused = report.get("market_error", {}).get("access_blocked", False)
    if report.get("scan_notice"):
        st.info(report["scan_notice"])
    scanner_tab, paper_tab, history_tab, rules_tab = st.tabs(["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"])
    with scanner_tab:
        st.write("**Seluruh pair spot USDT aktif → maksimal 10 koin → 1–2 kandidat → tiga mode → AI → PAPER + Telegram**")
        cols = st.columns(4)
        for col, label, value in zip(cols, ["Market aktif", "Lolos filter awal", "Shortlist", "Kandidat mendalam"],
                                    ([report.get("universe_count", 0), report.get("eligible_count", 0), len(report.get("shortlist", [])), len(report.get("selected", []))]
                                     if report and report.get("universe_loaded") is not False else ["N/A"] * 4)):
            col.metric(label, value)
        if report:
            freshness(report)
        auto_label = "DIJEDA (HTTP 451)" if auto and paused else (f"ON / {config.interval} detik" if auto else "OFF")
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
            st.markdown("**Shortlist seluruh market**")
            st.dataframe(pd.DataFrame(report["shortlist"]).rename(columns={"symbol":"Pair", "quote_volume":"Volume 24h (USDT)",
                "spread_bps":"Spread bps", "liquidity_score":"Skor likuiditas"}), hide_index=True, use_container_width=True)
        if report.get("selected"):
            st.markdown("**Kandidat terpilih untuk analisis tiga mode**")
            st.dataframe(pd.DataFrame(report["selected"])[["symbol", "screening_score", "activity", "momentum_atr_proxy"]], hide_index=True)
        st.caption("Skor adalah urutan penyaringan aktivitas/likuiditas, bukan confidence, peluang menang, atau sinyal entry.")
        if report.get("worker_analyses"):
            st.dataframe(pd.DataFrame(report["worker_analyses"]), hide_index=True, use_container_width=True)
        for i, data in enumerate(report.get("analyses", [])):
            r, snap = data["result"], data["snapshot"]
            side = {"LONG":"BUY/LONG", "SHORT":"SELL/SHORT"}.get(r.direction, "WAIT")
            st.markdown(f"**{snap.symbol} · {LABELS[snap.mode]} · {side} · {data['status']} (PAPER)**")
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
                    st.dataframe(pd.DataFrame([{"Trigger":p["entry_trigger"], "Entry limit":p["entry"], "SL":p["sl"],
                        "TP1":p["tp1"], "TP2":p["tp2"], "Q":p["qty"], "Risiko USDT":p["risk"], "Berlaku sampai":stamp(p["expires_ms"])}]), hide_index=True)
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
        st.markdown("**SCALPER:** bias 1H / eksekusi 5M · **INTRADAY:** 4H / 15M · **SWING:** 1D / 4H.")
        st.write("Ketiga mode dibandingkan dengan EMA, ADX, pullback, RSI, ATR, volume relatif, dan VWAP UTC. Mode SWING tidak memakai syarat VWAP harian. AI dapat menolak; tidak boleh meloloskan aturan yang gagal.")
        st.write("Aturan teknis dan risiko tetap berlaku. PAPER memakai harga spot serta ledger virtual. Mark Price, deviasi Mark–Last, funding aktual dan likuidasi perpetual tidak diverifikasi. SL/PnL memakai Last spot; LONG/SHORT/leverage hanya simulasi.")
        st.write("Kalender CPI diambil dari BLS. Jadwal FOMC dari Federal Reserve memblokir seluruh hari rapat secara konservatif. Kalender gagal/di luar cakupan → WAIT. Peristiwa tak terjadwal tidak dapat dijamin tercakup.")
        st.download_button("Unduh masterprompt asli", (ROOT/"STRATEGY_RULES.txt").read_text(), "STRATEGY_RULES.txt", "text/plain")


def main():
    st.set_page_config(page_title="Futures Desk · v5.8.2", page_icon="📡", layout="wide")
    st.markdown("<style>.block-container{max-width:1280px;padding-top:2rem}[data-testid=stMetric]{border:1px solid #e4e8ef;border-radius:12px;padding:12px}</style>", unsafe_allow_html=True)
    st.caption("FUTURES DESK / 5.8.2 / PAPER / SPOT")
    st.title("Market scanner otomatis")
    st.caption("Tanpa input pair · shortlist market · keputusan lintas timeframe · jurnal paper trading")
    st.info("SCANNER SPOT: harga, candle dan volume memakai sumber awal Binance Spot. Entry/SL/PnL adalah simulasi berbasis spot; Mark Price dan funding perpetual tidak tersedia.")
    engine, config, auto = configuration()
    # One lightweight UI timer. ScanSchedule owns the 30/45/60/90 second
    # network cadence; no nested timer holds a stale report or misses a due tick.
    st.fragment(run_every="5s")(dashboard)(engine, config, auto)


if __name__ == "__main__":
    main()

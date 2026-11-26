"""CoinAgentX Simple Reversal — Streamlit entry point."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import os
import time

import pandas as pd
import streamlit as st

# Read optional endpoint & proxy overrides before simple_engine imports market adapters.
for _env_key in (
    "COINAGENT_BYBIT_BASE",
    "COINAGENT_BYBIT_PROXY",
    "COINAGENT_BITGET_BASE",
    "COINAGENT_BITGET_PROXY",
    "COINAGENT_MARKET_PROXY",
):
    try:
        _val = st.secrets.get(_env_key, "")
    except Exception:
        _val = os.getenv(_env_key, "")
    if _val:
        os.environ[_env_key] = str(_val).strip()

from simple_engine import APP_VERSION, ScanSettings, SimpleTradingEngine

WIB = ZoneInfo("Asia/Jakarta")


def secret_getter(name: str):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def get_engine():
    return SimpleTradingEngine(secret_getter=secret_getter)


def fmt_time(ms):
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, WIB).strftime("%d-%m-%Y %H:%M:%S WIB")


def render_telegram_analysis_table(engine):
    st.subheader("Tabel Hasil Analisis — Lolos Alert Telegram")

    all_passed = engine.store.latest_telegram_signals(limit=200, sent_only=False)
    sent_only_list = [s for s in all_passed if s["telegram_sent"]]
    long_cnt = sum(1 for s in all_passed if s["direction"] == "LONG")
    short_cnt = sum(1 for s in all_passed if s["direction"] == "SHORT")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Lolos Filter Analisis", len(all_passed))
    m2.metric("Terkirim ke Telegram", len(sent_only_list))
    m3.metric("Sinyal LONG", long_cnt)
    m4.metric("Sinyal SHORT", short_cnt)

    f1, f2, f3 = st.columns([1.4, 1.0, 1.2])
    only_sent = f1.toggle("Hanya yang berstatus 'Alert Telegram terkirim'", value=False)
    dir_filter = f2.selectbox("Filter Arah", ["Semua", "LONG", "SHORT"], index=0)
    pair_query = f3.text_input("Cari Pair", placeholder="Contoh: BTCUSDT").strip().upper()

    filtered = [
        s for s in all_passed
        if (not only_sent or s["telegram_sent"])
        and (dir_filter == "Semua" or s["direction"] == dir_filter)
        and (not pair_query or pair_query in str(s["symbol"]).upper())
    ]

    if not filtered:
        st.info("Belum ada data analisis yang cocok dengan filter alert Telegram.")
        return

    rows = []
    for s in filtered:
        rows.append({
            "Waktu Alert (WIB)": fmt_time(s["created_ms"]),
            "Pair": s["symbol"],
            "Arah": "🟢 BUY / LONG" if s["direction"] == "LONG" else "🔴 SELL / SHORT",
            "Setup BB M15": f"Touch {s['touch_band']}",
            "RSI(14) M5": f"{s['rsi_previous']:.2f} → {s['rsi_current']:.2f}",
            "Delta RSI": f"{s['delta_rsi']:+.2f}",
            "Entry": round(float(s["entry"]), 6),
            "Stop Loss": round(float(s["stop"]), 6),
            "Take Profit (2R)": round(float(s["target"]), 6),
            "Risiko SL (%)": round(float(s["sl_pct"]), 3),
            "Target TP (%)": round(float(s["tp_pct"]), 3),
            "R:R": f"1 : {s['rr_ratio']:.2f}",
            "Candle M5 (WIB)": fmt_time(s["m5_close_ms"]),
            "Sumber Market": s["provider"],
            "Status Paper": s["paper_status"],
            "Status Alert Tele": "✅ Terkirim" if s["telegram_sent"] else s["alert_status"],
        })

    df_tele = pd.DataFrame(rows)
    st.dataframe(df_tele, use_container_width=True, hide_index=True)

    st.download_button(
        label="Download Tabel Analisis (CSV)",
        data=df_tele.to_csv(index=False).encode("utf-8"),
        file_name=f"coinagentx_alert_telegram_{datetime.now(WIB).strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )


def render_dashboard(engine, settings: ScanSettings, auto_scan: bool):
    st.title("CoinAgentX · Simple Reversal")
    st.caption(f"Versi {APP_VERSION} · Futures Perpetual · Paper Trading Only")
    st.warning("Mode simulasi saja. Aplikasi tidak mengirim order ke Binance, Bybit, atau Bitget.")

    manual_scan = st.button("Scan sekarang", type="primary", use_container_width=False)
    last_run = engine.store.latest_run()
    last_tick = float(st.session_state.get("simple_last_scan_monotonic", 0.0))
    due = auto_scan and (not last_run or (time.monotonic() - last_tick >= settings.interval_seconds - 1))

    if manual_scan or due:
        with st.spinner("Memindai market Futures dan memeriksa Bollinger Bands + RSI…"):
            report = engine.scan(settings, force=manual_scan)
        st.session_state["simple_last_scan_monotonic"] = time.monotonic()
        st.session_state["simple_last_report"] = report
        if report.get("status") == "FAILED":
            st.error(report.get("error", "Scan gagal"))
        elif report.get("status") == "BUSY":
            st.info(report.get("note", "Scanner sedang berjalan."))
        else:
            found = len(report.get("signals") or [])
            st.success(f"Scan selesai · {report.get('scanned', 0)} pair diperiksa · {found} sinyal baru.")

    report = st.session_state.get("simple_last_report") or {}
    last_run = engine.store.latest_run() or {}
    provider = report.get("provider") or last_run.get("provider") or "Belum terhubung"
    kind = report.get("instrument_kind") or last_run.get("instrument_kind") or "—"
    summary = engine.store.paper_summary(settings.initial_equity)
    trades = engine.store.paper_trades(100)
    active = [t for t in trades if t["status"] == "OPEN"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sumber market", provider)
    c2.metric("Instrumen", kind.replace("_", " "))
    c3.metric("Posisi paper aktif", summary["active"])
    c4.metric("Ekuitas paper", f"{summary['equity']:,.2f} USDT")

    render_telegram_analysis_table(engine)

    st.subheader("Paper trading aktif")
    if active:
        st.dataframe(pd.DataFrame([{
            "Pair": t["symbol"],
            "Arah": t["direction"],
            "Entry": t["entry"],
            "SL": t["stop"],
            "TP": t["target"],
            "Quantity": t["quantity"],
            "Leverage": f"{t['leverage']}x",
            "Mark": t["mark_price"],
            "Unrealized PnL": (t["mark_price"] - t["entry"]) * t["quantity"] * (1 if t["direction"] == "LONG" else -1),
        } for t in active]), use_container_width=True, hide_index=True)
    else:
        st.caption("Tidak ada posisi paper yang sedang terbuka.")

    with st.expander("Riwayat paper trading"):
        closed = [t for t in trades if t["status"] != "OPEN"]
        if closed:
            st.dataframe(pd.DataFrame([{
                "Pair": t["symbol"], "Arah": t["direction"], "Entry": t["entry"],
                "Exit": t["exit_price"], "Status": t["status"], "PnL (USDT)": t["pnl_usdt"],
                "Waktu buka": fmt_time(t["opened_ms"]), "Waktu tutup": fmt_time(t["closed_ms"]),
            } for t in closed]), use_container_width=True, hide_index=True)
        else:
            st.caption("Belum ada posisi yang ditutup.")

    with st.expander("Status scanner & diagnosa provider"):
        st.write({
            "Status scan terakhir": last_run.get("status", "Belum ada scan"),
            "Waktu scan terakhir": fmt_time(last_run.get("at_ms")),
            "Pair diperiksa": last_run.get("scanned", 0),
            "Sinyal baru": last_run.get("signals", 0),
            "Pair gagal dibaca": last_run.get("failed", 0),
            "Catatan": last_run.get("note", ""),
            "Failover aktif": getattr(engine.market, "failover_reason", None) or "Tidak ada",
            "Telegram": "Terkonfigurasi" if engine.telegram_configured else "Belum dikonfigurasi",
            "Database": "PostgreSQL" if engine.store.pg_url else "SQLite",
        })
        if st.button("Cek koneksi provider (Binance / Bybit / Bitget)"):
            diag = engine.market.verify_futures_providers()
            diag_rows = []
            for tier_key in ("primary", "fallback", "tertiary"):
                if tier_key in diag:
                    d = diag[tier_key]
                    diag_rows.append({
                        "Tier": tier_key.upper(),
                        "Provider": d["provider"],
                        "Instrumen": d["instrument_kind"],
                        "Status": "✅ OK" if d["ok"] else "❌ Gagal",
                        "HTTP": d["http_status"] or "—",
                        "Kelas Error": d["error_class"] or "—",
                        "Detail": d["error_detail"] or "Normal",
                    })
            st.dataframe(pd.DataFrame(diag_rows), use_container_width=True, hide_index=True)
        if report.get("errors"):
            st.write("Kesalahan pair terakhir:")
            for error in report["errors"][:8]:
                st.caption(error)

    with st.expander("Logika strategi"):
        st.markdown(
            "**Hanya dua indikator:** Bollinger Bands (20, 2) pada M15 dan RSI (14) pada M5.\n\n"
            "**LONG:** candle tertutup M15 menyentuh Bollinger bawah; candle tertutup M5 berikutnya memiliki "
            "high dan close lebih tinggi dari candle sebelumnya serta bullish; RSI M5 sebelumnya ≤ 30 dan RSI "
            "terbaru naik.\n\n"
            "**SHORT:** candle tertutup M15 menyentuh Bollinger atas; candle tertutup M5 berikutnya memiliki "
            "low dan close lebih rendah dari candle sebelumnya serta bearish; RSI M5 sebelumnya ≥ 70 dan RSI "
            "terbaru turun.\n\n"
            "Paper entry memakai close candle konfirmasi. SL memakai ekstrem dua candle konfirmasi M5 dan TP "
            "ditetapkan pada 2R. Jika satu candle menyentuh SL dan TP sekaligus, simulasi menganggap SL terjadi "
            "lebih dahulu agar hasil tidak terlalu optimistis."
        )


def main():
    st.set_page_config(page_title="CoinAgentX Simple Reversal", page_icon="📈", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1350px;padding-top:1.4rem}"
        "[data-testid=stMetric]{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:10px}</style>",
        unsafe_allow_html=True,
    )
    engine = get_engine()

    st.sidebar.title("Pengaturan sederhana")
    auto_scan = st.sidebar.toggle("Auto scan", value=True)
    scan_interval = st.sidebar.selectbox("Interval scan", [60, 120, 300], index=0,
                                         format_func=lambda x: f"{x // 60} menit" if x >= 60 else f"{x} detik")
    symbols = st.sidebar.select_slider("Jumlah pair", options=[10, 20, 30, 50, 75, 100], value=50)
    saved_equity = engine.store.get_initial_equity()
    initial_equity = st.sidebar.number_input("Modal awal paper (USDT)", min_value=50.0, max_value=1_000_000.0,
                                              value=float(saved_equity or 1000.0), step=50.0, disabled=saved_equity is not None)
    if saved_equity is not None:
        st.sidebar.caption("Modal awal sudah tersimpan di database dan tidak berubah saat aplikasi refresh.")
    risk_pct = st.sidebar.slider("Risiko paper per posisi (%)", min_value=0.1, max_value=2.0, value=0.5, step=0.1)
    leverage = st.sidebar.selectbox("Leverage simulasi", [1, 2, 3, 5, 10, 20], index=1)
    send_telegram = st.sidebar.toggle("Kirim alert Telegram", value=True)
    st.sidebar.caption("Token bot, Chat ID, dan opsional Proxy diatur melalui Streamlit Secrets atau environment variables.")

    settings = ScanSettings(
        symbols=int(symbols), interval_seconds=int(scan_interval), initial_equity=float(initial_equity),
        risk_pct=float(risk_pct), leverage=int(leverage), send_telegram=bool(send_telegram),
    )
    fragment = st.fragment(run_every=f"{scan_interval}s" if auto_scan else None)
    fragment(render_dashboard)(engine, settings, auto_scan)


if __name__ == "__main__":
    main()

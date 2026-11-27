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

from simple_engine import (
    APP_VERSION,
    ScanSettings,
    SimpleTradingEngine,
    describe_trailing_state,
)

WIB = ZoneInfo("Asia/Jakarta")
UI_ENGINE_KEY = f"{APP_VERSION}-supabase-persist-v5"


def secret_getter(name: str):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def get_engine(cache_key: str):
    """Cache engine keyed by version so Streamlit Cloud upgrades never reuse a stale instance."""
    _ = cache_key
    return SimpleTradingEngine(secret_getter=secret_getter)


def fmt_time(ms):
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, WIB).strftime("%d-%m-%Y %H:%M:%S WIB")


def load_telegram_signals(engine, limit: int = 200) -> list[dict]:
    """Load enriched signals for the Telegram analysis table with fallback for older store instances."""
    store = engine.store
    if hasattr(store, "latest_telegram_signals"):
        return store.latest_telegram_signals(limit=limit, sent_only=False)

    rows = store.latest_signals(limit=max(int(limit), 1))
    enriched = []
    for s in rows:
        alert_status = str(s.get("alert_status") or "")
        is_sent = alert_status == "Alert Telegram terkirim"
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


def render_telegram_analysis_table(engine):
    st.subheader("Tabel Hasil Analisis — Lolos Alert Telegram")

    all_passed = load_telegram_signals(engine, limit=200)
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
        target_val = float(s["target"])
        rows.append({
            "Waktu Alert (WIB)": fmt_time(s["created_ms"]),
            "Pair": s["symbol"],
            "Arah": "🟢 BUY / LONG" if s["direction"] == "LONG" else "🔴 SELL / SHORT",
            "Setup BB M15": f"Touch {s['touch_band']}",
            "RSI(14) M5": f"{s['rsi_previous']:.2f} → {s['rsi_current']:.2f}",
            "Delta RSI": f"{s['delta_rsi']:+.2f}",
            "Entry": round(float(s["entry"]), 6),
            "SL Awal": round(float(s["stop"]), 6),
            "Take Profit": round(target_val, 6) if target_val > 0 else "Open (Trail)",
            "Risiko SL (%)": round(float(s["sl_pct"]), 3),
            "Target TP (%)": round(float(s["tp_pct"]), 3) if target_val > 0 else "Trailing",
            "R:R": f"1 : {s['rr_ratio']:.2f}" if target_val > 0 else "Trailing",
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
    trail_badge = "Step R-Multiple Trailing ON" if settings.trailing_stop_enabled else "Fixed SL"
    tp_badge = f"TP {settings.tp_multiple:g}R" if settings.tp_multiple > 0 else "TP Open (Trailing)"
    st.caption(f"Versi {APP_VERSION} · Futures Perpetual · {trail_badge} · {tp_badge} · Paper Trading Only")
    st.warning("Mode simulasi saja. Aplikasi tidak mengirim order ke Binance, Bybit, atau Bitget.")
    storage_label = getattr(engine.store, "storage_label", "PostgreSQL" if engine.store.pg_url else "SQLite")
    if not getattr(engine.store, "is_persistent_cloud", bool(engine.store.pg_url)):
        st.error(
            "⚠️ Penyimpanan masih memakai SQLite lokal (sementara). Catatan paper trading akan hilang saat "
            "update/reboot Streamlit Cloud. Isi `DATABASE_URL` (Supabase Transaction Pooler) atau "
            "`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` di Streamlit Secrets."
        )
    elif getattr(engine.store, "restored_from_supabase", False):
        st.info(f"☁️ {engine.store.last_supabase_sync_note}")

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

    st.subheader("Paper trading aktif (Step R-Multiple Trailing Stop)")
    if active:
        active_rows = []
        for t in active:
            tstate = describe_trailing_state(t)
            upnl = (t["mark_price"] - t["entry"]) * t["quantity"] * (1 if t["direction"] == "LONG" else -1)
            active_rows.append({
                "Pair": t["symbol"],
                "Arah": "🟢 LONG" if t["direction"] == "LONG" else "🔴 SHORT",
                "Entry": round(float(t["entry"]), 6),
                "SL Awal": round(float(tstate["initial_stop"]), 6),
                "SL Aktif (Trail)": round(float(t["stop"]), 6),
                "Status Trailing": tstate["trail_stage"],
                "TP": round(float(t["target"]), 6) if float(t["target"] or 0) > 0 else "Open (Trail)",
                "Mark": round(float(t["mark_price"]), 6),
                "Profit (R)": f"{tstate['current_r']:+.2f}R",
                "Leverage": f"{t['leverage']}x",
                "Unrealized PnL (USDT)": round(upnl, 4),
            })
        st.dataframe(pd.DataFrame(active_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("Tidak ada posisi paper yang sedang terbuka.")

    with st.expander("Riwayat paper trading"):
        closed = [t for t in trades if t["status"] != "OPEN"]
        if closed:
            st.dataframe(pd.DataFrame([{
                "Pair": t["symbol"],
                "Arah": t["direction"],
                "Entry": round(float(t["entry"]), 6),
                "SL Terakhir": round(float(t["stop"]), 6),
                "Exit": round(float(t["exit_price"]), 6) if t["exit_price"] is not None else "—",
                "Status": t["status"],
                "PnL (USDT)": round(float(t["pnl_usdt"]), 4),
                "Waktu buka": fmt_time(t["opened_ms"]),
                "Waktu tutup": fmt_time(t["closed_ms"]),
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
            "Trailing Stop": "Step R-Multiple (Aktif)" if settings.trailing_stop_enabled else "Nonaktif",
            "Target TP": f"{settings.tp_multiple:g}R" if settings.tp_multiple > 0 else "Open (Trailing Only)",
            "Telegram": "Terkonfigurasi" if engine.telegram_configured else "Belum dikonfigurasi",
            "Database": getattr(engine.store, "storage_label", "PostgreSQL" if engine.store.pg_url else "SQLite"),
            "Sinkronisasi Supabase": getattr(engine.store, "last_supabase_sync_note", "") or ("Aktif (PostgreSQL langsung)" if engine.store.pg_url else "—"),
        })
        b1, b2 = st.columns(2)
        check_prov = b1.button("Cek koneksi provider (Binance / Bybit / Bitget)")
        check_db = b2.button("Cek koneksi & simpan Supabase")
        if check_db:
            if hasattr(engine.store, "verify_persistence_health"):
                db_health = engine.store.verify_persistence_health()
                if db_health.get("ok"):
                    st.success(f"✅ {db_health['label']} — {db_health['detail']}")
                else:
                    st.error(f"❌ {db_health['label']} — {db_health['detail']}")
        if check_prov:
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

    with st.expander("Logika strategi & Trailing Stop (Opsi 1)"):
        st.markdown(
            "**Hanya dua indikator:** Bollinger Bands (20, 2) pada M15 dan RSI (14) pada M5.\n\n"
            "**LONG:** candle tertutup M15 menyentuh Bollinger bawah; candle tertutup M5 berikutnya memiliki "
            "high dan close lebih tinggi dari candle sebelumnya serta bullish; RSI M5 sebelumnya ≤ 30 dan RSI "
            "terbaru naik.\n\n"
            "**SHORT:** candle tertutup M15 menyentuh Bollinger atas; candle tertutup M5 berikutnya memiliki "
            "low dan close lebih rendah dari candle sebelumnya serta bearish; RSI M5 sebelumnya ≥ 70 dan RSI "
            "terbaru turun.\n\n"
            "**Trailing Stop — Step R-Multiple (Opsi 1):**\n"
            "- Profit menyentuh **+1.0R** → SL digeser ke **Break-Even (+0.05R)** untuk menutup estimasi fee.\n"
            "- Profit menyentuh **+1.5R** → SL digeser mengunci **+0.50R**.\n"
            "- Jika TP diperluas (**3R / 4R / Open**): profit menyentuh **+2.0R** → SL mengunci **+1.20R**, "
            "dan naik **+0.50R** setiap kenaikan harga **+0.50R** berikutnya.\n"
            "- Evaluasi dilakukan secara konservatif per candle M5 tertutup: SL aktif dicek terlebih dahulu "
            "sebelum menaikkan tangga trailing agar tidak ada bias optimistis di dalam satu candle."
        )


def main():
    st.set_page_config(page_title="CoinAgentX Simple Reversal", page_icon="📈", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1350px;padding-top:1.4rem}"
        "[data-testid=stMetric]{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:10px}</style>",
        unsafe_allow_html=True,
    )
    engine = get_engine(UI_ENGINE_KEY)

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

    st.sidebar.divider()
    st.sidebar.markdown("**Trailing Stop (Opsi 1)**")
    trailing_enabled = st.sidebar.toggle("Step R-Multiple Trailing Stop", value=True)
    tp_options = {
        "2R (Standar)": 2.0,
        "3R (Diperluas)": 3.0,
        "4R (Trend)": 4.0,
        "Open (Trailing Only)": 0.0,
    }
    tp_label = st.sidebar.selectbox("Target Take Profit", list(tp_options.keys()), index=0)
    tp_multiple = tp_options[tp_label]
    if not trailing_enabled and tp_multiple == 0.0:
        st.sidebar.warning("Mode Open memerlukan Trailing Stop aktif; otomatis menggunakan TP 2R.")
        tp_multiple = 2.0

    st.sidebar.divider()
    send_telegram = st.sidebar.toggle("Kirim alert Telegram", value=True)
    st.sidebar.caption("Token bot, Chat ID, dan opsional Proxy diatur melalui Streamlit Secrets atau environment variables.")

    settings = ScanSettings(
        symbols=int(symbols),
        interval_seconds=int(scan_interval),
        initial_equity=float(initial_equity),
        risk_pct=float(risk_pct),
        leverage=int(leverage),
        send_telegram=bool(send_telegram),
        trailing_stop_enabled=bool(trailing_enabled),
        tp_multiple=float(tp_multiple),
    )
    fragment = st.fragment(run_every=f"{scan_interval}s" if auto_scan else None)
    fragment(render_dashboard)(engine, settings, auto_scan)


if __name__ == "__main__":
    main()

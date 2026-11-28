"""CoinAgentX Simple Reversal — Streamlit entry point (v7.0.7 Lean & Fast)."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
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
    SIZING_MARGIN_PCT,
    SIZING_MARGIN_USD,
    SIZING_RISK_PCT,
    ScanSettings,
    SimpleTradingEngine,
    describe_trailing_state,
    trade_financials,
)

WIB = ZoneInfo("Asia/Jakarta")
UI_ENGINE_KEY = f"{APP_VERSION}-single-perp-sizing-v707"


def secret_getter(name: str):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def get_engine(cache_key: str):
    _ = cache_key
    return SimpleTradingEngine(secret_getter=secret_getter)


def fmt_time(ms):
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, WIB).strftime("%d-%m-%Y %H:%M:%S WIB")


def fmt_pf(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_equity_curve_df(snap: dict) -> pd.DataFrame:
    closed = snap["closed_trades"]
    if not closed:
        return pd.DataFrame()
    base_eq = float(snap["summary"]["initial_equity"])
    chron = sorted(closed, key=lambda t: int(t.get("closed_ms") or t.get("updated_ms") or t.get("opened_ms") or 0))
    cur = base_eq
    start_ts = int(chron[0].get("closed_ms") or chron[0].get("opened_ms") or int(time.time() * 1000)) - 60_000
    points = [{"Waktu": datetime.fromtimestamp(start_ts / 1000, WIB), "Ekuitas (USDT)": cur}]
    for t in chron:
        ts = int(t.get("closed_ms") or t.get("updated_ms") or t.get("opened_ms") or 0)
        cur += float(t.get("pnl_usdt") or 0.0)
        points.append({"Waktu": datetime.fromtimestamp(ts / 1000, WIB), "Ekuitas (USDT)": round(cur, 4)})
    return pd.DataFrame(points).set_index("Waktu")


def render_performance_panel(snap: dict):
    st.subheader("📊 Statistik Performa & Win Rate")
    s = snap["summary"]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Ekuitas Paper", f"{s['equity']:,.2f} USDT", f"{s['total_pnl']:+,.2f} ({s['roi_pct']:+.2f}%)")
    m2.metric("Win Rate", f"{s['win_rate']:.1f}%", f"{s['wins']}W / {s['losses']}L / {s['breakevens']}BE")
    m3.metric("Profit Factor", fmt_pf(s["profit_factor"]), f"Max DD: {s['max_drawdown_pct']:.2f}%")
    m4.metric("Posisi Aktif / Selesai", f"{s['active']} / {s['closed']}", f"Realized: {s['realized']:+,.2f} USDT")

    if s["closed"] > 0:
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Modal Awal", f"{s['initial_equity']:,.2f} USDT")
        r2.metric("Unrealized PnL", f"{s['unrealized']:+,.2f} USDT")
        r3.metric("Rata-rata Win", f"{s['avg_win']:+,.2f} USDT")
        r4.metric("Rata-rata Loss", f"{s['avg_loss']:+,.2f} USDT")

    curve_df = build_equity_curve_df(snap)
    if not curve_df.empty:
        st.caption("Kurva Ekuitas Realized (USDT)")
        st.line_chart(curve_df)


def render_telegram_analysis_table(all_passed: list[dict]):
    st.subheader("Tabel Hasil Analisis — Lolos Alert Telegram")

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
    if settings.sizing_mode == SIZING_MARGIN_USD:
        size_badge = f"Margin {settings.margin_usd:g} USDT × {settings.leverage}x"
    elif settings.sizing_mode == SIZING_MARGIN_PCT:
        size_badge = f"Margin {settings.margin_pct:g}% Modal × {settings.leverage}x"
    else:
        size_badge = f"Risk {settings.risk_pct:g}% (Max {settings.leverage}x)"
    st.caption(f"Versi {APP_VERSION} · BB+RSI Murni · {size_badge} · {trail_badge} · {tp_badge} · Paper Only")
    st.warning("Mode simulasi saja. Aplikasi tidak mengirim order ke Binance, Bybit, atau Bitget.")

    if not getattr(engine.store, "is_persistent_cloud", bool(engine.store.pg_url)):
        st.error(
            "⚠️ Penyimpanan masih memakai SQLite lokal (sementara). Catatan paper trading akan hilang saat "
            "update/reboot Streamlit Cloud. Isi `DATABASE_URL` (Supabase Transaction Pooler) atau "
            "`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` di Streamlit Secrets."
        )
    elif getattr(engine.store, "restored_from_supabase", False):
        st.info(f"☁️ {engine.store.last_supabase_sync_note}")

    manual_scan = st.button("Scan sekarang", type="primary", use_container_width=False)
    last_tick = float(st.session_state.get("simple_last_scan_monotonic", 0.0))
    due = auto_scan and (last_tick == 0.0 or (time.monotonic() - last_tick >= settings.interval_seconds - 1))

    if manual_scan or due:
        with st.spinner("Memindai market Futures (paralel + pre-filter BB M15)…"):
            report = engine.scan(settings, force=manual_scan)
        st.session_state["simple_last_scan_monotonic"] = time.monotonic()
        st.session_state["simple_last_report"] = report
        if report.get("status") == "FAILED":
            st.error(report.get("error", "Scan gagal"))
        elif report.get("status") == "BUSY":
            st.info(report.get("note", "Scanner sedang berjalan."))
        else:
            found = len(report.get("signals") or [])
            dur = float(report.get("duration_seconds") or 0.0)
            m5_f = int(report.get("m5_fetched") or 0)
            scn = int(report.get("scanned") or 0)
            st.success(f"Scan selesai dalam {dur:.1f} detik · {scn} pair diperiksa (M5 diambil: {m5_f}) · {found} sinyal baru.")

    # ONE single DB connection loads the entire dashboard state
    snap = engine.store.dashboard_snapshot(settings.initial_equity, signal_limit=200)
    report = st.session_state.get("simple_last_report") or {}
    last_run = snap["latest_run"] or {}
    provider = report.get("provider") or last_run.get("provider") or "Belum terhubung"
    kind = report.get("instrument_kind") or last_run.get("instrument_kind") or "—"
    storage_short = "Supabase PostgreSQL" if engine.store.pg_url else ("Supabase REST" if engine.store.is_persistent_cloud else "SQLite Lokal")

    c1, c2, c3 = st.columns(3)
    c1.metric("Sumber market", provider)
    c2.metric("Instrumen", kind.replace("_", " "))
    c3.metric("Database", storage_short)

    render_performance_panel(snap)

    st.divider()
    render_telegram_analysis_table(snap["telegram_signals"])

    st.divider()
    st.subheader("Paper trading aktif (Perpetual · Step R-Multiple Trailing Stop)")
    active = snap["active_trades"]
    if active:
        active_rows = []
        for t in active:
            tstate = describe_trailing_state(t)
            fin = trade_financials(t)
            active_rows.append({
                "Pair": t["symbol"],
                "Arah": "🟢 LONG" if t["direction"] == "LONG" else "🔴 SHORT",
                "Entry": round(float(t["entry"]), 6),
                "Margin (USDT)": round(fin["margin_usdt"], 2),
                "Nilai Posisi (USDT)": round(fin["notional_usdt"], 2),
                "Leverage": f"{t['leverage']}x",
                "SL Awal": round(float(tstate["initial_stop"]), 6),
                "SL Aktif (Trail)": round(float(t["stop"]), 6),
                "Status Trailing": tstate["trail_stage"],
                "TP": round(float(t["target"]), 6) if float(t["target"] or 0) > 0 else "Open (Trail)",
                "Mark": round(float(t["mark_price"]), 6),
                "Profit (R)": f"{tstate['current_r']:+.2f}R",
                "Unrealized PnL (USDT)": round(fin["pnl_usdt"], 4),
                "ROE (%)": f"{fin['roe_pct']:+.2f}%",
            })
        st.dataframe(pd.DataFrame(active_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("Tidak ada posisi paper yang sedang terbuka.")

    with st.expander("Riwayat paper trading"):
        closed = snap["closed_trades"][:250]
        if closed:
            hist_rows = []
            for t in closed:
                fin = trade_financials(t)
                hist_rows.append({
                    "Pair": t["symbol"],
                    "Arah": t["direction"],
                    "Entry": round(float(t["entry"]), 6),
                    "Margin (USDT)": round(fin["margin_usdt"], 2),
                    "Nilai Posisi (USDT)": round(fin["notional_usdt"], 2),
                    "Leverage": f"{t['leverage']}x",
                    "SL Terakhir": round(float(t["stop"]), 6),
                    "Exit": round(float(t["exit_price"]), 6) if t["exit_price"] is not None else "—",
                    "Status": t["status"],
                    "PnL (USDT)": round(fin["pnl_usdt"], 4),
                    "ROE (%)": f"{fin['roe_pct']:+.2f}%",
                    "Waktu buka": fmt_time(t["opened_ms"]),
                    "Waktu tutup": fmt_time(t["closed_ms"]),
                })
            df_hist = pd.DataFrame(hist_rows)
            st.dataframe(df_hist, use_container_width=True, hide_index=True)
            st.download_button(
                label="Download Riwayat Paper Trading (CSV)",
                data=df_hist.to_csv(index=False).encode("utf-8"),
                file_name=f"coinagentx_paper_history_{datetime.now(WIB).strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )
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
            "Mode Ukuran Posisi": size_badge,
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

    with st.expander("Logika strategi, Perpetual Sizing & Trailing Stop"):
        st.markdown(
            "**Hanya dua indikator:** Bollinger Bands (20, 2) pada M15 dan RSI (14) pada M5.\n\n"
            "**LONG:** candle tertutup M15 menyentuh Bollinger bawah; candle tertutup M5 berikutnya bullish "
            "dengan high & close lebih tinggi dari candle sebelumnya; RSI(14) M5 sebelumnya ≤ 30 dan naik.\n\n"
            "**SHORT:** candle tertutup M15 menyentuh Bollinger atas; candle tertutup M5 berikutnya bearish "
            "dengan low & close lebih rendah dari candle sebelumnya; RSI(14) M5 sebelumnya ≥ 70 dan turun.\n\n"
            "**Perhitungan PnL Perpetual Futures:**\n"
            "- **Mode Margin (% Modal / Nominal USD):** `Nilai Posisi = Margin × Leverage`, `Qty = Nilai Posisi / Entry`, "
            "`PnL = (Exit - Entry) × Qty` (LONG) dan `ROE% = (PnL / Margin) × 100%`. "
            "Leverage 20x langsung menghasilkan Nilai Posisi dan PnL 10× lebih besar dibanding 2x.\n"
            "- **Proteksi Likuidasi Terisolasi:** Jika SL lebih jauh dari harga likuidasi pada leverage tinggi, simulasi membatasi "
            "kerugian maksimum sebesar margin yang dialokasikan (`CLOSED_LIQ`).\n\n"
            "**Trailing Stop — Step R-Multiple (Opsi 1):**\n"
            "- Profit ≥ **+1.0R** → SL digeser ke **Break-Even (+0.05R)**.\n"
            "- Profit ≥ **+1.5R** → SL mengunci **+0.50R**.\n"
            "- Jika TP > 2R / Open: Profit ≥ **+2.0R** → SL mengunci **+1.20R**, naik **+0.50R** tiap kenaikan **+0.50R**."
        )


def main():
    st.set_page_config(page_title="CoinAgentX Simple Reversal", page_icon="📈", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1380px;padding-top:1.3rem}"
        "[data-testid=stMetric]{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:10px}</style>",
        unsafe_allow_html=True,
    )
    db_sig_src = "|".join(
        str(secret_getter(k) or "")
        for k in ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL", "COINAGENT_DATABASE_URL", "SUPABASE_URL")
    )
    db_sig = sha256(db_sig_src.encode("utf-8")).hexdigest()[:12]
    engine = get_engine(f"{UI_ENGINE_KEY}:{db_sig}")

    st.sidebar.title("Pengaturan Bot")
    auto_scan = st.sidebar.toggle("Auto scan", value=True)
    scan_interval = st.sidebar.selectbox("Interval scan", [60, 120, 300], index=0,
                                         format_func=lambda x: f"{x // 60} menit" if x >= 60 else f"{x} detik")
    symbols = st.sidebar.select_slider("Jumlah pair", options=[10, 20, 30, 50, 75, 100], value=50)

    saved_equity = st.session_state.get("cached_saved_equity")
    if saved_equity is None:
        saved_equity = engine.store.get_initial_equity()
        if saved_equity is not None:
            st.session_state["cached_saved_equity"] = saved_equity
    initial_equity = st.sidebar.number_input("Modal awal paper (USDT)", min_value=50.0, max_value=1_000_000.0,
                                              value=float(saved_equity or 1000.0), step=50.0, disabled=saved_equity is not None)
    if saved_equity is not None:
        st.sidebar.caption("Modal awal sudah tersimpan di database.")

    st.sidebar.divider()
    st.sidebar.markdown("**Ukuran Entry & Leverage (Perpetual)**")
    sizing_labels = {
        "% dari Modal (Margin × Leverage)": SIZING_MARGIN_PCT,
        "Nominal USD (Margin × Leverage)": SIZING_MARGIN_USD,
        "Risiko Tetap (% Risk ke SL)": SIZING_RISK_PCT,
    }
    sizing_choice = st.sidebar.selectbox("Mode Ukuran Posisi", list(sizing_labels.keys()), index=0)
    sizing_mode = sizing_labels[sizing_choice]

    margin_pct = 5.0
    margin_usd = 50.0
    risk_pct = 0.5
    if sizing_mode == SIZING_MARGIN_PCT:
        margin_pct = st.sidebar.slider("Margin per Entry (% dari modal)", min_value=1.0, max_value=50.0, value=5.0, step=0.5)
    elif sizing_mode == SIZING_MARGIN_USD:
        margin_usd = st.sidebar.number_input("Margin per Entry (USDT)", min_value=5.0, max_value=100_000.0, value=50.0, step=5.0)
    else:
        risk_pct = st.sidebar.slider("Risiko ke SL (% modal)", min_value=0.1, max_value=5.0, value=0.5, step=0.1)

    leverage = st.sidebar.selectbox("Leverage Perpetual", [1, 2, 3, 5, 10, 15, 20, 25, 50], index=4)

    # Live preview of perpetual position size
    ref_eq = float(saved_equity or initial_equity)
    if sizing_mode == SIZING_MARGIN_PCT:
        est_margin = ref_eq * margin_pct / 100.0
        st.sidebar.caption(f"Estimasi entry: Margin **{est_margin:,.2f} USDT** × **{leverage}x** = **{est_margin * leverage:,.2f} USDT** posisi")
    elif sizing_mode == SIZING_MARGIN_USD:
        est_margin = min(margin_usd, ref_eq)
        st.sidebar.caption(f"Estimasi entry: Margin **{est_margin:,.2f} USDT** × **{leverage}x** = **{est_margin * leverage:,.2f} USDT** posisi")
    else:
        st.sidebar.caption(f"Mode Risiko Tetap: loss saat kena SL dijaga ~**{ref_eq * risk_pct / 100.0:,.2f} USDT** (leverage hanya batas maksimal posisi).")

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
    st.sidebar.caption("Token bot, Chat ID, dan Supabase Secrets diatur di Streamlit Secrets.")

    settings = ScanSettings(
        symbols=int(symbols),
        interval_seconds=int(scan_interval),
        initial_equity=float(initial_equity),
        sizing_mode=sizing_mode,
        margin_pct=float(margin_pct),
        margin_usd=float(margin_usd),
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

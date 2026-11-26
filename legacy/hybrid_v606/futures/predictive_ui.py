"""Dashboard Presentation Layer for Predictive Watchlist v1.0 (CoinAgentX v6.0.5 Stage 4).

Separates UI formatting, filtering, deterministic priority sorting, and color badges
from `app.py` so `app.py` stays clean and maintainable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .predictive_watchlist import (
    ACTIVE_STAGES,
    PredictiveSignal,
    STAGE_CONFIRMED,
    STAGE_DIRECTION_CHANGED,
    STAGE_EARLY_WATCH,
    STAGE_EXPIRED,
    STAGE_MISSED_ENTRY,
    STAGE_PAPER_ENTRY,
    STAGE_PRE_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_SIGNAL_INVALIDATED,
)

STAGE_COLORS = {
    STAGE_PRE_WATCH: "#2563EB",          # Biru
    STAGE_EARLY_WATCH: "#38BDF8",        # Biru muda
    STAGE_SETUP_FORMING: "#F59E0B",      # Kuning / Oranye
    f"{STAGE_CONFIRMED}_LONG": "#16A34A",  # Hijau
    f"{STAGE_CONFIRMED}_SHORT": "#DC2626", # Merah
    STAGE_PAPER_ENTRY: "#15803D",
    STAGE_SIGNAL_INVALIDATED: "#6B7280", # Abu-abu
    STAGE_EXPIRED: "#6B7280",            # Abu-abu
    STAGE_MISSED_ENTRY: "#9CA3AF",       # Abu-abu
    STAGE_DIRECTION_CHANGED: "#6B7280",  # Abu-abu
}

STAGE_DISCLAIMERS = (
    "PRE-WATCH bukan entry — hanya peringatan dini dari data intrabar M1/M5/M15.",
    "EARLY WATCH bukan entry — struktur awal mulai terbentuk, tunggu trigger M5.",
    "SETUP FORMING belum entry — wajib menunggu seluruh hard gates Hybrid Adaptive v1.1 lolos.",
    "Hanya CONFIRMED yang dapat menjadi PAPER_ENTRY (100% PAPER ONLY, tanpa live order).",
)


def badge_color_for_signal(stage: str, direction: str = "LONG") -> str:
    st = str(stage or "").upper()
    if st == STAGE_CONFIRMED:
        return STAGE_COLORS.get(f"{STAGE_CONFIRMED}_{str(direction).upper()}", "#16A34A")
    return STAGE_COLORS.get(st, "#6B7280")


def filter_predictive_signals(
    signals: Iterable[PredictiveSignal],
    *,
    stage_filter: str = "ALL",
    direction_filter: str = "ALL",
) -> list[PredictiveSignal]:
    st_f = str(stage_filter or "ALL").upper().replace(" ", "_")
    dir_f = str(direction_filter or "ALL").upper()
    out = []
    for s in signals:
        if st_f != "ALL":
            if st_f == "INVALIDATED":
                if s.stage not in (STAGE_SIGNAL_INVALIDATED, STAGE_DIRECTION_CHANGED, STAGE_MISSED_ENTRY):
                    continue
            elif st_f == "EXPIRED":
                if s.stage != STAGE_EXPIRED:
                    continue
            elif s.stage != st_f:
                continue
        if dir_f in ("BUY", "LONG") and s.direction != "LONG":
            continue
        if dir_f in ("SELL", "SHORT") and s.direction != "SHORT":
            continue
        out.append(s)
    return out


def sort_predictive_signals(signals: Iterable[PredictiveSignal]) -> list[PredictiveSignal]:
    """Priority sorting (Section H.3):
    1. Valid active signals closest to trigger
    2. Setup score (confirmed setup_quality_score or early_score)
    3. Freshness (lowest data_freshness_ms)
    4. Signal age (newest first)
    """
    def _key(s: PredictiveSignal):
        is_valid_active = 0 if (s.signal_state == "ACTIVE" and not s.is_stale and s.entry_validity not in ("CHASE_MISSED", "INVALIDATED", "EXPIRED", "STALE", "BLOCKED")) else 1
        dist = float(s.distance_to_trigger_pct if s.distance_to_trigger_pct is not None else 999.0)
        eff_score = float(s.setup_quality_score if s.setup_quality_score is not None else s.early_score)
        fresh = int(s.data_freshness_ms or 0)
        age = float(s.signal_age_seconds or 0.0)
        return (is_valid_active, round(dist, 4), -eff_score, fresh, age)

    return sorted(list(signals), key=_key)


def build_watchlist_rows(signals: Iterable[PredictiveSignal]) -> list[dict[str, Any]]:
    """Produce dashboard table rows containing all 18 mandatory columns (Section H.1 & H.5)."""
    rows = []
    tz = ZoneInfo("Asia/Jakarta")
    for s in signals:
        upd_str = datetime.fromtimestamp(s.updated_at_ms / 1000, tz).strftime("%H:%M:%S WIB") if s.updated_at_ms else "-"
        rows.append(
            {
                "Pair": s.symbol,
                "Direction": "BUY / LONG" if s.direction == "LONG" else "SELL / SHORT",
                "Stage": s.stage.replace("_", " "),
                "Badge Color": badge_color_for_signal(s.stage, s.direction),
                "Early Score": round(float(s.early_score), 1),
                "Setup Quality Score": round(float(s.setup_quality_score), 1) if s.setup_quality_score is not None else "-",
                "M15 Regime": s.m15_regime,
                "Strategy A-E": s.strategy_id or "-",
                "M1 Status": s.m1_status,
                "M5 Status": s.m5_status,
                "Detected Price": s.detected_price,
                "Current Price": s.current_price,
                "Change Since Detected (%)": round(float(s.price_change_since_detected_pct), 3),
                "Trigger Price": s.trigger_price,
                "Invalidation Price": s.invalidation_price,
                "Distance to Trigger (%)": round(float(s.distance_to_trigger_pct), 3),
                "Distance to Invalidation (%)": round(float(s.distance_to_invalidation_pct), 3),
                "Signal Age (s)": int(round(float(s.signal_age_seconds))),
                "Trend": s.trend_state,
                "Chase Status": "CHASED (MISSED)" if s.entry_validity == "CHASE_MISSED" else "OK",
                "Provider": s.provider,
                "Last Update": upd_str,
                "Signal Validity": "STALE" if s.is_stale else s.entry_validity,
            }
        )
    return rows


def render_predictive_watchlist_tab(st_mod, repo, *, worker_status: Optional[dict] = None) -> None:
    """Render the Predictive Watchlist tab inside Streamlit `app.py` (read-only from shared DB)."""
    st_mod.subheader("Predictive Watchlist v1.0 (M1/M5/M15 Early Detection · PAPER ONLY)")
    st_mod.info(" | ".join(STAGE_DISCLAIMERS))

    if worker_status:
        alive = worker_status.get("alive", False)
        st_mod.caption(
            f"Worker: {worker_status.get('worker_id', '-')} · Status: {'ONLINE' if alive else 'OFFLINE/STALE'} · "
            f"Provider: {worker_status.get('active_provider', '-')} · DB: {worker_status.get('database_fingerprint', '-')} "
            "(Monitoring berjalan pada worker independen; refresh browser tidak menggantikan worker)."
        )

    c1, c2, c3 = st_mod.columns([2, 2, 1])
    stage_opt = c1.selectbox(
        "Filter Tahap",
        ["ALL", "PRE_WATCH", "EARLY_WATCH", "SETUP_FORMING", "CONFIRMED", "INVALIDATED", "EXPIRED"],
        key="pw_stage_filter",
    )
    dir_opt = c2.selectbox("Filter Arah", ["ALL", "BUY", "SELL"], key="pw_dir_filter")
    if c3.button("Refresh Watchlist", key="pw_refresh_btn"):
        pass

    try:
        all_signals = repo.list_signals(limit=200)
    except Exception as exc:
        st_mod.error(f"Gagal membaca Predictive Watchlist dari database: {exc}")
        return

    filtered = filter_predictive_signals(all_signals, stage_filter=stage_opt, direction_filter=dir_opt)
    ordered = sort_predictive_signals(filtered)
    rows = build_watchlist_rows(ordered)

    if not rows:
        st_mod.caption("Belum ada sinyal Predictive Watchlist yang cocok dengan filter aktif.")
        return

    import pandas as pd
    st_mod.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

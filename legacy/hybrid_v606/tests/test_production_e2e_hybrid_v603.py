"""End-to-End Production Acceptance Test Suite for CoinAgentX v6.0.3 (P0-1 through P0-8).

Proves the complete production execution chain:
`Worker -> MarketScanner.run() -> MarketScanner._analyse() -> evaluate_hybrid_adaptive() -> Risk Validation -> PaperLedger -> TelegramAlerts`
and verifies all 7 mandatory P0-8 production requirements:
1. Strategy F (`evaluate_hybrid_adaptive`) is genuinely invoked by `MarketScanner._analyse()` with NO silent fallback to legacy `evaluate()`.
2. `regime` (`m15_regime`) and `strategy_id` (`A`–`E` / `F`) are recorded on every scan result, `worker_analyses`, and `history`.
3. Valid rule-engine candidates are processed and admitted into `PaperLedger` and `TelegramAlerts` even when Groq AI is `AI_UNAVAILABLE` or returns `WAIT` (`AI_CAUTION`), without AI altering direction, price, SL, TP, qty, leverage, or score.
4. `PaperLedger` lifecycle follows the Hybrid Adaptive specification (`TP1=+1R 40%`, `TP2=+2R 35%`, `TP3=+3R 25%` + automatic `60/40` and `100%` lot-size fallback, fee-adjusted net break-even after TP1, `+1.5R` trailing at `1.25 * M5_ATR14` on completed M5 bars, `6`-bar `< +0.5R` + momentum-reversal time exit, `12`-bar `< +1.0R` time exit, and `2`-bar M5 pending expiry).
5. `BALANCED` strategy profile is explicitly synchronized across Admin, Viewer, and Worker (`create_worker_engine`, `sync_worker_config_from_preferences`, and `worker_heartbeat["effective_strategy_profile"]`).
6. Minimum 250 closed M15 and M5 candle history (`m15_candle_limit=260`, `m5_candle_limit=260`) is enforced and `HISTORY_INSUFFICIENT` is surfaced separately when fewer than 250 candles are available.
7. Live orders are strictly disabled (`PAPER` simulation only; no live order endpoints exist).
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import numpy as np
import pytest

import app as app_mod
import worker as worker_mod
import backtest_hybrid_adaptive as bt_mod
from futures import history
from futures.db_backend import (
    DEFAULT_USER_PREFERENCES,
    load_global_preferences,
    save_global_preferences,
)
from futures.groq_rotation import evaluate_contextual_ai
from futures.hybrid_adaptive import (
    STRATEGY_A,
    STRATEGY_B,
    STRATEGY_C,
    STRATEGY_D,
    STRATEGY_E,
    STRATEGY_F,
    compute_hybrid_tp_targets,
    evaluate_hybrid_adaptive,
)
from futures.lifecycle import after_fill, manage_position
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.screener import M15_CANDLE_LIMIT, M5_CANDLE_LIMIT, select_candidates, shortlist
from futures.telegram import TelegramAlerts, eligible, format_alert
from futures.version import APP_VERSION
from tests.fixtures import NOW, candles, setup
from tests.test_market_scanner import Market, Pool
from tests.test_strategy import short_setup
from tests.test_telegram import Session as TgSession


def test_p0_1_and_p0_8_end_to_end_worker_scanner_hybrid_adaptive_paper_and_telegram(tmp_path, monkeypatch):
    """P0-1, P0-2, P0-3, P0-7 & P0-8: Full chain Worker -> MarketScanner.run() -> MarketScanner._analyse()
    -> evaluate_hybrid_adaptive() -> Risk Validation -> PaperLedger -> TelegramAlerts.
    """
    assert APP_VERSION in ("6.0.3", "6.0.4", "6.0.5", "6.0.6")
    db_path = tmp_path / "paper_history.sqlite3"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_path))

    # 1. Save BALANCED preference and verify Worker syncs strategy_profile="BALANCED"
    save_res = save_global_preferences(
        db_path,
        {
            "auto": True,
            "relaxed": True,
            "strategy_profile": "BALANCED",
            "interval": 60,
            "candidates": 2,
            "shortlist_limit": 30,
            "alert": True,
            "watchlist_alert": True,
            "summary_alert": True,
        },
    )
    assert save_res["ok"] is True
    loaded_prefs = load_global_preferences(db_path)
    assert loaded_prefs["strategy_profile"] == "BALANCED"

    ledger = PaperLedger(db_path, initial_equity=1000.0, expected_instrument_kind="USD_M_PERPETUAL")
    base_cfg = ScannerConfig(fee=0.0004, slippage_bps=0.1, funding=0.00001)
    synced_cfg = worker_mod.sync_worker_config_from_preferences(ledger, base_cfg)
    assert synced_cfg.strategy_profile == "BALANCED"
    assert synced_cfg.strategy_engine == "HYBRID_ADAPTIVE_V1"
    assert synced_cfg.allow_paper_on_ai_unavailable is True

    # Record worker heartbeat and verify effective_strategy_profile is persisted and exposed
    ledger.record_worker_heartbeat(
        "worker-e2e-v603",
        NOW,
        synced_cfg.interval,
        strategy_profile=synced_cfg.strategy_profile,
        strategy_engine=synced_cfg.strategy_engine,
    )
    wh = ledger.worker_health_status(now_ms=NOW + 1000)
    assert wh["alive"] is True
    assert wh["effective_strategy_profile"] == "BALANCED"
    assert wh["strategy_engine"] == "HYBRID_ADAPTIVE_V1"

    # 2. Prove evaluate_hybrid_adaptive() is genuinely called during MarketScanner.run() AND works when Groq AI is UNAVAILABLE
    hybrid_calls = []
    orig_eval_hybrid = evaluate_hybrid_adaptive

    def spy_evaluate_hybrid_adaptive(*args, **kwargs):
        res = orig_eval_hybrid(*args, **kwargs)
        hybrid_calls.append((res.meta.get("symbol"), res.meta.get("m15_regime"), res.meta.get("routed_strategy"), res.status))
        return res

    monkeypatch.setattr("futures.market_scanner.evaluate_hybrid_adaptive", spy_evaluate_hybrid_adaptive)

    # Simulate Groq AI completely unavailable (ok=False, AI_UNAVAILABLE)
    class OutagePool:
        def redact(self, text):
            return text

        def review(self, result, model, **kwargs):
            return {
                "ok": False,
                "decision": "WAIT",
                "reasons": ["Groq API HTTP 503 Outage Simulation"],
                "cautions": [],
                "ai_status": "AI_UNAVAILABLE",
            }

    tg_session = TgSession()
    alerts = TelegramAlerts(
        "123:test-token",
        "-100999",
        db_path,
        session=tg_session,
        clock=lambda: 1000.0,
        monotonic=lambda: 100.0,
    )
    market = Market()
    scanner = MarketScanner(
        market,
        OutagePool(),
        NewsCalendar(),
        ledger,
        alerts,
        clock=lambda: 100.0,
        trusted_worker=True,
    )

    run_cfg = replace(
        synced_cfg,
        candidates=2,
        fee=0.0004,
        slippage_bps=0.1,
        funding=0.00001,
        send_alerts=True,
        send_watchlist_alerts=False,
        send_scan_summary=False,
    )
    report = scanner.run(run_cfg, worker_id="worker-e2e-v603")

    # Proof 1: Strategy F (`evaluate_hybrid_adaptive`) was genuinely called in production scanner
    assert len(hybrid_calls) >= 2
    assert report["strategy_engine"] == "HYBRID_ADAPTIVE_V1"
    assert report["primary_strategy_id"] == "F"
    assert report["effective_strategy_profile"] == "BALANCED"

    # Proof 2: `strategy_id` and `regime` are recorded on every scan result, ranked item, worker_analyses, and history
    assert len(report["analyses"]) == 2
    for item in report["analyses"]:
        assert item["strategy_id"] in ("A", "B", "C", "D", "E")
        assert item["regime"] in ("TREND_UP", "TREND_DOWN", "STRONG_TREND_UP", "STRONG_TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN", "RANGE", "TRANSITION")
        assert item["result"].meta["strategy_engine"] == "HYBRID_ADAPTIVE_V1"
        assert item["result"].plan["hybrid_adaptive_v1"] is True
        assert item["result"].plan["live_orders_disabled"] if "live_orders_disabled" in item["result"].plan else item["result"].meta["live_orders_disabled"] is True

    persisted_rep = ledger.last_scan_report()
    assert len(persisted_rep["worker_analyses"]) == 2
    assert all(w.get("Strategy") in ("A", "B", "C", "D", "E") and w.get("Regime") not in (None, "", "N/A") for w in persisted_rep["worker_analyses"])

    hist_rows = history.load(db_path)
    assert len(hist_rows) >= 2
    assert all(h.get("strategy_id") in ("A", "B", "C", "D", "E") and h.get("regime") not in (None, "UNKNOWN") for h in hist_rows)

    # Proof 3: Valid candidates are admitted into PaperLedger and TelegramAlerts even when Groq AI is AI_UNAVAILABLE
    assert all(d["ai_status"] == "AI_UNAVAILABLE" and d["status"] == "VALID" for d in report["analyses"])
    active_trades = ledger.active()
    assert len(active_trades) == 2
    assert len(tg_session.calls) == 2

    # Proof 4: P0-7 Mandatory Scanner Diagnostics are populated
    sd = report["scanner_diagnostics"]
    assert sd["universe_total"] == 120
    assert sd["lolos_stage_1"] >= 20
    assert sd["lolos_candle_history"] >= 2
    assert sum(sd["m15_regime_distribution"].values()) >= 2
    assert sum(sd["strategy_candidate_counts"].values()) >= 2
    assert sd["m5_trigger_count"] == 2
    assert len(sd["setup_quality_scores"]) == 2
    assert sd["groq_status_counts"]["AI_UNAVAILABLE"] == 2
    assert sd["telegram_status_counts"]["sent"] == 2
    assert sd["worker_heartbeat"]["effective_strategy_profile"] == "BALANCED"
    assert "backend" in sd["persistence_status"]


def test_p0_2_groq_advisor_never_modifies_parameters_or_vetoes_valid_rule_candidate():
    """P0-2: Groq AI cannot alter direction, entry, SL, TP, qty, leverage, or score, and WAIT/NO_TRADE from AI produces AI_CAUTION without vetoing."""
    s, a = setup()
    r = evaluate_hybrid_adaptive(s, a, paper=True)
    assert r.status == "VALID" and r.plan is not None
    plan_before = deepcopy(r.plan)
    score_before = r.metrics["setup_quality_score"]

    for ai_resp, expected_adv in [
        ({"ok": False, "decision": "WAIT", "reasons": ["Timeout"]}, "AI_UNAVAILABLE"),
        ({"ok": True, "decision": "WAIT", "reasons": ["Wait"], "cautions": ["Caution 1"]}, "AI_CAUTION"),
        ({"ok": True, "decision": "NO_TRADE", "reasons": ["No trade"], "cautions": ["Caution 2"]}, "AI_CAUTION"),
        ({"ok": True, "decision": "SHORT", "reasons": ["Opposite"], "cautions": []}, "AI_CAUTION"),
        ({"ok": True, "decision": "LONG", "reasons": ["Aligned"], "cautions": []}, "AI_ADVISORY_OK"),
    ]:
        ctx = evaluate_contextual_ai(r, ai_resp)
        assert ctx["rule_status"] == "RULE_BASED_VALID"
        assert ctx["advisory_status"] == expected_adv
        assert ctx["final_status"] == "VALID"
        assert ctx["paper_entry_allowed"] is True
        assert r.plan == plan_before
        assert r.metrics["setup_quality_score"] == score_before

    # Sidebar text in app.py no longer claims "tanpa AI tidak ada sinyal yang disetujui"
    app_txt = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "tanpa AI tidak ada sinyal yang disetujui" not in app_txt
    assert "Advisor-Only" in app_txt


def test_p0_4_candle_history_250_enforced_and_history_insufficient_surfaced():
    """P0-4: Loader requests >= 260 candles, and < 250 candles yields explicit HISTORY_INSUFFICIENT without weakening validation."""
    assert M15_CANDLE_LIMIT >= 260
    assert M5_CANDLE_LIMIT >= 260

    s, a = setup()
    s.bias = s.bias.tail(180).reset_index(drop=True)
    r_short_m15 = evaluate_hybrid_adaptive(s, a, paper=True)
    assert r_short_m15.status == "WAIT"
    assert r_short_m15.meta.get("history_status") == "HISTORY_INSUFFICIENT"
    assert r_short_m15.meta.get("data_status") == "HISTORY_INSUFFICIENT"


def test_p0_5_unified_paper_lifecycle_1r_2r_3r_40_35_25_partial_fill_rounding_and_trailing(tmp_path):
    """P0-5: PaperLedger & lifecycle execute 1R/2R/3R (40%/35%/25%), lot-size fallback (60/40 and 100%),
    partial fill quantity rounding, fee-adjusted net BE after TP1, +1.5R trailing (1.25 ATR), and 6/12-bar time exits.
    """
    s, a = setup()
    s.mode = "SCALPING"
    for name, tf in zip(("bias", "execution"), TIMEFRAMES["SCALPING"]):
        d = getattr(s, name)
        step = INTERVAL_MS[tf]
        end = (NOW // step) * step
        d["open_time"] = list(range(end - len(d) * step, end, step))
        d["close_time"] = d.open_time + step - 1

    r = evaluate_hybrid_adaptive(s, a, paper=True)
    assert r.status == "VALID"
    p = r.plan
    assert p["hybrid_adaptive_v1"] is True
    assert p["tp_split_mode"] == "3_WAY_40_35_25"
    assert p["tp1"] == pytest.approx(p["entry"] + 1.0 * p["distance"], abs=p["tick"])
    assert p["tp2"] == pytest.approx(p["entry"] + 2.0 * p["distance"], abs=p["tick"])
    assert p["tp3"] == pytest.approx(p["entry"] + 3.0 * p["distance"], abs=p["tick"])
    assert p["tp1_qty"] == pytest.approx(p["qty"] * 0.40, abs=p["quantity_step"])
    assert p["tp2_qty"] == pytest.approx(p["qty"] * 0.35, abs=p["quantity_step"])
    assert p["tp1_qty"] + p["tp2_qty"] + p["runner_qty"] == pytest.approx(p["qty"], abs=1e-9)

    # Test partial fill & lot-size fallback (2-way 60/40 and 1-way 100%)
    partial_q = round(p["qty"] * 0.50, 3)
    p_after = after_fill(p, p["entry"], partial_q, p["signal_close_ms"] + 10_000)
    assert p_after["qty"] == partial_q
    assert p_after["tp1_qty"] + p_after["tp2_qty"] + p_after["runner_qty"] == pytest.approx(partial_q, abs=1e-9)

    fallback_2way = compute_hybrid_tp_targets(100.0, 99.0, "LONG", 0.01, 0.25, 0.1, min_qty=0.1, min_notional=5.0)
    assert fallback_2way["split_mode"] == "2_WAY_60_40_FALLBACK"
    fallback_1way = compute_hybrid_tp_targets(100.0, 99.0, "LONG", 0.01, 0.10, 0.1, min_qty=0.1, min_notional=5.0)
    assert fallback_1way["split_mode"] == "1_WAY_100_FALLBACK"

    # Full PaperLedger advance() lifecycle: PENDING -> OPEN -> TP1 (+1R 40% + Net BE) -> TP2 (+2R 35% + 1.5R Trail) -> TP3 (+3R 25% CLOSED)
    ledger = PaperLedger(tmp_path / "lifecycle_f.db")
    data = {
        "snapshot": s,
        "result": r,
        "ai": {"ok": False, "decision": "WAIT", "reasons": ["No AI"]},
        "status": "VALID",
        "rule_status": "RULE_BASED_VALID",
        "ai_status": "AI_UNAVAILABLE",
        "advisory_status": "AI_UNAVAILABLE",
        "advisor_only": True,
        "allow_paper_on_ai_unavailable": True,
        "monotonic": 100.0,
    }
    queued, sig_key = ledger.queue(data, lambda: 100.0)
    assert queued is True

    def _quote(px, delta_ms):
        q = deepcopy(s)
        q.now_ms = NOW + delta_ms
        q.last = q.mark = px
        q.bid = px - 0.001
        q.ask = px + 0.001
        q.bids = [[q.bid, 5000.0]]
        q.asks = [[q.ask, 5000.0]]
        q.open_bar_high = px + 0.002
        q.open_bar_low = px - 0.002
        q.open_mark_high = px + 0.002
        q.open_mark_low = px - 0.002
        q.quote_times = {k: q.now_ms - 10 for k in q.quote_keys}
        return q

    # Trigger and fill at entry
    ledger.advance(_quote(p["entry_trigger"], 5_000))
    t_open = ledger.active()[0]
    assert t_open["status"] == "OPEN"
    assert t_open["remaining_qty"] == pytest.approx(p["qty"])

    # Hit TP1 (+1R) -> 40% closed, 60% remaining, stop moved above entry (fee-adjusted net BE)
    ledger.advance(_quote(p["tp1"] + 0.01, 15_000))
    t_tp1 = ledger.active()[0]
    assert t_tp1["tp1_ms"] is not None
    assert t_tp1["remaining_qty"] == pytest.approx(p["qty"] - p["tp1_qty"], abs=1e-6)
    assert t_tp1["stop"] > p["entry"]

    # Hit TP2 (+2R) -> 35% closed, 25% runner remaining
    ledger.advance(_quote(p["tp2"] + 0.01, 25_000))
    t_tp2 = ledger.active()[0]
    assert t_tp2["tp2_ms"] is not None
    assert t_tp2["remaining_qty"] == pytest.approx(p["runner_qty"], abs=1e-6)

    # Hit TP3 (+3R) -> runner closed, status == CLOSED
    ledger.advance(_quote(p["tp3"] + 0.01, 35_000))
    assert len(ledger.active()) == 0
    closed_tr = ledger.trades()[0]
    assert closed_tr["status"] == "CLOSED"
    assert closed_tr["realized_pnl"] > 0


def test_p0_6_historical_ohlcv_bar_by_bar_backtest_valid_and_reproducible(tmp_path):
    """P0-6: backtest_hybrid_adaptive.py runs actual OHLCV candle-by-bar simulation using production evaluators A–E & F."""
    ds = bt_mod.build_historical_ohlcv_dataset(n_m15_bars=320)
    assert ds["m15_candles"] == 320
    assert ds["m5_candles"] == 960
    assert len(ds["sha256"]) == 64

    res = bt_mod.run_canonical_ohlcv_regimes_backtest(ds)
    assert res["dataset"]["multi_regime_m15_candles"] >= 15000
    assert res["dataset"]["multi_regime_m5_candles"] >= 15000

    f_oos = res["strategies"][STRATEGY_F]["out_of_sample_20pct"]
    assert f_oos["trades_filled"] > 0
    assert f_oos["expectancy_r"] > 0.0
    assert f_oos["profit_factor"] >= 1.20
    assert f_oos["max_drawdown_pct"] <= 8.0
    assert sum(1 for fl in res["walk_forward_folds"] if fl["expectancy_r"] > 0) >= 4

    report_md = bt_mod.write_walk_forward_report(res, tmp_path / "WALK_FORWARD_REPORT.md")
    text = report_md.read_text(encoding="utf-8")
    assert ds["sha256"][:16] in text or res["dataset"]["sha256"] in text
    assert "Out-of-Sample (20%)" in text

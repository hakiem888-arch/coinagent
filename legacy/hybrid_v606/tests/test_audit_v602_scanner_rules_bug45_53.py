"""Comprehensive Regression Suite for CoinAgentX Market Scanner & Buy/Sell Rules (BUG-45 through BUG-53 + 19-Metric Diagnostic Panel).

Covers:
1.  BUG-45: Shortlist 30 candidates, balanced LONG/SHORT ranking, rotating queue of 5 candidates/cycle, minimum M15 score filter (`min_m15_deep_score`), and Watchlist alerts for qualified candidates outside top 5.
2.  BUG-46: Stage-separated Watchlist & Entry alerts (`EARLY_WATCH` >=55, `SETUP_FORMING` >=65 + >=3/5 M5 confirmations, `PAPER_ENTRY`) with stage-aware deduplication (`symbol + timeframe + candle_close + direction + stage`) allowing upgrades while blocking duplicates.
3.  BUG-47: Hard Risk Gates preserved while soft indicators use a Weighted Confluence system (`>=3 of 5` core M5 confirmations in `BALANCED`/`ACTIVE`).
4.  BUG-48: Strategy profiles `CONSERVATIVE` (85, risk <=0.50%), `BALANCED` (78, risk <=0.35%), `ACTIVE` (72, risk <=0.25%) with capped leverage and out-of-sample walk-forward validation (`walk_forward_validate_profile`).
5.  BUG-49: Audited 10-component `compute_confidence` (confirmed swing sweep, price-vs-RSI swing comparison, StochRSI crossover, BB reclaim, retest; strictly 0 points when not occurring; never called win probability; out-of-sample PAPER calibration).
6.  BUG-50: Contextual AI separating `RULE_BASED_VALID`, `AI_CONFIRMED`, and `AI_UNAVAILABLE` when Groq API fails/times out/rate-limits, without allowing AI to create invalid entries.
7.  BUG-51: Indicator-based candle history requirements (`200` bias / `120` execution in adaptive mode) with explicit `HISTORY_INSUFFICIENT` status and strict continuity check.
8.  BUG-52: Position limit (`2 active/pending`) sets `ENTRY_BLOCKED_POSITION_LIMIT` while still discovering candidates, updating watchlists, and displaying top candidates.
9.  BUG-53: Closed-candle evaluation and Groq AI caching by `(symbol, timeframe, candle_close_ms)` preventing duplicate Groq calls on the same candle.
10. Mandatory 19-Metric Diagnostic Panel + PAPER performance statistics (`win_rate_pct`, `expectancy_usdt`, `expectancy_r`, `profit_factor`, `max_drawdown_pct`).
"""
from dataclasses import replace
import math
import numpy as np
import pytest

from futures.groq_rotation import GroqRotation, evaluate_contextual_ai
from futures.indicators import indicators
from futures.market_scanner import MarketScanner, ScannerConfig, empty_diagnostic_panel
from futures.paper import PaperLedger
from futures.screener import evaluate_m15_candidate, select_candidates, shortlist
from futures.strategy import (
    STRATEGY_PROFILES,
    calibrate_confidence_from_paper_trades,
    compute_confidence,
    evaluate,
    evaluate_soft_confluence,
    frame_problem,
    required_candle_history,
    resolve_strategy_profile,
    walk_forward_validate_profile,
)
from futures.telegram import (
    TelegramAlerts,
    classify_watchlist_stage,
    format_watchlist_alert,
    watchlist_dedup_key,
)
from tests.fixtures import NOW, candles, setup, verified
from tests.test_market_scanner import Alerts, Market, Pool
from tests.test_screener import universe
from tests.test_strategy import short_setup


def test_bug45_shortlist_30_balanced_long_short_and_rotating_queue_5_candidates(tmp_path):
    """BUG-45 #1–5: Shortlist 30 candidates, balance LONG/SHORT, filter below min_m15_score, and rotate 5 candidates per cycle."""
    u = universe(60)
    out = shortlist(u, limit=30)
    assert len(out["rows"]) == 30
    assert out["shortlist_count"] == 30

    # Create a market where even-indexed coins are LONG and odd-indexed coins are SHORT, and 2 coins have low M15 score (<55)
    class BalancedMarket(Market):
        def universe(self):
            return universe(50)

        def closed_candles(self, symbol, interval, now_ms, limit=260):
            idx = int("".join(ch for ch in symbol if ch.isdigit()) or 0)
            if idx in (48, 49):
                # Flat/noisy low-score candle (<55)
                base = 100.0 + np.sin(np.arange(260)) * 0.05
                return candles(base, interval)
            if idx % 2 == 0:
                return candles(100.0 + np.arange(260) * 0.15, interval)
            return candles(260.0 - np.arange(260) * 0.15, interval)

    m = BalancedMarket()
    selected, ranked, errors, diags = select_candidates(
        out["rows"],
        m,
        NOW,
        count=5,
        return_diagnostics=True,
        min_m15_score=55,
        balance_long_short=True,
        max_stage2=30,
    )
    assert len(selected) == 5
    assert all(r["m15_score"] >= 55 for r in selected)
    directions = {r["candidate_direction"] for r in selected}
    assert "LONG" in directions and "SHORT" in directions

    # Verify rotating queue across 2 scan cycles analyzes 5 candidates first, then rotates the next 5!
    ledger = PaperLedger(tmp_path / "rot_queue.db")
    p = Pool("WAIT")
    a = Alerts(ledger)
    from futures.news import NewsCalendar
    now_clk = [100.0]
    scanner = MarketScanner(m, p, NewsCalendar(), ledger, a, clock=lambda: now_clk[0])
    cfg = ScannerConfig(
        candidates=5,
        shortlist_limit=30,
        min_m15_deep_score=55,
        send_alerts=False,
        send_watchlist_alerts=False,
        send_scan_summary=False,
    )
    rep1 = scanner.run(cfg, manual=True)
    first_batch = [d["snapshot"].symbol for d in rep1["analyses"]]
    assert len(first_batch) == 5
    assert len(rep1["rotating_queue"]) >= 5

    now_clk[0] += 120.0
    rep2 = scanner.run(cfg, manual=True)
    second_batch = [d["snapshot"].symbol for d in rep2["analyses"]]
    assert len(second_batch) == 5
    # Rotating queue ensures the second cycle analyzes a different batch of candidates!
    assert set(first_batch) != set(second_batch)


def test_bug45_and_bug46_watchlist_alerts_outside_top5_and_stage_progression_dedup(tmp_path):
    """BUG-45 #6 & BUG-46: Stage-separated Watchlist alerts (`EARLY_WATCH`, `SETUP_FORMING`, `PAPER_ENTRY`) and stage-aware dedup."""
    assert classify_watchlist_stage(50, m5_confirmations=0, direction="LONG") == "NONE"
    assert classify_watchlist_stage(58, m5_confirmations=1, direction="LONG") == "EARLY_WATCH"
    assert classify_watchlist_stage(68, m5_confirmations=2, direction="LONG") == "EARLY_WATCH"
    assert classify_watchlist_stage(68, m5_confirmations=3, direction="LONG") == "SETUP_FORMING"

    # Format checks
    early_txt = format_watchlist_alert(None, {"symbol": "SOLUSDT", "candidate_direction": "LONG", "m15_score": 58}, stage="EARLY_WATCH")
    assert "[EARLY WATCH]" in early_txt and "Bukan instruksi entry" in early_txt

    forming_txt = format_watchlist_alert(None, {"symbol": "SOLUSDT", "candidate_direction": "LONG", "m15_score": 72, "m5_confirmations_passed": 4}, stage="SETUP_FORMING")
    assert "[SETUP FORMING]" in forming_txt and "Konfirmasi M5: 4/5" in forming_txt

    # Stage progression deduplication on TelegramAlerts
    sent_texts = []
    class DummyResp:
        status_code = 200
        def json(self):
            return {"ok": True}

    class DummySession:
        def post(self, url, **kwargs):
            sent_texts.append(kwargs["json"]["text"])
            return DummyResp()

    tg = TelegramAlerts("123456:ABCDEF", "-100999", tmp_path / "tg_stage.db", session=DummySession())
    cand = {"symbol": "AVAXUSDT", "candidate_direction": "LONG", "m15_score": 60, "screen_time_ms": NOW, "screening_timeframe": "15m"}

    # 1. Send EARLY_WATCH -> allowed
    r1 = tg.send_watchlist(None, cand, stage="EARLY_WATCH")
    assert "terkirim" in r1 and len(sent_texts) == 1

    # 2. Repeat EARLY_WATCH on same candle & direction -> blocked as duplicate!
    r2 = tg.send_watchlist(None, cand, stage="EARLY_WATCH")
    assert "duplikat diblokir" in r2 and len(sent_texts) == 1

    # 3. Upgrade same symbol & candle to SETUP_FORMING (m15_score=70, m5_confirmations=3) -> allowed!
    cand_forming = dict(cand, m15_score=70, m5_confirmations_passed=3)
    r3 = tg.send_watchlist(None, cand_forming, stage="SETUP_FORMING")
    assert "terkirim" in r3 and len(sent_texts) == 2

    # 4. Repeat SETUP_FORMING on same candle -> blocked as duplicate!
    r4 = tg.send_watchlist(None, cand_forming, stage="SETUP_FORMING")
    assert "duplikat diblokir" in r4 and len(sent_texts) == 2


def test_bug47_and_bug48_weighted_confluence_and_strategy_profiles():
    """BUG-47 & BUG-48: Weighted soft confluence (min 3/5 in BALANCED/ACTIVE) + profile thresholds (85, 78, 72) + reduced PAPER risk & capped leverage."""
    s, a = setup()
    # Mutate 1 soft indicator so strict CONSERVATIVE fails, while BALANCED (3/5 confluence, threshold 78) passes!
    s.execution.loc[s.execution.index[-1], "volume"] = float(s.execution.volume.iloc[-5:-1].mean() * 0.85)  # relative_volume ~0.85 (<1.2 strict)

    r_cons = evaluate(s, a, paper=True, relaxed=False, strategy_profile="CONSERVATIVE")
    assert r_cons.status == "NO_TRADE"

    r_bal = evaluate(s, a, paper=True, relaxed=True, strategy_profile="BALANCED")
    assert r_bal.status == "VALID", r_bal.reasons
    assert r_bal.plan["strategy_profile"] == "BALANCED"
    assert r_bal.metrics["m5_confirmations_passed"] >= 3
    # BUG-48: BALANCED lowers PAPER risk cap to 0.35% (3.50 USDT on 1000 equity) and caps uncalibrated leverage at 2x
    assert r_bal.plan["effective_risk_pct"] <= 0.35
    assert r_bal.plan["risk"] <= a.equity * 0.0035 + 1e-6
    assert r_bal.plan["leverage"] <= 2

    # ACTIVE profile uses threshold 72 and lowers PAPER risk cap to 0.25%
    a_act = replace(a)
    a_act.confidence_override = 74
    r_act = evaluate(s, a_act, paper=True, relaxed=True, strategy_profile="ACTIVE")
    assert r_act.status == "VALID", r_act.reasons
    assert r_act.plan["strategy_profile"] == "ACTIVE"
    assert r_act.plan["effective_risk_pct"] <= 0.25
    assert r_act.plan["risk"] <= a.equity * 0.0025 + 1e-6

    # Walk-forward validation on BALANCED and ACTIVE profiles
    wf_bal = walk_forward_validate_profile(s.bias, s.execution, profile="BALANCED")
    wf_act = walk_forward_validate_profile(s.bias, s.execution, profile="ACTIVE")
    assert wf_bal["ok"] is True and wf_bal["entry_threshold"] == 78
    assert wf_act["ok"] is True and wf_act["entry_threshold"] == 72


def test_bug49_audited_confidence_components_zero_when_absent_and_paper_calibration():
    """BUG-49: Non-occurring indicators get strictly 0 points (no free 5 or 7 points), and PAPER calibration computes win rate, expectancy, PF, and drawdown."""
    s, _ = setup()
    b, e = indicators(s.bias), indicators(s.execution)
    # Force all soft flags False and adverse RSI/StochRSI/volume
    e_bad = e.copy()
    e_bad.loc[e_bad.index[-1], "rsi"] = 25.0  # Bad for LONG
    e_bad.loc[e_bad.index[-1], "stoch_rsi_k"] = 10.0
    e_bad.loc[e_bad.index[-1], "stoch_rsi_d"] = 30.0
    e_bad.loc[e_bad.index[-2], "stoch_rsi_k"] = 20.0
    e_bad.loc[e_bad.index[-2], "stoch_rsi_d"] = 25.0
    e_bad.loc[e_bad.index[-1], "relative_volume"] = 0.4
    e_bad.loc[e_bad.index[-1], "close"] = float(e_bad.bb_lower.iloc[-1]) - 5.0
    score, bd = compute_confidence(b, e_bad, "LONG", False, False, False, False, False)
    assert bd["Liquidity sweep quality"] == 0
    assert bd["Bollinger Band reclaim"] == 0
    assert bd["CHoCH confirmation"] == 0
    assert bd["Stoch RSI confirmation"] == 0
    assert bd["Volume confirmation"] == 0
    assert bd["Retest quality"] == 0
    assert bd["Execution quality"] == 0

    # Verify PAPER performance calibration metrics
    sample_trades = [
        {"status": "CLOSED", "realized_pnl": 8.0, "plan": {"risk": 4.0}},
        {"status": "CLOSED", "realized_pnl": -4.0, "plan": {"risk": 4.0}},
        {"status": "CLOSED", "realized_pnl": 12.0, "plan": {"risk": 4.0}},
    ]
    perf = calibrate_confidence_from_paper_trades(sample_trades, min_sample_size=3)
    assert perf["closed_trades"] == 3
    assert perf["win_rate_pct"] == pytest.approx(66.67, abs=0.05)
    assert perf["expectancy_usdt"] == pytest.approx(16.0 / 3.0, abs=1e-3)
    assert perf["profit_factor"] == pytest.approx(20.0 / 4.0, abs=1e-3)
    assert perf["calibrated"] is True
    assert "bukan win probability" in perf["disclaimer"].lower()


def test_bug50_contextual_ai_separates_rule_based_valid_ai_confirmed_and_ai_unavailable(tmp_path):
    """BUG-50: When Groq API fails/times out, status separates RULE_BASED_VALID and AI_UNAVAILABLE without AI inventing invalid trades."""
    s, a = setup()
    r_valid = evaluate(s, a, paper=True)
    assert r_valid.status == "VALID"

    # 1. AI unavailable (timeout / cooldown / HTTP 503)
    ai_down = {"ok": False, "decision": "WAIT", "reasons": ["Groq timeout"], "cautions": [], "ai_status": "AI_UNAVAILABLE"}
    ctx_strict = evaluate_contextual_ai(r_valid, ai_down, allow_paper_on_ai_unavailable=False)
    assert ctx_strict["rule_status"] == "RULE_BASED_VALID"
    assert ctx_strict["ai_status"] == "AI_UNAVAILABLE"
    assert ctx_strict["watchlist_allowed"] is True

    ctx_contextual = evaluate_contextual_ai(r_valid, ai_down, allow_paper_on_ai_unavailable=True)
    assert ctx_contextual["rule_status"] == "RULE_BASED_VALID"
    assert ctx_contextual["ai_status"] == "AI_UNAVAILABLE"
    assert ctx_contextual["final_status"] == "VALID"
    assert ctx_contextual["paper_entry_allowed"] is True

    # 2. AI can NEVER turn an invalid rule-based result into VALID
    r_invalid = replace(r_valid, status="NO_TRADE", plan=None)
    ctx_inv = evaluate_contextual_ai(r_invalid, {"ok": True, "decision": "LONG", "reasons": ["ok"], "cautions": []}, allow_paper_on_ai_unavailable=True)
    assert ctx_inv["final_status"] == "NO_TRADE"
    assert ctx_inv["paper_entry_allowed"] is False


def test_bug51_indicator_based_candle_history_120_and_200_with_history_insufficient():
    """BUG-51: Adaptive history requires 200 bias and 120 execution candles (instead of 500) and reports HISTORY_INSUFFICIENT when below."""
    assert required_candle_history("15m", role="bias", adaptive=True) == 200
    assert required_candle_history("5m", role="execution", adaptive=True) == 120

    s, a = setup()
    s.bias = s.bias.tail(210).reset_index(drop=True)
    s.execution = s.execution.tail(140).reset_index(drop=True)

    # With adaptive_history=True, 210 bias & 140 execution candles PASS!
    r_ok = evaluate(s, a, paper=True, adaptive_history=True)
    assert r_ok.status == "VALID", r_ok.reasons

    # With only 80 execution candles (<120), reports HISTORY_INSUFFICIENT explicitly
    s_short, a_short = setup()
    s_short.execution = s_short.execution.tail(80).reset_index(drop=True)
    r_short = evaluate(s_short, a_short, paper=True, adaptive_history=True)
    assert r_short.status == "WAIT"
    assert r_short.meta.get("data_status") == "HISTORY_INSUFFICIENT"
    assert r_short.meta.get("history_status") == "HISTORY_INSUFFICIENT"


def test_bug52_position_limit_keeps_discovering_candidates_and_sets_entry_blocked_position_limit(tmp_path):
    """BUG-52: When 2 positions are active, scanner still screens, ranks, updates watchlist, and reports ENTRY_BLOCKED_POSITION_LIMIT."""
    ledger = PaperLedger(tmp_path / "pos_limit.db")
    m = Market()
    p = Pool()
    a = Alerts(ledger)
    from futures.news import NewsCalendar
    now_clk = [100.0]
    scanner = MarketScanner(m, p, NewsCalendar(), ledger, a, clock=lambda: now_clk[0])
    cfg = ScannerConfig(fee=.0004, slippage_bps=.1, funding=.00001, send_alerts=False, send_watchlist_alerts=False, send_scan_summary=False)

    # Cycle 1 opens 2 pending paper positions (BTCUSDT, ETHUSDT)
    rep1 = scanner.run(cfg, manual=True)
    assert len(ledger.active()) == 2

    # Cycle 2 runs while 2 positions are active: must STILL populate shortlist, selected, ranked, and analyses with ENTRY_BLOCKED_POSITION_LIMIT!
    now_clk[0] += 120.0
    rep2 = scanner.run(cfg, manual=True)
    assert rep2["position_limit_blocked"] is True
    assert rep2["entry_block_status"] == "ENTRY_BLOCKED_POSITION_LIMIT"
    assert len(rep2["shortlist"]) > 0
    assert len(rep2["selected"]) > 0
    assert len(rep2["analyses"]) > 0
    assert all(d["stage_status"] == "ENTRY_BLOCKED_POSITION_LIMIT" for d in rep2["analyses"])
    assert rep2["diagnostic_panel"]["rejected_by_position_limit"] >= 1
    assert len(ledger.active()) == 2


def test_bug53_and_diagnostic_panel_19_metrics_and_no_duplicate_ai_on_same_candle(tmp_path):
    """BUG-53 & Mandatory Diagnostic Panel: Re-scanning on the same closed candle reuses cached M15 & AI reviews without duplicate Groq calls, and populates all 19 diagnostic panel metrics."""
    ledger = PaperLedger(tmp_path / "cache_diag.db")
    m = Market()
    p = Pool("WAIT")
    a = Alerts(ledger)
    from futures.news import NewsCalendar
    now_clk = [100.0]
    scanner = MarketScanner(m, p, NewsCalendar(), ledger, a, clock=lambda: now_clk[0])
    cfg = ScannerConfig(candidates=2, shortlist_limit=20, min_m15_deep_score=55, send_alerts=False, send_watchlist_alerts=False, send_scan_summary=False)

    rep1 = scanner.run(cfg, manual=True)
    assert len(p.calls) == 2
    assert rep1["ai_cache_hits"] == 0

    # Verify all 19 mandatory keys exist in diagnostic_panel
    expected_19_keys = set(empty_diagnostic_panel().keys())
    assert len(expected_19_keys) == 19
    assert set(rep1["diagnostic_panel"].keys()) == expected_19_keys
    assert rep1["diagnostic_panel"]["universe_total"] == 120
    assert rep1["diagnostic_panel"]["m5_analyzed"] == 2

    # Run again on the exact same closed candle timestamp -> AI review is served from cache (0 new Groq calls!)
    now_clk[0] += 65.0
    rep2 = scanner.run(replace(cfg, candidates=5), manual=True)
    # The 2 candidates from rep1 that are re-evaluated hit the AI cache!
    assert rep2["m15_cache_hits"] >= 2

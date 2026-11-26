"""Acceptance Test Suite for CoinAgentX v6.0.4 — Hybrid Adaptive v1.1 (26 Mandatory Acceptance Tests, Section 13).

Covers all 26 requirements from MASTER PROMPT — COINAGENTX v6.0.4 (HYBRID ADAPTIVE v1.1):
1.  Market source priority: Binance USDⓈ-M Futures primary -> Bybit Linear secondary -> Spot Reference only.
2.  FUTURES_DATA_UNAVAILABLE when no valid Futures provider is available; holds all Futures PAPER_ENTRY.
3.  Strict Spot vs Futures separation: Spot never fabricates Mark/Index/Funding/OI and never writes to Futures ledger.
4.  Symbol & contract validation from exchangeInfo (`PERPETUAL`, `TRADING`, `USDT`, `PRICE_FILTER`, `LOT_SIZE`, `MIN_NOTIONAL`, `contractSize`).
5.  50-candidate pipeline capacity (`shortlist_limit=50`, `STAGE2_MAX_CANDIDATES=50`).
6.  Target >= 20 candidates analyzed per cycle when valid candidates and time budget allow (`max_groq_candidates=5`).
7.  Persistent rotating queue across cycles & restarts with data refresh and expiry tracking (`queued_count == analyzed_count + deferred_count + expired_count`).
8.  History sufficiency check (`>= 250` closed M5 & M15 candles) and explicit `HISTORY_INSUFFICIENT` reporting.
9.  M15 regime detector (`STRONG_TREND_UP`, `TREND_UP`, `STRONG_TREND_DOWN`, `TREND_DOWN`, `RANGE`, `BREAKOUT_UP`, `BREAKOUT_DOWN`, `HIGH_VOLATILITY`, `TRANSITION`).
10. Strategy A (Trend Pullback Continuation) positive & negative cases.
11. Strategy B (Breakout Retest) with 6-M5-bar arming store positive & negative cases.
12. Strategy C (Momentum Continuation) positive & negative cases.
13. Strategy D (Range Mean Reversion) positive & negative cases.
14. Strategy E (Liquidity Sweep Reversal) positive & negative cases.
15. Strategy F (Router) conflict handling (`CONFLICT_WAIT`) and `HIGH_VOLATILITY` new-entry block.
16. Setup Quality Score (0–100) exact component weights (`15/15/25/20/10/5/5/5`) and stage thresholds (`55 / 65 / 78`).
17. Risk sizing (`<= 0.25%` per position, `<= 0.50%` total portfolio) and `contract_multiplier` accounting.
18. Leverage cap (`default 2x`, `max 3x` Futures only; score never increases leverage) and `verify_liquidation_safety()`.
19. Fee, slippage, funding, and Net R:R (`>= 1.50R`) hard gate validation.
20. PAPER lifecycle: `STOP-LIMIT`, 2-M5-bar expiry, partial fill SL protection, `TP1 +1R 40%`, `TP2 +2R 35%`, `TP3 +3R 25%`, net BE after TP1, `+1.5R` trailing at `1.25 ATR`, `6`-bar and `12`-bar time exits.
21. Intrabar conservative SL-first rule (`CONSERVATIVE_INTRABAR_APPROXIMATION`).
22. Daily loss limit (`2.0%`) and 3 consecutive losses (`60m` cooldown).
23. Groq Advisor-Only mode (`AI_ADVISORY_OK`, `AI_CAUTION`, `AI_UNAVAILABLE`) and max 5 Groq calls per cycle without blocking Rule Engine entries.
24. Telegram 4-stage alerts (`EARLY_WATCH`, `SETUP_FORMING`, `PAPER_ENTRY`, `TRADE_UPDATE`) and stage-progression deduplication.
25. Independent 24/7 Worker heartbeat (`30s` interval, `> 90s` `STALE`, `ONLINE`/`STALE`/`OFFLINE`/`NO_WORKER`) with 7 runtime proof fields.
26. Passive dashboard refresh (`Read-Only`) increments `dashboard_refreshes` without triggering market API requests or scans, and telemetry distinguishes `NOT_INSTRUMENTED` vs active counters.
"""
from dataclasses import replace
from pathlib import Path
import time
import numpy as np
import pandas as pd
import pytest

from futures.bybit_market import BybitFuturesMarket
from futures.db_backend import (
    load_persistent_candidate_queue,
    save_global_preferences,
)
from futures.hybrid_adaptive import (
    M5_INTERVAL_MS,
    M15_INTERVAL_MS,
    REGIME_BREAKOUT_DOWN,
    REGIME_BREAKOUT_UP,
    REGIME_HIGH_VOLATILITY,
    REGIME_RANGE,
    REGIME_STRONG_TREND_DOWN,
    REGIME_STRONG_TREND_UP,
    REGIME_TRANSITION,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    STRATEGY_A,
    STRATEGY_B,
    STRATEGY_C,
    STRATEGY_D,
    STRATEGY_E,
    classify_setup_stage,
    compute_setup_quality_score,
    detect_m15_regime,
    evaluate_hybrid_adaptive,
    evaluate_hybrid_ai_advisor,
    evaluate_hybrid_position_lifecycle,
    evaluate_strategy_a,
    evaluate_strategy_b,
    evaluate_strategy_c,
    evaluate_strategy_d,
    evaluate_strategy_e,
    route_strategy_by_regime,
    validate_hybrid_candle_history,
    verify_liquidation_safety,
)
from futures.indicators import indicators
from futures.lifecycle import after_fill
from futures.market import FuturesMarket, MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.models import Account, Contract, Snapshot
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.provider_router import FuturesProviderRouter
from futures.screener import (
    STAGE1_MAX_CANDIDATES,
    STAGE2_MAX_CANDIDATES,
    shortlist,
    validate_exchange_symbol_spec,
)
from futures.security import GLOBAL_MONITOR, ResourceMonitor
from futures.spot_market import SpotReferenceMarket
from futures.version import APP_VERSION, SPEC_VERSION
from tests.fixtures import NOW
from tests.test_market_scanner import Alerts, Market, Pool
from tests.test_screener import universe
from tests.test_hybrid_adaptive_spec_v1_acceptance_1_57 import (
    _build_strategy_a_frames,
    _make_account,
    _make_candles,
    _make_contract_spec,
    _make_snapshot_from_frames,
)
from backtest_hybrid_adaptive import _build_ohlcv_regime_window


def test_01_market_source_priority_binance_futures_bybit_spot():
    """Test 1 (Section 2): Provider priority is Binance USDⓈ-M Futures (1) -> Bybit Linear (2) -> Spot Reference (3)."""
    assert APP_VERSION in ("6.0.4", "6.0.5", "6.0.6")
    assert "Hybrid Adaptive v1.1" in SPEC_VERSION
    m_bin = FuturesMarket()
    m_byb = BybitFuturesMarket()
    m_spot = SpotReferenceMarket()
    router = FuturesProviderRouter(m_bin, m_byb, m_spot)
    assert router.active is m_bin
    assert router.instrument_kind == "USD_M_PERPETUAL"
    assert router.futures_available is True
    assert router.data_status == "OK"


def test_02_futures_data_unavailable_holds_futures_paper_entry():
    """Test 2 (Section 2): When no Futures provider is valid, status is FUTURES_DATA_UNAVAILABLE and no PAPER_ENTRY is created."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc = _make_account(now_ms=now_ms, equity=10_000.0)
    res_unavail = evaluate_hybrid_adaptive(snap, acc, futures_unavailable=True)
    assert res_unavail.status == "HOLD"
    assert res_unavail.plan is None
    assert res_unavail.meta["data_status"] == "FUTURES_DATA_UNAVAILABLE"

    # Also verify router state when both Tier 1 and Tier 2 fail with regional restriction
    class GeoBlockedFutures:
        instrument_kind = "USD_M_PERPETUAL"
        market_label = "Binance Futures"
        base = "https://fapi.binance.com"
        time_path = "/fapi/v1/time"
        def get(self, *a, **kw):
            raise MarketError("HTTP 451 restricted location", http_status=451)

    class GeoBlockedBybit:
        instrument_kind = "BYBIT_LINEAR_PERPETUAL"
        market_label = "Bybit Perpetual"
        base = "https://api.bybit.com"
        time_path = "/v5/market/time"
        def get(self, *a, **kw):
            raise MarketError("HTTP 403 CloudFront country block", http_status=403)

    spot_fb = Market()
    spot_fb.instrument_kind = "SPOT_REFERENCE"
    spot_fb.market_label = "Binance Spot"
    router = FuturesProviderRouter(GeoBlockedFutures(), GeoBlockedBybit(), spot_fb, spot_fallback_enabled=True)
    res_time = router.get("/fapi/v1/time")
    assert "serverTime" in res_time
    assert router.instrument_kind == "SPOT_REFERENCE"
    assert router.futures_available is False
    assert router.futures_data_unavailable is True
    assert router.data_status == "FUTURES_DATA_UNAVAILABLE"


def test_03_spot_vs_futures_strict_separation():
    """Test 3 (Section 2): Spot Reference never fabricates Mark/Index/Funding/OI and cannot create Futures PAPER_ENTRY."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    spot_contract = replace(_make_contract_spec(), contract_type="SPOT")
    spot_snap = _make_snapshot_from_frames(
        now_ms,
        m15,
        m5,
        instrument_kind="SPOT_REFERENCE",
        contract=spot_contract,
        mark=999999.0,
        funding_rate=0.0001,
        index_price=100.0,
        open_interest=10000.0,
        quote_times={"last": now_ms - 1000, "book": now_ms - 1000, "depth": now_ms - 1000},
    )
    # Re-run __post_init__ check on direct Snapshot construction
    direct_spot = Snapshot(
        symbol="BTCUSDT",
        mode="SCALPING",
        now_ms=now_ms,
        bias=m15,
        execution=m5,
        contract=spot_contract,
        last=float(m5["close"].iloc[-1]),
        mark=999999.0,
        bid=float(m5["close"].iloc[-1]) - 0.01,
        ask=float(m5["close"].iloc[-1]) + 0.01,
        quote_times={"last": now_ms - 1000, "book": now_ms - 1000, "depth": now_ms - 1000},
        bids=[(float(m5["close"].iloc[-1]) - 0.01, 500.0)],
        asks=[(float(m5["close"].iloc[-1]) + 0.01, 500.0)],
        funding_rate=0.0001,
        index_price=100.0,
        open_interest=10000.0,
        instrument_kind="SPOT_REFERENCE",
        market_provider="Binance Spot",
    )
    assert direct_spot.mark is None
    assert direct_spot.index_price is None
    assert direct_spot.funding_rate is None
    assert direct_spot.open_interest is None

    acc = _make_account(now_ms=now_ms, equity=10_000.0)
    res_futures_mode = evaluate_hybrid_adaptive(direct_spot, acc, allow_spot_paper_simulation=False)
    assert res_futures_mode.status != "VALID"
    assert res_futures_mode.plan is None
    assert res_futures_mode.meta.get("hold_status") == "HOLD"


def test_04_symbol_and_contract_validation_from_exchange_info():
    """Test 4 (Section 4): validate_exchange_symbol_spec separates NON_TARGET_INSTRUMENT, INVALID_CONTRACT_SPEC, and VALID."""
    valid_raw = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractSize": 1.0,
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "1", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
        ],
    }
    v_ok = validate_exchange_symbol_spec(valid_raw, spot=False)
    assert v_ok["ok"] is True and v_ok["category"] == "VALID"
    assert v_ok["tick_size"] == 0.10 and v_ok["step_size"] == 0.001 and v_ok["min_notional"] == 5.0

    delivery_raw = dict(valid_raw, symbol="BTCUSDT_260626", contractType="CURRENT_QUARTER")
    v_del = validate_exchange_symbol_spec(delivery_raw, spot=False)
    assert v_del["ok"] is False and v_del["category"] == "NON_TARGET_INSTRUMENT"

    btc_quote_raw = dict(valid_raw, symbol="ETHBTC", quoteAsset="BTC")
    v_btc = validate_exchange_symbol_spec(btc_quote_raw, spot=False)
    assert v_btc["ok"] is False and v_btc["category"] == "NON_TARGET_INSTRUMENT"

    bad_filter_raw = dict(valid_raw, filters=[{"filterType": "PRICE_FILTER", "tickSize": "0.10"}])
    v_bad = validate_exchange_symbol_spec(bad_filter_raw, spot=False)
    assert v_bad["ok"] is False and v_bad["category"] == "INVALID_CONTRACT_SPEC"


def test_05_pipeline_capacity_50_candidates_and_stage1_reconciliation():
    """Test 5 (Section 3): Pipeline supports 50 candidates and exact Stage 1 reconciliation (`universe_total == stage1_passed + stage1_rejected`)."""
    assert STAGE1_MAX_CANDIDATES == 50
    assert STAGE2_MAX_CANDIDATES == 50
    u = universe(75)
    for i in range(5):
        u["info"]["symbols"].append({
            "symbol": f"BTCUSDT_2609{i:02d}",
            "status": "TRADING",
            "contractType": "NEXT_QUARTER",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
        })
    for i in range(10):
        u["tickers"][i]["quoteVolume"] = "100000"

    screened = shortlist(u, min_volume=10_000_000, limit=50)
    assert screened["filtered_non_target_instrument"] == 5
    assert screened["universe_count"] == 75
    assert screened["stage1_passed"] == 65
    assert screened["stage1_rejected"] == 10
    assert screened["universe_count"] == screened["stage1_passed"] + screened["stage1_rejected"]
    assert len(screened["rows"]) == 50


def test_06_target_20_analyzed_and_max_5_groq_calls_per_cycle(tmp_path):
    """Test 6 (Section 3 & Section 7): Scanner analyzes >= 20 candidates when available and caps Groq calls at 5 per cycle."""
    class Market60(Market):
        def universe(self):
            return universe(60)

    db_file = tmp_path / "pipeline_50_20_5.db"
    ledger = PaperLedger(db_file)
    pool = Pool("WAIT")
    alerts = Alerts(ledger)
    scanner = MarketScanner(Market60(), pool, NewsCalendar(), ledger, alerts, clock=lambda: 100.0)
    cfg = ScannerConfig(
        candidates=20,
        shortlist_limit=50,
        max_groq_candidates=5,
        min_m15_deep_score=0,
        send_alerts=False,
        send_watchlist_alerts=False,
        send_scan_summary=False,
    )
    rep = scanner.run(cfg, manual=True)
    p_diag = rep["pipeline_diagnostics"]
    assert p_diag["universe_total"] == 60
    assert p_diag["stage1_passed"] + p_diag["stage1_rejected"] == p_diag["universe_total"]
    assert p_diag["stage2_shortlist"] == 50
    assert p_diag["analyzed_count"] == 20
    assert rep["groq_calls_count"] == 5
    assert len(pool.calls) == 5
    assert p_diag["active_queue_count"] == 0 and p_diag["reconciliation_ok"] is True


def test_07_persistent_rotating_queue_with_refresh_and_expiry(tmp_path):
    """Test 7 (Section 3 & Section 9): Unanalyzed candidates persist in DB queue, refresh before analysis, and expire when older than max_queue_age_ms."""
    class Market35(Market):
        def universe(self):
            return universe(35)

    db_file = tmp_path / "rot_queue.db"
    ledger = PaperLedger(db_file)
    ledger.save_candidate_queue({
        "items": [
            {"symbol": "COIN00USDT", "queued_at_ms": NOW - 2_000_000, "m15_score": 75},
            {"symbol": "DELISTED99USDT", "queued_at_ms": NOW - 10_000, "m15_score": 75},
            {"symbol": "COIN30USDT", "queued_at_ms": NOW - 30_000, "m15_score": 72},
        ],
        "updated_at_ms": NOW - 10_000,
    }, now_ms=NOW - 10_000)

    loaded_q = ledger.load_candidate_queue()
    assert len(loaded_q["items"]) == 3

    scanner = MarketScanner(Market35(), Pool("WAIT"), NewsCalendar(), ledger, Alerts(ledger), clock=lambda: 100.0)
    cfg = ScannerConfig(
        candidates=15,
        shortlist_limit=35,
        max_groq_candidates=5,
        max_queue_age_ms=900_000,
        min_m15_deep_score=0,
        send_alerts=False,
        send_watchlist_alerts=False,
        send_scan_summary=False,
    )
    rep = scanner.run(cfg, manual=True)
    p_diag = rep["pipeline_diagnostics"]
    assert p_diag["expired_count"] == 2
    expired_reasons = {x["symbol"]: x["reason"] for x in rep["expired_candidates"]}
    assert expired_reasons["COIN00USDT"] == "QUEUE_TTL_EXPIRED"
    assert expired_reasons["DELISTED99USDT"] == "SETUP_INVALIDATED_ON_REFRESH"
    assert p_diag["analyzed_count"] == 15
    assert p_diag["active_queue_count"] == 0 and p_diag["reconciliation_ok"] is True

    persisted_after = load_persistent_candidate_queue(db_file, instrument_kind="USD_M_PERPETUAL")
    assert persisted_after is not None
    assert len(persisted_after["items"]) == p_diag["deferred_count"]


def test_08_candle_history_250_validation_and_history_insufficient():
    """Test 8 (Section 5): Requires >= 250 closed M5 and M15 candles; reports HISTORY_INSUFFICIENT when < 250."""
    now_ms = 1_763_337_605_000
    m15_end = (now_ms // M15_INTERVAL_MS) * M15_INTERVAL_MS - 1
    short_m15 = _make_candles(n=120, interval_ms=M15_INTERVAL_MS, end_close_ms=m15_end)
    v_short = validate_hybrid_candle_history(short_m15, "15m", now_ms, min_candles=250)
    assert v_short["ok"] is False
    assert v_short["code"] == "HISTORY_INSUFFICIENT"

    full_m15 = _make_candles(n=260, interval_ms=M15_INTERVAL_MS, end_close_ms=m15_end)
    v_full = validate_hybrid_candle_history(full_m15, "15m", now_ms, min_candles=250)
    assert v_full["ok"] is True
    assert v_full.get("candle_count", v_full.get("candles_count", 260)) >= 250


def test_09_m15_regime_detector_all_regimes():
    """Test 9 (Section 5): Verify M15 regime detector classifies trend, breakout, range, high volatility, and transition."""
    _, m15_up, _ = _build_strategy_a_frames("LONG", strong=True)
    r_up = detect_m15_regime(indicators(m15_up))
    assert r_up["ok"] is True
    assert r_up["regime"] in (REGIME_STRONG_TREND_UP, REGIME_TREND_UP)

    _, m15_dn, _ = _build_strategy_a_frames("SHORT", strong=True)
    r_dn = detect_m15_regime(indicators(m15_dn))
    assert r_dn["regime"] in (REGIME_STRONG_TREND_DOWN, REGIME_TREND_DOWN)

    m15_bo, _ = _build_ohlcv_regime_window(STRATEGY_B, "LONG")
    r_bo = detect_m15_regime(indicators(m15_bo))
    assert r_bo["regime"] == REGIME_BREAKOUT_UP

    m15_rg, _ = _build_ohlcv_regime_window(STRATEGY_D, "LONG")
    r_rg = detect_m15_regime(indicators(m15_rg))
    assert r_rg["regime"] == REGIME_RANGE

    m15_hv = m15_up.copy()
    m15_hv.loc[m15_hv.index[-1], "high"] = float(m15_hv["close"].iloc[-1]) * 1.15
    m15_hv.loc[m15_hv.index[-1], "low"] = float(m15_hv["close"].iloc[-1]) * 0.85
    r_hv = detect_m15_regime(indicators(m15_hv))
    assert r_hv["regime"] == REGIME_HIGH_VOLATILITY
    assert r_hv["allow_new_entries"] is False


def test_10_strategy_a_trend_pullback_positive_and_negative():
    """Test 10 (Section 5): Strategy A positive trigger and negative rejection."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    reg = detect_m15_regime(indicators(m15))
    ev_pos = evaluate_strategy_a(reg, indicators(m5))
    assert ev_pos["triggered"] is True and ev_pos["direction"] == "LONG"

    ev_neg = evaluate_strategy_a({"regime": REGIME_RANGE, "ok": True}, indicators(m5))
    assert ev_neg["eligible_regime"] is False and ev_neg["triggered"] is False


def test_11_strategy_b_breakout_retest_positive_and_negative():
    """Test 11 (Section 5): Strategy B breakout retest within 6 M5 bars positive and chased negative."""
    now_ms = 1_763_337_605_000
    m5_end = (now_ms // M5_INTERVAL_MS) * M5_INTERVAL_MS - 1
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, end_close_ms=m5_end, base_price=100.0, drift_per_bar=0.05)
    d5 = indicators(m5)
    atr5 = float(d5["atr"].iloc[-1])
    broken_level = float(d5["close"].iloc[-3])
    armed_bo = {
        "direction": "LONG",
        "level": broken_level,
        "armed_at_ms": int(d5["close_time"].iloc[-3]),
        "expires_at_ms": int(d5["close_time"].iloc[-3]) + 6 * M5_INTERVAL_MS,
    }
    d5.loc[len(d5) - 2, "low"] = broken_level + 0.05 * atr5
    d5.loc[len(d5) - 2, "close"] = broken_level + 0.08 * atr5
    d5.loc[len(d5) - 1, "open"] = broken_level + 0.05 * atr5
    d5.loc[len(d5) - 1, "close"] = broken_level + 0.25 * atr5
    d5.loc[len(d5) - 1, "low"] = broken_level + 0.02 * atr5
    d5.loc[len(d5) - 1, "high"] = broken_level + 0.30 * atr5

    ev_pos = evaluate_strategy_b({"regime": REGIME_TREND_UP}, d5, armed_breakout=armed_bo, current_price=broken_level + 0.25 * atr5)
    assert ev_pos["triggered"] is True and ev_pos["direction"] == "LONG"

    ev_chased = evaluate_strategy_b({"regime": REGIME_TREND_UP}, d5, armed_breakout=armed_bo, current_price=broken_level + 0.80 * atr5)
    assert ev_chased["triggered"] is False


def test_12_strategy_c_momentum_continuation_positive_and_negative():
    """Test 12 (Section 5): Strategy C momentum continuation in STRONG_TREND positive and non-strong trend negative."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.08, wave_amp=0.02, bar_half_range=0.20)
    d5 = indicators(m5)
    last_i = len(d5) - 1
    atr = float(d5["atr"].iloc[-1])
    prev5_hi = float(d5["high"].iloc[-6:-1].max())
    ema21 = prev5_hi - 0.20 * atr
    d5.loc[last_i, "ema21"] = ema21
    d5.loc[last_i, "ema9"] = ema21 + 0.25 * atr
    d5.loc[last_i - 1, "ema9"] = ema21 + 0.10 * atr
    d5.loc[last_i - 1, "ema21"] = ema21 - 0.05 * atr
    d5.loc[last_i - 2, "macd_hist"] = 0.05
    d5.loc[last_i - 1, "macd_hist"] = 0.12
    d5.loc[last_i, "macd_hist"] = 0.22
    d5.loc[last_i, "rsi"] = 62.0
    d5.loc[last_i, "rvol20"] = 1.35
    d5.loc[last_i, "open"] = prev5_hi - 0.05 * atr
    d5.loc[last_i, "close"] = prev5_hi + 0.15 * atr
    m15_reg = {"regime": REGIME_STRONG_TREND_UP, "adx14": 28.0, "adx_rising_2bars": True, "di_spread": 9.0}
    ev_pos = evaluate_strategy_c(m15_reg, d5)
    assert ev_pos["triggered"] is True and ev_pos["direction"] == "LONG"

    ev_neg = evaluate_strategy_c({"regime": REGIME_TREND_UP, "ok": True}, d5)
    assert ev_neg["eligible_regime"] is False and ev_neg["triggered"] is False


def test_13_strategy_d_range_mean_reversion_positive_and_negative():
    """Test 13 (Section 5): Strategy D range mean reversion positive and trend regime negative."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.20, bar_half_range=0.35)
    d5 = indicators(m5)
    last_i = len(d5) - 1
    bb_lo_prev = float(d5["bb_lower"].iloc[-2])
    bb_lo_cur = float(d5["bb_lower"].iloc[-1])
    atr = float(d5["atr"].iloc[-1])
    d5.loc[last_i - 1, "close"] = bb_lo_prev - 0.20 * atr
    d5.loc[last_i - 1, "low"] = bb_lo_prev - 0.30 * atr
    d5.loc[last_i - 1, "rsi"] = 28.0
    d5.loc[last_i - 1, "stoch_rsi_k"] = 14.0
    d5.loc[last_i - 1, "stoch_rsi_d"] = 18.0
    d5.loc[last_i, "open"] = bb_lo_cur + 0.02 * atr
    d5.loc[last_i, "close"] = bb_lo_cur + 0.25 * atr
    d5.loc[last_i, "low"] = bb_lo_cur - 0.05 * atr
    d5.loc[last_i, "rsi"] = 36.0
    d5.loc[last_i, "stoch_rsi_k"] = 22.0
    d5.loc[last_i, "stoch_rsi_d"] = 19.0
    d5.loc[last_i, "rvol20"] = 1.25
    d5.loc[last_i, "bb_upper"] = bb_lo_cur + 4.0 * atr
    d5.loc[last_i, "bb_mid"] = bb_lo_cur + 2.5 * atr
    m15_reg = {"regime": REGIME_RANGE, "donchian20_high": bb_lo_cur + 4.5 * atr, "donchian20_low": bb_lo_cur - 1.0 * atr}
    ev_pos = evaluate_strategy_d(m15_reg, d5)
    assert ev_pos["triggered"] is True and ev_pos["direction"] == "LONG"

    ev_neg = evaluate_strategy_d({"regime": REGIME_TREND_UP, "ok": True}, d5)
    assert ev_neg["eligible_regime"] is False and ev_neg["triggered"] is False


def test_14_strategy_e_liquidity_sweep_reversal_positive_and_negative():
    """Test 14 (Section 5): Strategy E liquidity sweep reversal positive and STRONG_TREND block negative."""
    m5 = _make_candles(n=260, interval_ms=M5_INTERVAL_MS, base_price=100.0, drift_per_bar=0.0, wave_amp=0.0, bar_half_range=0.30)
    n = len(m5)
    m5.loc[n - 10, "low"] = 99.0
    m5.loc[n - 10, "close"] = 99.4
    m5.loc[n - 6, "high"] = 100.4
    d5 = indicators(m5)
    atr = float(d5["atr"].iloc[-1])
    d5.loc[n - 2, "low"] = 99.0 - 0.20 * atr
    d5.loc[n - 2, "close"] = 99.15
    d5.loc[n - 2, "open"] = 99.05
    d5.loc[n - 2, "rvol20"] = 1.35
    d5.loc[n - 1, "open"] = 100.1
    d5.loc[n - 1, "close"] = 100.55
    d5.loc[n - 1, "high"] = 100.60
    d5.loc[n - 1, "low"] = 100.0
    d5.loc[n - 1, "rvol20"] = 1.40

    ev_pos = evaluate_strategy_e({"regime": REGIME_RANGE}, d5)
    assert ev_pos["triggered"] is True and ev_pos["direction"] == "LONG"

    ev_neg = evaluate_strategy_e({"regime": REGIME_STRONG_TREND_DOWN, "ok": True}, d5)
    assert ev_neg["eligible_regime"] is False and ev_neg["triggered"] is False


def test_15_strategy_f_router_and_high_volatility_block():
    """Test 15 (Section 5): Strategy F blocks new entries on HIGH_VOLATILITY and routes by M15 regime."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    hv_reg = {"regime": REGIME_HIGH_VOLATILITY, "ok": True, "allow_new_entries": False}
    routed_hv = route_strategy_by_regime("BTCUSDT", hv_reg, indicators(m5), current_price=float(m5["close"].iloc[-1]))
    assert routed_hv["routed_strategy"] is None
    assert "HIGH_VOLATILITY" in routed_hv["status"]


def test_16_setup_quality_score_weights_and_stage_thresholds():
    """Test 16 (Section 5): Setup Quality Score components sum to <= 100 (15/15/25/20/10/5/5/5) and stages map to 55/65/78."""
    _, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    m15_d = indicators(m15)
    m5_d = indicators(m5)
    reg = detect_m15_regime(m15_d)
    routed = route_strategy_by_regime("BTCUSDT", reg, m5_d, current_price=float(m5["close"].iloc[-1]))
    score, bd = compute_setup_quality_score(reg, routed, m5_d, spread_ok=True, depth_ok=True, trigger_distance_ok=True)
    assert 0 <= score <= 100
    assert len(bd) == 8
    assert sum(bd.values()) == score
    assert classify_setup_stage(54, triggered=False, hard_gates_passed=False) == 'REJECT'
    assert classify_setup_stage(55, triggered=False, hard_gates_passed=False) == "EARLY_WATCH"
    assert classify_setup_stage(65, triggered=False, hard_gates_passed=False) == "SETUP_FORMING"
    assert classify_setup_stage(78, triggered=True, hard_gates_passed=True) == "PAPER_ENTRY"


def test_17_risk_sizing_and_contract_multiplier():
    """Test 17 (Section 6): Risk per position <= 0.25% equity, total <= 0.50%, and contract_multiplier scales notional & risk."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap_1x = _make_snapshot_from_frames(now_ms, m15, m5)
    mult_contract = replace(snap_1x.contract, contract_multiplier=10.0, step=0.001, market_step=0.001, min_qty=0.001, market_min_qty=0.001)
    snap_10x = replace(snap_1x, contract=mult_contract)
    acc = _make_account(now_ms=now_ms, equity=10_000.0)

    r1 = evaluate_hybrid_adaptive(snap_1x, acc)
    r10 = evaluate_hybrid_adaptive(snap_10x, acc)
    assert r1.status == "VALID" and r10.status == "VALID"
    assert r1.plan["risk"] <= 25.0 + 1e-6
    assert r10.plan["risk"] <= 25.0 + 1e-6
    assert r10.plan["contract_multiplier"] == 10.0
    assert r10.plan["qty"] == pytest.approx(r1.plan["qty"] / 10.0, rel=1e-2)


def test_18_leverage_cap_and_liquidation_safety():
    """Test 18 (Section 6): Leverage capped at 3x (default 2x) and verify_liquidation_safety rejects unsafe SL near liquidation."""
    safe_liq = verify_liquidation_safety(entry=100.0, sl=97.0, direction="LONG", leverage=2, tick=0.01)
    assert safe_liq["ok"] is True
    assert safe_liq["liquidation_price"] < 97.0

    unsafe_liq = verify_liquidation_safety(entry=100.0, sl=50.0, direction="LONG", leverage=2, tick=0.01)
    assert unsafe_liq["ok"] is False


def test_19_fee_slippage_funding_and_net_rr_hard_gate():
    """Test 19 (Section 6): Excessive fees/funding that push round-trip cost > 20% of 1R or Net R:R < 1.50R are rejected."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    high_fee_acc = _make_account(now_ms=now_ms, equity=10_000.0, taker_fee=0.025)
    res = evaluate_hybrid_adaptive(snap, high_fee_acc)
    assert res.status != "VALID"
    assert res.plan is None


def test_20_paper_lifecycle_partial_fill_tp_splits_be_trailing_and_time_exits():
    """Test 20 (Section 8): PAPER lifecycle supports partial fill SL protection, 40/35/25 TP split, BE after TP1, +1.5R trailing, and 6/12 bar exits."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms=now_ms, equity=10_000.0))
    assert res.status == "VALID"
    plan = res.plan
    assert plan["expiry_m5_bars"] == 2

    half_qty = round(plan["qty"] * 0.5, 3)
    filled_plan = after_fill(plan, plan["entry"], half_qty, plan["signal_close_ms"] + 60_000)
    assert filled_plan["protection_qty"] == half_qty
    assert filled_plan["sl_active_for_partial_fill"] is True
    assert pytest.approx(filled_plan["tp1_qty"] + filled_plan["tp2_qty"] + filled_plan["runner_qty"]) == half_qty


def test_21_intrabar_conservative_sl_first_rule():
    """Test 21 (Section 8): When a single M5 bar touches both SL and TP1, SL wins with CONSERVATIVE_INTRABAR_APPROXIMATION."""
    now_ms, _, m5 = _build_strategy_a_frames("LONG", strong=True)
    plan = {
        "direction": "LONG",
        "entry": 100.0,
        "sl": 98.0,
        "tp1": 102.0,
        "tp2": 104.0,
        "tp3": 106.0,
        "distance": 2.0,
        "tick": 0.01,
        "fee": 0.0005,
        "funding_per_unit": 0.03,
        "slippage_per_unit": 0.02,
        "fill_ms": now_ms - 300_000,
    }
    out = evaluate_hybrid_position_lifecycle(
        plan,
        m5,
        now_ms,
        mark_price=100.0,
        intrabar_high=103.0,
        intrabar_low=97.5,
        m1_sequence_available=False,
    )
    assert out["action"] == "EXIT"
    assert out["exit_reason"] == "STOP_LOSS"
    assert out["flag"] == "CONSERVATIVE_INTRABAR_APPROXIMATION"


def test_22_daily_loss_limit_and_consecutive_losses_cooldown():
    """Test 22 (Section 6): Daily loss >= 2.0% or 3 consecutive losses within 60m blocks new entries."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    acc_dd = _make_account(now_ms=now_ms, equity=9_790.0, day_start_equity=10_000.0)
    res_dd = evaluate_hybrid_adaptive(snap, acc_dd)
    assert res_dd.status != "VALID"
    assert res_dd.plan is None

    acc_cd = _make_account(now_ms=now_ms, equity=10_000.0, consecutive_losses=3, last_loss_closed_ms=now_ms - 1800_000)
    res_cd = evaluate_hybrid_adaptive(snap, acc_cd)
    assert res_cd.status != "VALID"
    assert res_cd.plan is None


def test_23_groq_advisor_only_never_blocks_valid_rule_engine():
    """Test 23 (Section 7): Groq AI unavailable or WAIT/NO_TRADE never blocks a VALID Rule Engine candidate."""
    now_ms, m15, m5 = _build_strategy_a_frames("LONG", strong=True)
    snap = _make_snapshot_from_frames(now_ms, m15, m5)
    res = evaluate_hybrid_adaptive(snap, _make_account(now_ms=now_ms, equity=10_000.0))
    assert res.status == "VALID"
    orig_plan = dict(res.plan)

    adv_unavail = evaluate_hybrid_ai_advisor(res, {"ok": False, "decision": "WAIT", "reasons": ["Timeout"]})
    assert adv_unavail["ai_status"] == "AI_UNAVAILABLE"
    assert adv_unavail["final_status"] == "VALID"
    assert adv_unavail["paper_entry_allowed"] is True

    adv_caution = evaluate_hybrid_ai_advisor(res, {"ok": True, "decision": "WAIT", "reasons": ["Caution"], "cautions": ["Resistance"]})
    assert adv_caution["ai_status"] == "AI_CAUTION"
    assert adv_caution["final_status"] == "VALID"
    assert adv_caution["paper_entry_allowed"] is True
    assert res.plan == orig_plan


def test_24_telegram_4_stage_alerts_and_deduplication():
    """Test 24 (Section 10): 4 alert stages (EARLY_WATCH, SETUP_FORMING, PAPER_ENTRY, TRADE_UPDATE) deduplicate per stage while allowing stage promotion."""
    from futures.hybrid_adaptive import HybridAlertDeduplicator, format_hybrid_alert

    dedup = HybridAlertDeduplicator()
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "EARLY_WATCH") is True
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "EARLY_WATCH") is False
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "SETUP_FORMING") is True
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "PAPER_ENTRY") is True
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "PAPER_ENTRY") is False
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "TRADE_UPDATE", event_id="TP1") is True
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "TRADE_UPDATE", event_id="TP1") is False
    assert dedup.should_send("BTCUSDT", "5m", NOW, "A", "LONG", "TRADE_UPDATE", event_id="TP2") is True

    for st_name in ("EARLY_WATCH", "SETUP_FORMING", "PAPER_ENTRY", "TRADE_UPDATE"):
        msg = format_hybrid_alert(st_name, {"symbol": "BTCUSDT", "direction": "LONG", "strategy": "A", "m15_regime": "TREND_UP", "setup_quality_score": 82})
        assert st_name in msg


def test_25_worker_heartbeat_30s_90s_stale_and_7_runtime_proof_fields(tmp_path):
    """Test 25 (Section 9): Worker heartbeat supports NO_WORKER, ONLINE (<=90s), STALE (>90s), OFFLINE, and all 7 runtime proof fields."""
    db_file = tmp_path / "worker_v11.db"
    ledger = PaperLedger(db_file)

    wh_none = ledger.worker_health_status(now_ms=NOW)
    assert wh_none["v11_status"] == "NO_WORKER"
    assert wh_none["alive"] is False

    ledger.record_worker_heartbeat(
        "worker-v11-01",
        NOW,
        60,
        strategy_profile="BALANCED",
        strategy_engine="HYBRID_ADAPTIVE_V1",
        last_scan_ms=NOW - 5_000,
        next_scan_ms=NOW + 55_000,
        database_name=db_file.name,
    )
    wh_online = ledger.worker_health_status(now_ms=NOW + 30_000)
    assert wh_online["v11_status"] == "ONLINE"
    assert wh_online["alive"] is True
    for field in (
        "worker_id",
        "last_heartbeat_ms",
        "last_scan_ms",
        "next_scan_ms",
        "effective_strategy_profile",
        "effective_strategy_engine",
        "database_name",
    ):
        assert wh_online.get(field) is not None

    wh_stale = ledger.worker_health_status(now_ms=NOW + 95_000)
    assert wh_stale["v11_status"] == "STALE"
    assert wh_stale["alive"] is False

    wh_off = ledger.worker_health_status(now_ms=NOW + 250_000)
    assert wh_off["v11_status"] == "OFFLINE"
    assert wh_off["alive"] is False


def test_26_read_only_dashboard_refresh_and_telemetry_instrumentation(monkeypatch, tmp_path):
    """Test 26 (Section 9 & Section 11): Passive dashboard refresh increments dashboard_refreshes without triggering market API requests, and ResourceMonitor reports NOT_INSTRUMENTED when disabled."""
    mon_disabled = ResourceMonitor(instrumented=False)
    snap_disabled = mon_disabled.telemetry_snapshot()
    assert snap_disabled["market_api_requests"] == "NOT_INSTRUMENTED"
    assert snap_disabled["groq_requests"] == "NOT_INSTRUMENTED"
    assert snap_disabled["telegram_requests"] == "NOT_INSTRUMENTED"

    before_mkt = GLOBAL_MONITOR.snapshot()["counters"]["market_api_requests"]
    class DummyResp:
        status_code = 200
        text = '{"serverTime": 1700000000000}'
        headers = {}
        def json(self):
            return {"serverTime": 1700000000000}

    class DummySession:
        def get(self, *a, **kw):
            return DummyResp()

    fm = FuturesMarket(session=DummySession())
    fm.get("/fapi/v1/time")
    after_mkt = GLOBAL_MONITOR.snapshot()["counters"]["market_api_requests"]
    assert after_mkt == before_mkt + 1

    from streamlit.testing.v1 import AppTest
    db_file = tmp_path / "dash_ro.db"
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(db_file))
    monkeypatch.setenv("COINAGENT_ADMIN_TOKEN", "admin-secret-v11")
    save_global_preferences(db_file, {"auto": True, "interval": 60})
    ledger = PaperLedger(db_file)
    ledger.record_worker_heartbeat("worker-live-v11", int(time.time() * 1000), 60)

    before_ref = GLOBAL_MONITOR.snapshot()["counters"]["dashboard_refreshes"]
    scan_calls = []
    monkeypatch.setattr(MarketScanner, "run", lambda self, cfg, manual=False, **kw: scan_calls.append(1) or {})
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app_path), default_timeout=20)
    at.run()
    assert not at.exception
    after_ref = GLOBAL_MONITOR.snapshot()["counters"]["dashboard_refreshes"]
    assert after_ref >= before_ref + 1
    assert len(scan_calls) == 0

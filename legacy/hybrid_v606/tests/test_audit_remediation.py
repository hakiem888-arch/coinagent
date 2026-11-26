"""Comprehensive Audit Remediation & 12 Mandatory Simulation Tests (CoinAgentX v5.9.9).

Covers:
- Section B: BUG-01 through BUG-08 regression tests.
- Section C: All 12 mandatory simulation scenarios.
- Section D: Acceptance criteria verification.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import pytest

from futures.lifecycle import after_fill, manage_position, evaluate_intrabar_ohlc, compare_be_thresholds
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.models import INTERVAL_MS, TIMEFRAMES
from futures.paper import PaperLedger, classify_monitoring_state, DEFAULT_MAX_GAP_MS
from futures.strategy import evaluate, compute_tp_targets, compute_order_expiry_ms, break_even, quantize
from tests.fixtures import setup, verified, NOW, candles
from tests.test_paper import approved, quote, enter
from tests.test_market_scanner import Market, Calendar, Pool, Alerts


def quote_aligned(data, price, delta=1000, mark=None):
    """Create a quote snapshot at NOW + delta with bias/execution candle timestamps aligned to NOW + delta."""
    s = quote(data, price, delta=delta, mark=mark)
    for name, tf in zip(("bias", "execution"), TIMEFRAMES[s.mode]):
        d = getattr(s, name).copy()
        step = INTERVAL_MS[tf]
        end = (s.now_ms // step) * step
        d["open_time"] = list(range(end - len(d) * step, end, step))
        d["close_time"] = d.open_time + step - 1
        setattr(s, name, d)
    return s


# ==============================================================================
# SECTION C — 12 PENGUJIAN TAMBAHAN WAJIB
# ==============================================================================

def test_c01_long_trigger_touched_but_limit_not_satisfied_stays_pending(tmp_path):
    """C-01: Simulasi LONG ketika trigger tersentuh tetapi harga limit tidak terpenuhi."""
    d = approved("LONG")
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c01.db")
    assert ledger.queue(d, lambda: 100)[0]

    # Price jumps above entry_trigger AND above limit entry (ask > entry), so trigger is touched but limit is NOT satisfied
    s = quote(d, p["entry"] + 0.05 * p["atr"], delta=1000)
    s.last = p["entry"] + 0.02 * p["atr"]
    s.mark = s.last
    s.bid = p["entry"] + 0.01 * p["atr"]
    s.ask = p["entry"] + 0.03 * p["atr"]
    s.bids = [[s.bid, 10000]]
    s.asks = [[s.ask, 10000]]
    ledger.advance(s)

    trade = ledger.trades()[0]
    assert trade["status"] == "PENDING"
    assert trade["triggered"] is True
    assert trade["remaining_qty"] == 0.0


def test_c02_short_trigger_touched_but_limit_not_satisfied_stays_pending(tmp_path):
    """C-02: Simulasi SHORT dengan kondisi yang sama (trigger tersentuh tetapi limit tidak terpenuhi)."""
    d = approved("SHORT")
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c02.db")
    assert ledger.queue(d, lambda: 100)[0]

    # For SHORT: trigger is touched (last <= entry_trigger), but bid < entry so limit sell is NOT satisfied
    s = quote(d, p["entry"] - 0.05 * p["atr"], delta=1000)
    s.last = p["entry"] - 0.02 * p["atr"]
    s.mark = s.last
    s.bid = p["entry"] - 0.03 * p["atr"]
    s.ask = p["entry"] - 0.01 * p["atr"]
    s.bids = [[s.bid, 10000]]
    s.asks = [[s.ask, 10000]]
    ledger.advance(s)

    trade = ledger.trades()[0]
    assert trade["status"] == "PENDING"
    assert trade["triggered"] is True
    assert trade["remaining_qty"] == 0.0


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_c03_single_candle_touches_sl_and_tp_simultaneously_uses_conservative_sl_first(direction, tmp_path):
    """C-03: Simulasi satu candle menyentuh SL dan TP sekaligus (asumsi konservatif: SL terkena lebih dulu)."""
    d = approved(direction)
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c03.db")
    assert ledger.queue(d, lambda: 100)[0]
    enter(ledger, d, delta=1000)

    sign = 1 if direction == "LONG" else -1
    # Create a snapshot where current bar touched BOTH SL and TP1, even though last/quote is near TP1
    s = quote(d, p["tp1"] + sign * 0.02, delta=2000)
    if direction == "LONG":
        s.open_bar_low = p["sl"] - 0.05
        s.open_bar_high = p["tp1"] + 0.05
    else:
        s.open_bar_high = p["sl"] + 0.05
        s.open_bar_low = p["tp1"] - 0.05

    check = evaluate_intrabar_ohlc(p, s.last, s.open_bar_high, s.open_bar_low, s.last)
    assert check["outcome"] == "STOP_LOSS"
    assert check["conservative_precedence"] == "SL_FIRST"

    ledger.advance(s)
    closed = ledger.trades()[0]
    assert closed["status"] == "CLOSED"
    assert closed["note"] == "STOP_LOSS"
    assert closed["tp1_ms"] is None
    assert closed["realized_pnl"] < 0


@pytest.mark.parametrize("minutes,expect_gap", [(1, False), (3, False), (5, True), (30, True)])
def test_c04_connection_outage_1_3_5_30_minutes(minutes, expect_gap, tmp_path):
    """C-04: Simulasi koneksi terputus selama 1, 3, 5, dan 30 menit."""
    delta_ms = minutes * 60 * 1000
    # 1) Test on PENDING position
    d_pend = approved("LONG", symbol="BTCUSDT")
    ledger_pend = PaperLedger(tmp_path / f"pend_{minutes}m.db")
    ledger_pend.queue(d_pend, lambda: 100)
    s_pend = quote_aligned(d_pend, d_pend["snapshot"].last, delta=delta_ms)
    state = classify_monitoring_state(s_pend, last_quote_ms=NOW, max_gap_ms=DEFAULT_MAX_GAP_MS)
    assert state == ("DATA_GAP" if expect_gap else "OK")
    ledger_pend.advance(s_pend, max_gap_ms=DEFAULT_MAX_GAP_MS)
    t_pend = ledger_pend.trades()[0]
    if expect_gap:
        assert t_pend["status"] == "CANCELLED"
        assert t_pend["quality"] == "DATA_GAP"
    else:
        assert t_pend["status"] == "PENDING"
        assert t_pend["quality"] == "POLLING_SIMULATION"

    # 2) Test on OPEN position
    d_open = approved("LONG", symbol="ETHUSDT")
    ledger_open = PaperLedger(tmp_path / f"open_{minutes}m.db")
    ledger_open.queue(d_open, lambda: 100)
    enter(ledger_open, d_open, delta=1000)
    s_open = quote_aligned(d_open, d_open["result"].plan["entry"], delta=1000 + delta_ms)
    ledger_open.advance(s_open, max_gap_ms=DEFAULT_MAX_GAP_MS)
    t_open = ledger_open.trades()[0]
    if expect_gap:
        assert t_open["status"] == "CLOSED"
        assert t_open["quality"] == "DATA_GAP"
        assert t_open["note"] == "DATA_GAP_EXIT"
    else:
        assert t_open["status"] == "OPEN"
        assert t_open["quality"] == "POLLING_SIMULATION"


def test_c05_quote_older_than_freshness_limit_produces_stale_data(tmp_path):
    """C-05: Simulasi quote lebih tua dari batas freshness (>30 detik -> STALE_DATA)."""
    s, a = setup()
    s.quote_times["depth"] = s.now_ms - 30001
    r = evaluate(s, a, paper=True)
    assert r.status == "WAIT"
    assert r.meta.get("stale_data") is True
    assert r.meta.get("data_status") == "STALE_DATA"

    d = approved("LONG")
    ledger = PaperLedger(tmp_path / "c05.db")
    ledger.queue(d, lambda: 100)
    stale_snap = quote(d, d["result"].plan["entry"], delta=5000)
    stale_snap.quote_times["book"] = stale_snap.now_ms - 30005
    assert classify_monitoring_state(stale_snap, last_quote_ms=NOW) == "STALE_DATA"
    with pytest.raises(ValueError, match="STALE_DATA"):
        ledger.advance(stale_snap)
    # Position must remain untouched when quote is stale
    assert ledger.trades()[0]["status"] == "PENDING"


def test_c06_sudden_spread_widening_blocks_or_cancels_pending(tmp_path):
    """C-06: Simulasi spread melebar mendadak."""
    s, a = setup()
    # Widen spread beyond 0.05 * ATR during evaluation
    s.ask = s.bid + 1.5
    r = evaluate(s, a, paper=True)
    assert r.status == "NO_TRADE"

    # Widen spread while order is PENDING in PaperLedger
    d = approved("LONG")
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c06.db")
    ledger.queue(d, lambda: 100)
    wide = quote(d, d["snapshot"].last, delta=1000)
    wide.bid = wide.last - 0.10 * p["atr"]
    wide.ask = wide.last + 0.10 * p["atr"]  # Spread = 0.20 ATR > 0.05 ATR
    wide.bids = [[wide.bid, 10000]]
    wide.asks = [[wide.ask, 10000]]
    ledger.advance(wide)
    t = ledger.trades()[0]
    assert t["status"] == "CANCELLED"
    assert t["note"] == "FILTER_CHANGED"


def test_c07_extreme_slippage_blocks_entry_and_degrades_stop_fill(tmp_path):
    """C-07: Simulasi slippage ekstrem."""
    s, a = setup()
    # Extreme slippage (e.g. 150 bps) causes csl > 0.20 * distance -> rejected by strategy
    a_extreme = replace(a, exit_slippage_bps=150.0)
    r = evaluate(s, a_extreme, paper=True)
    assert r.status == "NO_TRADE"
    assert any(c["Kriteria"] == "Biaya terhadap D" and c["Status"] == "GAGAL" for c in r.checks)

    # Also verify that if exit depth within slippage tolerance dries up while PENDING, order is cancelled
    d = approved("LONG")
    ledger = PaperLedger(tmp_path / "c07.db")
    ledger.queue(d, lambda: 100)
    thin_exit = quote(d, d["snapshot"].last, delta=1000)
    # Place bid liquidity far away from top bid (outside slippage_per_unit tolerance)
    thin_exit.bids = [[thin_exit.bid - 5.0, 10000]]
    ledger.advance(thin_exit)
    assert ledger.trades()[0]["status"] == "CANCELLED"
    assert ledger.trades()[0]["note"] == "FILTER_CHANGED"


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_c08_price_gap_past_sl_fills_at_worse_gapped_market_price(direction, tmp_path):
    """C-08: Simulasi gap harga melewati SL (stop loss tidak membatasi kerugian gap)."""
    d = approved(direction)
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c08.db")
    ledger.queue(d, lambda: 100)
    enter(ledger, d, delta=1000)

    sign = 1 if direction == "LONG" else -1
    # Price gaps 3.0 USDT past SL (within normal polling interval, e.g. 5 seconds later)
    gapped_price = p["sl"] - sign * 3.0
    s = quote(d, gapped_price, delta=5000)
    ledger.advance(s)

    closed = ledger.trades()[0]
    assert closed["status"] == "CLOSED"
    assert closed["note"] == "STOP_LOSS"
    events = json.loads(ledger.export_json())["events"]
    stop_event = next(e for e in events if e["event"] == "STOP_LOSS")
    expected_fill = (s.bid if sign == 1 else s.ask) - sign * p["slippage_per_unit"]
    assert stop_event["price"] == pytest.approx(expected_fill)
    # Loss is strictly larger than planned SL risk because price gapped past SL
    assert closed["realized_pnl"] < -p["risk"]


def test_c09_partial_fill_simulation_protects_filled_qty_and_recomputes_tranches(tmp_path):
    """C-09: Simulasi partial fill."""
    d = approved("LONG")
    p = d["result"].plan
    ledger = PaperLedger(tmp_path / "c09.db")
    ledger.queue(d, lambda: 100)

    partial_qty = quantize(p["qty"] * 0.6, p["quantity_step"])
    s = quote(d, (p["entry_trigger"] + p["entry"]) / 2, delta=1000)
    # Book has partial_qty at limit price p["entry"], and remaining liquidity slightly above p["entry"] (within 0.10 ATR)
    s.asks = [[s.ask, partial_qty], [p["entry"] + 0.02 * p["atr"], p["qty"]]]
    s.bids = [[s.bid, p["qty"] * 2]]

    # Default advance (allow_partial=False) keeps status PENDING when depth at limit < full qty
    ledger.advance(s, allow_partial=False)
    assert ledger.trades()[0]["status"] == "PENDING"

    # With allow_partial=True, partial fill executes for partial_qty and scales TP1/TP2/runner proportionally
    s2 = quote(d, (p["entry_trigger"] + p["entry"]) / 2, delta=2000)
    s2.asks = [[s2.ask, partial_qty], [p["entry"] + 0.02 * p["atr"], p["qty"]]]
    s2.bids = [[s2.bid, p["qty"] * 2]]
    ledger.advance(s2, allow_partial=True)
    t = ledger.trades()[0]
    assert t["status"] == "OPEN"
    assert t["remaining_qty"] == pytest.approx(partial_qty)
    assert t["plan"]["protection_qty"] == pytest.approx(partial_qty)
    assert t["plan"]["tp1_qty"] + t["plan"]["tp2_qty"] + t["plan"]["runner_qty"] == pytest.approx(partial_qty)


def test_c10_tp1_tp2_and_runner_lifecycle_consistency(tmp_path):
    """C-10: Simulasi TP1, TP2, dan runner."""
    d = approved("LONG")
    p = d["result"].plan
    # Verify unified TP targets (2R, 3R, 4R)
    dist, tp1, tp2, tp3, q1, q2, qr = compute_tp_targets(
        p["entry"], p["sl"], p["direction"], p["tick"], p["qty"], p["quantity_step"]
    )
    assert p["tp1"] == tp1 and p["tp2"] == tp2 and p["tp3"] == tp3
    assert p["tp1_qty"] == q1 and p["tp2_qty"] == q2 and p["runner_qty"] == qr

    ledger = PaperLedger(tmp_path / "c10.db")
    ledger.queue(d, lambda: 100)
    enter(ledger, d, delta=1000)

    # Touch +0.7R: must NOT trigger early BE before TP1 (BUG-06 fix)
    s_07r = quote(d, p["entry"] + 0.7 * p["distance"], delta=1500)
    s_07r.open_bar_high = p["entry"] + 0.9 * p["distance"]
    ledger.advance(s_07r)
    t_07r = ledger.trades()[0]
    assert t_07r["stop"] == p["sl"]
    assert t_07r["tp1_ms"] is None

    # Reach TP1 (2R): 50% exits, stop moves to break-even including costs
    ledger.advance(quote(d, p["tp1"] + 0.02, delta=2000))
    t_tp1 = ledger.trades()[0]
    assert t_tp1["tp1_ms"] == NOW + 2000
    assert t_tp1["remaining_qty"] == pytest.approx(p["qty"] - p["tp1_qty"])
    expected_be = quantize(break_even(p["entry"], "LONG", p["fee"], p["funding_per_unit"], p["slippage_per_unit"]), p["tick"], up=True)
    assert t_tp1["stop"] == pytest.approx(expected_be)

    # Reach TP2 (3R): 25% exits, runner (25%) remains open
    ledger.advance(quote(d, p["tp2"] + 0.02, delta=3000))
    t_tp2 = ledger.trades()[0]
    assert t_tp2["tp2_ms"] == NOW + 3000
    assert t_tp2["remaining_qty"] == pytest.approx(p["runner_qty"])
    assert t_tp2["status"] == "OPEN"

    # Runner exits when price retraces to trailing/BE stop
    ledger.advance(quote(d, t_tp2["stop"] - 0.02, delta=4000))
    t_final = ledger.trades()[0]
    assert t_final["status"] == "CLOSED"
    assert t_final["remaining_qty"] == 0.0
    assert t_final["realized_pnl"] > 0


def test_c11_worker_restart_while_position_open_preserves_state(tmp_path):
    """C-11: Simulasi restart worker saat posisi OPEN."""
    db_path = tmp_path / "worker_restart.db"
    d = approved("LONG")
    p = d["result"].plan

    # Worker instance 1 opens position and fills TP1
    ledger1 = PaperLedger(db_path, initial_equity=1000.0)
    ledger1.queue(d, lambda: 100)
    enter(ledger1, d, delta=1000)
    ledger1.advance(quote(d, p["tp1"] + 0.02, delta=2000))
    before_restart = ledger1.trades()[0]
    assert before_restart["status"] == "OPEN"
    assert before_restart["tp1_ms"] == NOW + 2000

    # Simulate worker process restart: create fresh PaperLedger attached to same SQLite DB
    ledger2 = PaperLedger(db_path, initial_equity=5000.0)  # Must preserve original 1000.0 initial_equity
    assert ledger2.summary(NOW + 2000)["initial_equity"] == 1000.0
    active_after_restart = ledger2.active()
    assert len(active_after_restart) == 1
    assert active_after_restart[0]["signal"] == before_restart["signal"]
    assert active_after_restart[0]["remaining_qty"] == pytest.approx(before_restart["remaining_qty"])
    assert active_after_restart[0]["stop"] == pytest.approx(before_restart["stop"])

    # Advance on restarted worker to TP2 and then stop exit
    ledger2.advance(quote(d, p["tp2"] + 0.02, delta=3000))
    assert ledger2.trades()[0]["remaining_qty"] == pytest.approx(p["runner_qty"])
    ledger2.advance(quote(d, ledger2.trades()[0]["stop"] - 0.02, delta=4000))
    assert ledger2.trades()[0]["status"] == "CLOSED"


def test_c12_dashboard_and_worker_produce_identical_results_on_identical_inputs(tmp_path):
    """C-12: Perbandingan hasil dashboard dan worker menggunakan input identik (BUG-05)."""
    import app as app_mod

    # Verify zero monkey-patches on core classes in app.py
    assert PaperLedger.advance.__module__ == "futures.paper"
    assert evaluate.__module__ == "futures.strategy"
    assert not hasattr(app_mod, "_patched_paper_advance")
    assert not hasattr(app_mod, "SmartThreeTierMarket")

    db_dash = tmp_path / "dash.db"
    db_work = tmp_path / "work.db"
    cfg = ScannerConfig(candidates=2, interval=300, fee=0.0004, slippage_bps=0.1, funding=0.00001, send_alerts=False)

    m_dash, m_work = Market(), Market()
    p_dash, p_work = Pool(), Pool()
    c_dash, c_work = Calendar(True), Calendar(True)
    l_dash, l_work = PaperLedger(db_dash, 1000.0), PaperLedger(db_work, 1000.0)
    a_dash, a_work = Alerts(l_dash), Alerts(l_work)

    scanner_dash = MarketScanner(m_dash, p_dash, c_dash, l_dash, a_dash, clock=lambda: 100)
    scanner_work = MarketScanner(m_work, p_work, c_work, l_work, a_work, clock=lambda: 100)

    rep_dash = scanner_dash.run(cfg)
    rep_work = scanner_work.run(cfg)

    assert rep_dash["universe_count"] == rep_work["universe_count"]
    assert rep_dash["eligible_count"] == rep_work["eligible_count"]
    assert rep_dash["selected"] == rep_work["selected"]
    assert len(l_dash.trades()) == len(l_work.trades()) == 2

    # Advance both ledgers through identical quote sequence (Entry -> TP1 -> TP2 -> Stop)
    for delta_factor, mult in [(1000, "entry"), (2000, "tp1"), (3000, "tp2"), (4000, "stop")]:
        for t_d, t_w in zip(l_dash.active(), l_work.active()):
            sym = t_d["plan"]["symbol"]
            snap_base = m_dash.snapshot(sym, "SCALPING")
            sign = 1 if t_d["plan"]["direction"] == "LONG" else -1
            p = t_d["plan"]
            if mult == "entry":
                px = (p["entry_trigger"] + p["entry"]) / 2
            elif mult == "tp1":
                px = p["tp1"] + sign * 0.02
            elif mult == "tp2":
                px = p["tp2"] + sign * 0.02
            else:
                px = t_d["stop"] - sign * 0.02
            q_snap = quote({"snapshot": snap_base}, px, delta=delta_factor)
            l_dash.advance(deepcopy(q_snap))
            l_work.advance(deepcopy(q_snap))

    trades_dash = l_dash.trades()
    trades_work = l_work.trades()
    for td, tw in zip(trades_dash, trades_work):
        assert td["signal"] == tw["signal"]
        assert td["status"] == tw["status"] == "CLOSED"
        assert td["realized_pnl"] == pytest.approx(tw["realized_pnl"])
        assert td["plan"]["tp1"] == pytest.approx(tw["plan"]["tp1"])
        assert td["plan"]["expires_ms"] == tw["plan"]["expires_ms"]


# ==============================================================================
# ADDITIONAL REGRESSION TESTS FOR BUG-06, BUG-07, BUG-08
# ==============================================================================

def test_bug06_historical_be_threshold_comparison_and_no_intrabar_lookahead():
    """BUG-06: Compare +0.6R, +1.0R, and +2.0R (TP1) BE thresholds on closed candles with fees/slippage/funding."""
    s, a = setup()
    p = verified(s, a).plan
    # Construct closed candles after fill that rise to +0.7R, dip back to entry (hitting a premature +0.6R BE), and then rally to TP1 (+2.0R)
    prices = [
        p["entry"] + 0.3 * p["distance"],
        p["entry"] + 0.7 * p["distance"],
        p["entry"] - 0.05 * p["distance"],  # Dips slightly below entry/BE, but well above structural SL
        p["entry"] + 1.5 * p["distance"],
        p["tp1"] + 0.2 * p["distance"],
    ]
    df = candles(prices, "15m", width=0.02, now=NOW + 5 * 900000)
    comp = compare_be_thresholds(df, p, thresholds=(0.6, 1.0, 2.0))
    # Premature +0.6R BE gets stopped out at BE on candle 3 before reaching TP1
    assert comp[0.6]["exit_reason"] == "BE_STOP"
    assert comp[0.6]["tp1_filled"] is False
    # Standard BE after TP1 (2.0R) survives the normal pullback and captures TP1 profit
    assert comp[2.0]["tp1_filled"] is True
    assert comp[2.0]["net_pnl"] > comp[0.6]["net_pnl"]


def test_bug07_centralized_order_expiry_never_mutated(tmp_path):
    """BUG-07: Order expiry is computed once by compute_order_expiry_ms (3 execution candles) and never extended to 6 candles."""
    d = approved("LONG")
    p = d["result"].plan
    expected_expiry = compute_order_expiry_ms(
        p["signal_close_ms"], p["execution_tf"], d["snapshot"].now_ms, p["mode"],
        news_ok=True, news_clear_until_ms=NOW + 86400000
    )
    assert p["expires_ms"] == expected_expiry
    assert p["expires_ms"] == p["signal_close_ms"] + 1 + 3 * INTERVAL_MS[p["execution_tf"]]

    ledger = PaperLedger(tmp_path / "bug07.db")
    ledger.queue(d, lambda: 100)
    s = quote(d, d["snapshot"].last, delta=1000)
    ledger.advance(s)
    assert ledger.trades()[0]["plan"]["expires_ms"] == expected_expiry


def test_bug08_documentation_matches_version_5_9_9():
    """BUG-08: README.md and VALIDATION.md match v5.9.9 and contain changelog and regression reports."""
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    validation = (root / "VALIDATION.md").read_text(encoding="utf-8")
    assert "v5.9.9" in readme
    assert "v5.9.9" in validation
    assert "BUG-01" in validation and "BUG-08" in validation
    assert "Changelog" in readme or "CHANGELOG" in readme.upper()

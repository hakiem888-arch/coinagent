from dataclasses import replace
from decimal import Decimal
import math
import pytest
from futures.strategy import evaluate, quantize, common_step, break_even, net_pnl, pnl_scenarios
from tests.fixtures import setup, verified, NOW
from futures.models import TIMEFRAMES, INTERVAL_MS


def test_full_long_pipeline_and_exact_grid_targets():
    s, a = setup()
    result = verified(s, a)
    assert result.status == "VALID", result.reasons
    p = result.plan
    assert p["direction"] == "LONG"
    assert p["tp1"] == pytest.approx(p["entry"] + 2 * p["distance"])
    assert p["tp2"] == pytest.approx(p["entry"] + 3 * p["distance"])
    assert p["risk"] <= a.equity * .005
    assert p["margin"] <= a.equity * .2
    assert p["tp1_qty"] + p["tp2_qty"] + p["runner_qty"] == pytest.approx(p["qty"])
    assert p["entry"] - p["entry_trigger"] <= .10 * p["atr"]
    for field in ("entry", "sl", "tp1", "tp2", "entry_trigger"):
        assert Decimal(str(p[field])) % Decimal(str(s.contract.tick)) == 0


def short_setup():
    s, a = setup()
    for name in ("bias", "execution"):
        d = getattr(s, name).copy()
        old = d.copy()
        d["open"], d["close"] = 228 - old.open, 228 - old.close
        d["high"], d["low"] = 228 - old.low, 228 - old.high
        setattr(s, name, d)
    s.last, s.mark = 228 - s.last, 228 - s.mark
    s.bid, s.ask = 228 - s.ask, 228 - s.bid
    s.open_bar_high, s.open_bar_low = 228 - s.open_bar_low, 228 - s.open_bar_high
    s.open_mark_high, s.open_mark_low = 228 - s.open_mark_low, 228 - s.open_mark_high
    s.bids, s.asks = [[s.bid, 1000]], [[s.ask, 1000]]
    s.previous_day_high, s.previous_day_low = 128, 118
    return s, a


def test_full_short_pipeline_symmetric():
    s, a = short_setup()
    r = verified(s, a)
    assert r.status == "VALID", r.reasons
    p = r.plan
    assert p["direction"] == "SHORT"
    assert p["sl"] > p["entry"] > p["tp1"] > p["tp2"]
    assert p["entry_trigger"] - p["entry"] <= .10 * p["atr"]
    assert p["tp1"] == pytest.approx(p["entry"] - 2 * p["distance"])


@pytest.mark.parametrize("field,value", [("account_verified", False), ("costs_verified", False), ("taker_fee", None),
    ("news_source", ""), ("news_clear_until_ms", NOW+899999), ("news_verified_at_ms", NOW-3600001),
    ("news_verified_at_ms", NOW+1000), ("account_day_utc", "2026-10-23"), ("order_semantics_verified", False)])
def test_missing_inputs_never_ready(field, value):
    s, a = setup()
    setattr(a, field, value)
    r = evaluate(s, a)
    assert r.status == "WAIT"
    assert r.plan is None


@pytest.mark.parametrize("field,value", [("risk_pct", .6), ("leverage", 4), ("leverage", 0), ("isolated", False),
    ("auto_add_margin", True), ("active_pending_count", 2), ("consecutive_losses", 3), ("equity", 980.),
    ("total_existing_risk", 10.), ("free_margin", 0.)])
def test_hard_risk_veto(field, value):
    s, a = setup()
    setattr(a, field, value)
    r = evaluate(s, a)
    assert r.status == "NO_TRADE", r.reasons
    assert r.plan is None


def test_daily_equity_adjusted_for_cash_flow():
    s, a = setup()
    a.equity = 900
    a.net_cash_flow = -100
    r = evaluate(s, a)
    assert not any(x["Kriteria"] == "Batas rugi harian UTC" and x["Status"] != "LULUS" for x in r.checks)


@pytest.mark.parametrize("mutation", ["short_history", "open_candle", "missing_last", "gap", "nan", "stale_quote", "future_quote"])
def test_bad_or_stale_data_blocks_entry(mutation):
    s, a = setup()
    if mutation == "short_history": s.execution = s.execution.tail(499)
    elif mutation == "open_candle": s.execution.loc[s.execution.index[-1], "close_time"] = NOW+1000
    elif mutation == "missing_last": s.bias = s.bias.iloc[:-1]
    elif mutation == "gap": s.execution = s.execution.drop(index=400)
    elif mutation == "nan": s.execution.loc[400, "close"] = math.nan
    elif mutation == "stale_quote": s.quote_times["mark"] = NOW-30001
    elif mutation == "future_quote": s.quote_times["last"] = NOW+1
    assert evaluate(s, a).status == "WAIT"


def test_liquidation_bound_to_candidate_and_leverage():
    s, a = setup()
    assert verified(s, a).status == "VALID"
    a.liquidation_plan_id = "wrong-plan"
    assert evaluate(s, a).status == "WAIT"
    verified(s, a)
    a.leverage = 3
    assert evaluate(s, a).status != "VALID"


def test_liquidation_buffer_failure_vetoes():
    s, a = setup()
    r = verified(s, a)
    a.liquidation_price = r.plan["sl"] - .1 * r.plan["atr"]
    assert evaluate(s, a).status == "NO_TRADE"


def test_price_crossed_trigger_then_retraced_still_waits():
    s, a = setup()
    p = evaluate(s, a).candidate
    s.open_bar_high = p["entry_trigger"]
    assert evaluate(s, a).status == "WAIT"


def test_close_at_trigger_is_wait():
    s, a = setup()
    p = evaluate(s, a).candidate
    s.last = s.mark = p["entry_trigger"]
    assert evaluate(s, a).status == "WAIT"


def test_nearby_resistance_veto():
    s, a = setup()
    p = evaluate(s, a).candidate
    s.previous_day_high = p["entry"] + p["distance"]
    assert evaluate(s, a).status == "NO_TRADE"


def test_altcoin_group_risk_includes_pending():
    s, a = setup()
    s.symbol = "SOLUSDT"
    a.total_existing_risk = a.correlated_alt_risk = 5.
    assert evaluate(s, a).status == "NO_TRADE"


def test_unsupported_contract_no_futures_inference():
    s, a = setup()
    s.contract.contract_type = "CURRENT_QUARTER"
    assert evaluate(s, a).status == "NO_TRADE"


def test_excessive_spread_and_mark_deviation():
    for attribute in ("ask", "mark"):
        s, a = setup()
        setattr(s, attribute, getattr(s, attribute)+2)
        assert evaluate(s, a).status == "NO_TRADE"


def test_insufficient_depth_no_trade():
    s, a = setup()
    s.asks = [[s.ask, .0001]]
    assert evaluate(s, a).status == "NO_TRADE"


def test_stop_reached_before_fill_then_retraced_cancels():
    for field in ("open_bar_low", "open_mark_low"):
        s, a = setup()
        p = evaluate(s, a).candidate
        setattr(s, field, p["sl"])
        assert evaluate(s, a).status == "NO_TRADE"


def test_nonfinite_order_book_rejected():
    s, a = setup()
    s.asks.append([math.nan, 1])
    assert evaluate(s, a).status == "NO_TRADE"


def test_partial_lot_minimum_blocks_small_accounts():
    s, a = setup()
    s.contract.min_notional = 200.
    assert evaluate(s, a).status == "NO_TRADE"


def test_snapshot_thirty_seconds_boundary():
    s, a = setup()
    s.quote_times = {k: NOW-30000 for k in s.quote_times}
    assert evaluate(s, a).candidate is not None


def test_order_expiry_three_execution_candles_and_news_limit():
    s, a = setup()
    p = evaluate(s, a).candidate
    assert p["expires_ms"] == p["signal_close_ms"] + 1 + 3 * 900000
    a.news_clear_until_ms = NOW+1200000
    assert evaluate(s, a).candidate["expires_ms"] == NOW+300000


@pytest.mark.parametrize("mode", ["SCALPING", "INTRADAY", "SWING"])
def test_all_timeframe_modes_use_closed_data(mode):
    s, a = setup()
    s.mode = mode
    for name, tf in zip(("bias", "execution"), TIMEFRAMES[mode]):
        d = getattr(s, name)
        interval = INTERVAL_MS[tf]
        end = NOW // interval * interval
        d["open_time"] = list(range(end-len(d)*interval, end, interval))
        d["close_time"] = d.open_time + interval - 1
    r = verified(s, a)
    assert r.status == "VALID", r.reasons
    assert r.meta["bias_tf"] == TIMEFRAMES[mode][0]
    assert (any(x["Kriteria"] == "VWAP harian UTC" for x in r.checks)) == (mode != "SWING")


def test_intraday_order_expires_at_utc_midnight_if_earlier():
    s, a = setup()
    s.now_ms = NOW + 11*3600000 + 45*60000
    for name, tf in zip(("bias", "execution"), TIMEFRAMES[s.mode]):
        d, interval = getattr(s, name), INTERVAL_MS[tf]
        end = s.now_ms//interval*interval
        d["open_time"] = list(range(end-len(d)*interval, end, interval))
        d["close_time"] = d.open_time + interval - 1
    s.quote_times = {k: s.now_ms-1 for k in s.quote_times}
    s.next_funding_ms = s.now_ms+3600000
    a.news_verified_at_ms = s.now_ms-1
    a.news_clear_until_ms = s.now_ms+3600000
    r = evaluate(s, a)
    assert r.candidate is not None, r.reasons
    assert r.candidate["expires_ms"] == (s.now_ms//86400000+1)*86400000


def test_quantity_and_tick_rounding():
    assert quantize(1.239, .01) == 1.23
    assert quantize(1.231, .01, up=True) == 1.24
    assert common_step(.002, .003) == .006
    assert quantize(1.26, .25) == 1.25


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_break_even_recovers_costs_without_leverage_double_count(direction):
    s, a = setup() if direction == "LONG" else short_setup()
    p = verified(s, a).plan
    be = break_even(p["entry"], direction, p["fee"], p["funding_per_unit"], p["slippage_per_unit"])
    assert net_pnl(p, [(p["qty"], be)]) == pytest.approx(0, abs=1e-9)
    cases = pnl_scenarios(p)
    assert cases[0]["PnL bersih USDT"] >= -p["risk"] - 1e-8
    assert cases[0]["% ekuitas"] == pytest.approx(cases[0]["PnL bersih USDT"] / p["equity"] * 100)

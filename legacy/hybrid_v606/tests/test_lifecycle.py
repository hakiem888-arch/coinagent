import numpy as np
import pytest
from futures.lifecycle import after_fill, manage_position
from tests.fixtures import setup, verified, candles, NOW
from tests.test_strategy import short_setup


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_partial_fill_protection_and_targets_use_actual_fill(direction):
    s, a = setup() if direction == "LONG" else short_setup()
    p = verified(s, a).plan
    fill = p["entry"] - .01 if direction == "LONG" else p["entry"] + .01
    filled = after_fill(p, fill, .4, p["signal_close_ms"] + 1000)
    assert filled["sl"] == p["sl"]
    assert filled["protection_qty"] == .4
    assert filled["qty"] <= p["qty"]
    assert filled["risk"] <= p["risk"]
    assert filled["tp1"] != p["tp1"]
    assert filled["remaining_entry_qty"] == pytest.approx(p["qty"] - .4)


def test_bad_fill_and_expired_order_rejected():
    s, a = setup()
    p = verified(s, a).plan
    for fill, qty, stamp in [(p["entry"] + 1, p["qty"], NOW), (p["entry"], p["qty"] + 1, NOW),
                             (p["entry"], p["qty"], p["expires_ms"]), (p["entry"], .0005, NOW)]:
        with pytest.raises(ValueError):
            after_fill(p, fill, qty, stamp)


def test_time_exit_after_twelve_full_candles_without_one_r():
    s, a = setup()
    p = verified(s, a).plan
    fill_at = p["signal_close_ms"] + 1
    filled = after_fill(p, p["entry"], p["qty"], fill_at)
    later = fill_at + 12 * 900000 + 10000
    d = candles(np.full(700, p["entry"]), "15m", .01, now=later)
    out = manage_position(filled, d, later, p["entry"])
    assert out["action"] == "EXIT"
    assert out["full_candles_since_fill"] == 12
    assert manage_position(filled, d, later, p["entry"], reached_one_r=True)["action"] == "HOLD"


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trailing_never_loosens_and_excludes_tp2_partial_candle(direction):
    s, a = setup() if direction == "LONG" else short_setup()
    p = verified(s, a).plan
    fill_at = p["signal_close_ms"] + 1
    filled = after_fill(p, p["entry"], p["qty"], fill_at)
    later = fill_at + 5 * 900000 + 10000
    sign = 1 if direction == "LONG" else -1
    d = candles(np.linspace(p["entry"], p["entry"] + sign * 4, 700), "15m", .1, now=later)
    tp2_at = later-20000  # No fully completed candle after this TP2 fill.
    previous = p["entry"] + sign * 1
    out = manage_position(filled, d, later, p["entry"] + sign * 4, previous,
                          fill_at+900000, tp2_at)
    assert out["stop"] == previous
    out2 = manage_position(filled, d, later, p["entry"] + sign * 4, previous,
                           fill_at+900000, fill_at+2*900000)
    assert sign * (out2["stop"] - previous) >= 0


def test_mark_already_crossed_new_stop_means_exit():
    s, a = setup()
    p = verified(s, a).plan
    filled = after_fill(p, p["entry"], p["qty"], NOW-1000)
    out = manage_position(filled, s.execution, NOW, p["entry"] - .01, tp1_fill_ms=NOW-500)
    assert out["action"] == "EXIT"
    assert out["remaining_qty"] == pytest.approx(p["qty"] - p["tp1_qty"])


def test_tp2_without_confirmed_tp1_rejected():
    s, a = setup()
    p = verified(s, a).plan
    filled = after_fill(p, p["entry"], p["qty"], NOW-1000)
    with pytest.raises(ValueError):
        manage_position(filled, s.execution, NOW, p["entry"], tp2_fill_ms=NOW-100)


def test_partial_tp_updates_protection_without_premature_break_even():
    s, a = setup()
    p = verified(s, a).plan
    filled = after_fill(p, p["entry"], p["qty"], NOW-1000)
    out = manage_position(filled, s.execution, NOW, p["entry"], tp1_filled_qty=.1)
    assert out["remaining_qty"] == pytest.approx(p["qty"] - .1)
    assert out["stop"] == p["sl"]
    assert out["cancel_unfilled"]

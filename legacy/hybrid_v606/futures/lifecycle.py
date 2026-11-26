"""Read-only management calculator using user-confirmed fills, never OHLC touches.

P0-5 & Hybrid Adaptive v1.1: Unified PAPER lifecycle supporting both Strategy F (`hybrid_adaptive_v1`: 1R/2R/3R,
40%/35%/25% split with automatic 60%/40% and 100% lot-size fallback, net BE after TP1, +1.5R trailing at 1.25 ATR
on completed M5 bars, 6-bar < +0.5R + momentum reversal time exit, 12-bar < +1.0R time exit, partial-fill SL
protection, and contract_multiplier accounting) and legacy compatibility.
"""
import copy
from .indicators import indicators
from .strategy import finite, quantize, break_even, pnl_scenarios, frame_problem, compute_tp_targets
from .hybrid_adaptive import compute_hybrid_tp_targets, evaluate_hybrid_position_lifecycle


def after_fill(plan, average_fill, filled_qty, fill_ms):
    if not finite(average_fill) or not finite(filled_qty) or not 0 < filled_qty <= plan["qty"]:
        raise ValueError("Harga/kuantitas fill tidak valid; Q tidak boleh melampaui rencana.")
    if not plan["signal_close_ms"] < fill_ms < plan["expires_ms"]:
        raise ValueError("Fill harus terjadi setelah sinyal dan sebelum kedaluwarsa.")
    sign = 1 if plan["direction"] == "LONG" else -1
    if sign * (average_fill - plan["entry"]) > 1e-9 or sign * (average_fill - plan["sl"]) <= 0:
        raise ValueError("Fill melewati limit atau berada di sisi SL yang tidak valid.")
    step = plan["quantity_step"]
    if abs(quantize(filled_qty, step) - filled_qty) > step * 1e-8:
        raise ValueError("Jumlah fill harus sesuai quantity step.")
    p = copy.deepcopy(plan)
    p.update(entry=average_fill, qty=filled_qty, fill_ms=int(fill_ms))
    p["funding_per_unit"] = plan["funding_per_unit"] * average_fill / plan["entry"]
    p["slippage_per_unit"] = plan["slippage_per_unit"] * average_fill / plan["entry"]
    c_mult = float(p.get("contract_multiplier", 1.0) or 1.0)
    if not finite(c_mult) or c_mult <= 0:
        c_mult = 1.0
    p["contract_multiplier"] = c_mult

    if p.get("hybrid_adaptive_v1"):
        tp_bundle = compute_hybrid_tp_targets(
            average_fill,
            p["sl"],
            p["direction"],
            p["tick"],
            filled_qty,
            step,
            min_qty=float(p.get("min_qty", 0.0)),
            min_notional=float(p.get("min_notional", 0.0)),
        )
        p["distance"] = tp_bundle["distance"]
        p["tp1"] = tp_bundle["tp1"]
        p["tp2"] = tp_bundle["tp2"]
        p["tp3"] = tp_bundle["tp3"]
        p["tp1_qty"] = tp_bundle["tp1_qty"]
        p["tp2_qty"] = tp_bundle["tp2_qty"]
        p["runner_qty"] = tp_bundle["runner_qty"]
        p["tp_split_mode"] = tp_bundle["split_mode"]
        p["partial_tp_ready"] = bool(tp_bundle["partial_tp_ready"])
    else:
        distance, tp1, tp2, tp3, q1, q2, qr = compute_tp_targets(
            average_fill, p["sl"], p["direction"], p["tick"], filled_qty, step
        )
        p["distance"] = distance
        p["tp1"] = tp1
        p["tp2"] = tp2
        p["tp3"] = tp3
        p["tp1_qty"] = q1
        p["tp2_qty"] = q2
        p["runner_qty"] = qr
        p["partial_tp_ready"] = all(
            q >= p["min_qty"] and q * min(p["sl"], p["tp1"], p["tp2"]) * c_mult >= p["min_notional"]
            for q in (p["tp1_qty"], p["tp2_qty"], p["runner_qty"])
        )

    p["notional"] = filled_qty * average_fill * c_mult
    p["margin"] = p["notional"] / p["leverage"]
    p["cost_sl_per_unit"] = average_fill * p["fee"] + (p["sl"] + p["slippage_per_unit"]) * p["fee"] + p["funding_per_unit"] + p["slippage_per_unit"]
    p["risk"] = filled_qty * (p["distance"] + p["cost_sl_per_unit"]) * c_mult
    p["remaining_entry_qty"] = round(plan["qty"] - filled_qty, 12)
    p["protection_qty"] = filled_qty
    p["sl_active_for_partial_fill"] = True
    p["risk_action"] = "REDUCE_OR_EXIT" if p["risk"] > plan["risk"] + 1e-8 else "PROTECT_FILLED_QTY"
    return p


def evaluate_intrabar_ohlc(plan, bar_open, bar_high, bar_low, bar_close, current_stop=None):
    """Conservative intrabar OHLC evaluation: if a single candle touches both SL and TP, SL is always assumed hit first."""
    sign = 1 if plan["direction"] == "LONG" else -1
    stop = plan["sl"] if current_stop is None else current_stop
    adverse = bar_low if sign == 1 else bar_high
    favorable = bar_high if sign == 1 else bar_low
    sl_touched = finite(adverse) and sign * (adverse - stop) <= 0
    tp1_touched = finite(favorable) and sign * (favorable - plan["tp1"]) >= 0
    if sl_touched and tp1_touched:
        return {"outcome": "STOP_LOSS", "sl_touched": True, "tp1_touched": True, "conservative_precedence": "SL_FIRST", "flag": "CONSERVATIVE_INTRABAR_APPROXIMATION"}
    if sl_touched:
        return {"outcome": "STOP_LOSS", "sl_touched": True, "tp1_touched": False, "conservative_precedence": "SL_ONLY", "flag": "NONE"}
    if tp1_touched:
        return {"outcome": "TP1_CANDIDATE", "sl_touched": False, "tp1_touched": True, "conservative_precedence": "TP_ONLY", "flag": "NONE"}
    return {"outcome": "HOLD", "sl_touched": False, "tp1_touched": False, "conservative_precedence": "NONE", "flag": "NONE"}


def compare_be_thresholds(closed_candles, plan, thresholds=(0.6, 1.0, 2.0)):
    """BUG-06: Historical closed-candle comparison of BE thresholds accounting for spread, fee, slippage, and funding without intrabar lookahead."""
    sign = 1 if plan["direction"] == "LONG" else -1
    be_price = quantize(
        break_even(plan["entry"], plan["direction"], plan["fee"], plan["funding_per_unit"], plan["slippage_per_unit"]),
        plan["tick"],
        up=sign == 1,
    )
    results = {}
    for r_mult in thresholds:
        stop = plan["sl"]
        be_armed = False
        tp1_filled = False
        realized = -plan["qty"] * (plan["entry"] * plan["fee"] + plan["funding_per_unit"])
        rem_qty = plan["qty"]
        exit_reason = "HOLD"
        for row in closed_candles.itertuples():
            adverse = float(row.low if sign == 1 else row.high)
            if sign * (adverse - stop) <= 0:
                fill_px = stop - sign * plan["slippage_per_unit"]
                realized += rem_qty * (sign * (fill_px - plan["entry"]) - fill_px * plan["fee"])
                rem_qty = 0.0
                exit_reason = "BE_STOP" if be_armed else "STOP_LOSS"
                break
            close_px = float(row.close)
            if not tp1_filled and sign * (close_px - plan["tp1"]) >= 0:
                fill_px = plan["tp1"] - sign * plan["slippage_per_unit"]
                realized += plan["tp1_qty"] * (sign * (fill_px - plan["entry"]) - fill_px * plan["fee"])
                rem_qty = round(rem_qty - plan["tp1_qty"], 12)
                tp1_filled = True
            if not be_armed and sign * (close_px - plan["entry"]) >= r_mult * plan["distance"]:
                stop = max(stop, be_price) if sign == 1 else min(stop, be_price)
                be_armed = True
        results[float(r_mult)] = {
            "threshold_r": float(r_mult),
            "be_price": be_price,
            "exit_reason": exit_reason,
            "tp1_filled": tp1_filled,
            "remaining_qty": rem_qty,
            "net_pnl": round(realized, 6),
        }
    return results


def manage_position(p, frame, now_ms, mark, previous_stop=None, tp1_fill_ms=None, tp2_fill_ms=None,
                    reached_one_r=False, paid_funding_per_unit=0.0, entry_fee_per_unit=None,
                    tp1_filled_qty=None, tp2_filled_qty=None, reached_half_r=False, reached_15_r=False):
    if not finite(mark) or mark <= 0 or not finite(paid_funding_per_unit) or paid_funding_per_unit < 0:
        raise ValueError("Mark/funding aktual tidak valid")
    if tp2_fill_ms is not None and tp1_fill_ms is None:
        raise ValueError("TP2 membutuhkan catatan fill TP1")
    for stamp in (tp1_fill_ms, tp2_fill_ms):
        if stamp is not None and not p["fill_ms"] <= stamp <= now_ms:
            raise ValueError("Timestamp TP tidak valid")
    if tp1_fill_ms is not None and tp2_fill_ms is not None and tp2_fill_ms < tp1_fill_ms:
        raise ValueError("TP2 tidak boleh sebelum TP1")
    if now_ms < p["fill_ms"]:
        raise ValueError("Fill belum terjadi")
    tp1_qty = (p["tp1_qty"] if tp1_fill_ms is not None else 0.0) if tp1_filled_qty is None else tp1_filled_qty
    tp2_qty = (p["tp2_qty"] if tp2_fill_ms is not None else 0.0) if tp2_filled_qty is None else tp2_filled_qty
    for qty, target, stamp in ((tp1_qty, p["tp1_qty"], tp1_fill_ms), (tp2_qty, p["tp2_qty"], tp2_fill_ms)):
        if not finite(qty) or not 0 <= qty <= target or abs(quantize(qty, p["quantity_step"]) - qty) > p["quantity_step"] * 1e-8:
            raise ValueError("Jumlah partial TP tidak valid")
        if stamp is not None and abs(qty - target) > 1e-9:
            raise ValueError("Waktu TP selesai hanya berlaku setelah seluruh jumlah target terisi")
    if tp2_qty > 0 and tp1_fill_ms is None:
        raise ValueError("Catat TP1 selesai sebelum partial TP2")

    closed = frame[frame.close_time < now_ms].reset_index(drop=True)
    exec_tf = p.get("execution_tf", "5m" if p.get("mode") == "SCALPING" else "15m")
    min_req = min(len(closed), 250) if (p.get("hybrid_adaptive_v1") and len(closed) >= 60) else 500
    problem = frame_problem(closed, exec_tf, now_ms, min_candles=min_req)
    if problem:
        raise ValueError(problem)
    d = indicators(closed)
    if d.empty or not finite(d.atr.iloc[-1]):
        raise ValueError("Candle tertutup tidak mencukupi")

    sign = 1 if p["direction"] == "LONG" else -1
    tighten = max if sign == 1 else min
    stop = p["sl"]
    if previous_stop is not None:
        if not finite(previous_stop) or previous_stop <= 0:
            raise ValueError("Stop sebelumnya tidak valid")
        stop = tighten(stop, previous_stop)
    remaining = p["qty"] - tp1_qty - tp2_qty

    if p.get("hybrid_adaptive_v1"):
        hyb = evaluate_hybrid_position_lifecycle(
            p,
            d,
            now_ms,
            mark,
            current_stop=stop,
            tp1_filled=(tp1_fill_ms is not None),
            tp2_filled=(tp2_fill_ms is not None),
            reached_half_r=reached_half_r,
            reached_one_r=reached_one_r,
            reached_15_r=reached_15_r,
        )
        stop = hyb["stop"]
        hit_one_r = hyb.get("reached_one_r", bool(reached_one_r or tp1_fill_ms is not None))
        full_candles = int(hyb.get("bars_open", len(d[d.open_time >= p["fill_ms"]])))
        action = "HOLD"
        reason = "Pertahankan proteksi sesuai kuantitas terisi (Strategy F Hybrid Adaptive)."
        if hyb["action"] == "EXIT":
            action = "EXIT"
            if hyb["exit_reason"] == "STOP_LOSS":
                reason = "Mark sudah melewati SL yang dihitung; jangan memasang stop di sisi harga yang salah."
            elif hyb["exit_reason"] == "TIME_EXIT_6_BARS":
                reason = "TIME_EXIT_6_BARS: 6 candle M5 sejak fill belum mencapai +0,5R dan momentum berbalik."
            elif hyb["exit_reason"] == "TIME_EXIT_12_BARS":
                reason = "TIME_EXIT_12_BARS: 12 candle lengkap sejak fill tanpa mencapai +1R."
            else:
                reason = hyb["exit_reason"]
        if p.get("risk_action") == "REDUCE_OR_EXIT":
            action, reason = "REDUCE_OR_EXIT", "Risiko setelah fill melebihi rencana awal."
        return {
            "action": action,
            "reason": reason,
            "exit_reason": hyb.get("exit_reason", "NONE"),
            "stop": stop,
            "remaining_qty": max(0, remaining),
            "full_candles_since_fill": full_candles,
            "reached_half_r": bool(hyb.get("reached_half_r", reached_half_r)),
            "reached_one_r": bool(hit_one_r),
            "reached_15_r": bool(hyb.get("reached_15_r", reached_15_r)),
            "cancel_unfilled": tp1_qty > 0 or tp2_qty > 0 or now_ms >= p["expires_ms"] or sign * (mark - p["sl"]) <= 0,
            "scenarios": pnl_scenarios(p),
            "notice": "Kalkulasi manual Strategy F. Tidak memantau atau mengirim order ke bursa.",
        }

    if tp1_fill_ms is not None:
        be = break_even(p["entry"], p["direction"], p["fee"], paid_funding_per_unit,
                        p["slippage_per_unit"], entry_fee_per_unit)
        be = quantize(be, p["tick"], up=sign == 1)
        stop = tighten(stop, be)
    if tp2_fill_ms is not None:
        completed_after_tp2 = d[d.open_time >= tp2_fill_ms]
        if len(completed_after_tp2):
            raw = (completed_after_tp2.high.max() - 2 * d.atr.iloc[-1]) if sign == 1 else (completed_after_tp2.low.min() + 2 * d.atr.iloc[-1])
            stop = tighten(stop, quantize(float(raw), p["tick"], up=sign == 1))
    after = d[d.open_time >= p["fill_ms"]]
    hit_one_r = reached_one_r or tp1_fill_ms is not None or bool(len(after) and (
        after.high.max() >= p["entry"] + p["distance"] if sign == 1 else after.low.min() <= p["entry"] - p["distance"]))
    full_candles = len(after)
    next_midnight = (p["fill_ms"] // 86400000 + 1) * 86400000
    reason, action = "Pertahankan proteksi sesuai kuantitas terisi.", "HOLD"
    if sign * (mark - stop) <= 0:
        action, reason = "EXIT", "Mark sudah melewati SL yang dihitung; jangan memasang stop di sisi harga yang salah."
    elif full_candles >= 12 and not hit_one_r:
        action, reason = "EXIT", "12 candle lengkap sejak fill tanpa mencapai +1R."
    elif p["mode"] != "SWING" and now_ms >= next_midnight:
        action, reason = "EXIT", "Batas akhir hari 00.00 UTC tercapai."
    if p["risk_action"] == "REDUCE_OR_EXIT":
        action, reason = "REDUCE_OR_EXIT", "Risiko setelah fill melebihi rencana awal."
    return {"action": action, "reason": reason, "stop": stop, "remaining_qty": max(0, remaining),
            "full_candles_since_fill": full_candles, "reached_one_r": bool(hit_one_r),
            "cancel_unfilled": tp1_qty > 0 or tp2_qty > 0 or now_ms >= p["expires_ms"] or sign * (mark - p["sl"]) <= 0,
            "scenarios": pnl_scenarios(p),
            "notice": "Kalkulasi manual. Tidak memantau atau mengirim order ke bursa."}

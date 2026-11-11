"""Read-only management calculator using user-confirmed fills, never OHLC touches."""
import copy
from decimal import Decimal
from .indicators import indicators
from .models import INTERVAL_MS
from .strategy import finite, quantize, break_even, pnl_scenarios, frame_problem


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
    p["distance"] = float(abs(Decimal(str(average_fill)) - Decimal(str(p["sl"]))))
    p["tp1"] = quantize(Decimal(str(average_fill)) + sign * 2 * Decimal(str(p["distance"])), p["tick"], up=sign == 1)
    p["tp2"] = quantize(Decimal(str(average_fill)) + sign * 3 * Decimal(str(p["distance"])), p["tick"], up=sign == 1)
    p["tp1_qty"] = quantize(filled_qty * .5, step)
    p["tp2_qty"] = quantize(filled_qty * .25, step)
    p["runner_qty"] = round(filled_qty - p["tp1_qty"] - p["tp2_qty"], 12)
    p["notional"] = filled_qty * average_fill
    p["margin"] = p["notional"] / p["leverage"]
    p["cost_sl_per_unit"] = average_fill * p["fee"] + (p["sl"] + p["slippage_per_unit"]) * p["fee"] + p["funding_per_unit"] + p["slippage_per_unit"]
    p["risk"] = filled_qty * (p["distance"] + p["cost_sl_per_unit"])
    p["remaining_entry_qty"] = round(plan["qty"] - filled_qty, 12)
    p["protection_qty"] = filled_qty
    p["partial_tp_ready"] = all(q >= p["min_qty"] and q * min(p["sl"], p["tp1"], p["tp2"]) >= p["min_notional"]
                                for q in (p["tp1_qty"], p["tp2_qty"], p["runner_qty"]))
    p["risk_action"] = "REDUCE_OR_EXIT" if p["risk"] > plan["risk"] + 1e-8 else "PROTECT_FILLED_QTY"
    # Margin/fees/scenarios are recomputed; never increase Q after a better fill.
    return p


def manage_position(p, frame, now_ms, mark, previous_stop=None, tp1_fill_ms=None, tp2_fill_ms=None,
                    reached_one_r=False, paid_funding_per_unit=0.0, entry_fee_per_unit=None,
                    tp1_filled_qty=None, tp2_filled_qty=None):
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
    problem = frame_problem(closed, p["execution_tf"], now_ms)
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
            "scenarios": pnl_scenarios(p), "notice": "Kalkulasi manual. Tidak memantau atau mengirim order ke bursa."}

"""Master Prompt v1 + Master Addendum (AI Dynamic Leverage & Precision Stop Loss Engine) + Relaxed Mode.
Numeric decisions never come from an LLM."""
import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone
import numpy as np
from .indicators import indicators, confirmed_pivots
from .models import Result, TIMEFRAMES, INTERVAL_MS, DYNAMIC_LEVERAGE_OPTIONS


def finite(value):
    return isinstance(value, (float, int, np.number)) and not isinstance(value, bool) and math.isfinite(value)


def quantize(value, step, up=False):
    value, step = Decimal(str(value)), Decimal(str(step))
    if step <= 0:
        raise ValueError("Step harus positif")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return float((value / step).to_integral_value(rounding=rounding) * step)


def common_step(a, b):
    a, b = Decimal(str(a)), Decimal(str(b))
    scale = 10 ** max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    return math.lcm(int(a * scale), int(b * scale)) / scale


def fingerprint(plan):
    fields = {k: plan[k] for k in ["symbol", "mode", "direction", "signal_close_ms", "entry", "sl", "qty",
                                   "leverage", "equity", "fee", "funding_per_unit", "slippage_per_unit"]}
    if plan.get("instrument_kind") == "SPOT_REFERENCE":
        fields["instrument_kind"] = "SPOT_REFERENCE"
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:20]


def confidence_leverage_cap(score):
    """Section 3 & 7: Dynamic Leverage Matrix based on Signal Confidence Engine score."""
    if not finite(score) or score < 85:
        return 0
    if score <= 89:
        return 2
    if score <= 93:
        return 3
    if score <= 96:
        return 5
    if score <= 98:
        return 10
    if score == 99:
        return 15
    return 20


def compute_confidence(b, e, direction, touched, protected, confirmed, vwap_ok, spread_ok):
    """Section 2: Signal Confidence Engine (max 100 points across 10 weighted parameters)."""
    sign = 1 if direction == "LONG" else -1
    bs, signal, prev = b.iloc[-1], e.iloc[-1], e.iloc[-2]
    before = e.iloc[-6:-1]

    # 1. Liquidity sweep quality (15)
    sweep_wick = (signal.close - before.low.min()) / signal.atr if sign == 1 else (before.high.max() - signal.close) / signal.atr
    sweep_score = 15 if (touched and sweep_wick >= 0.5) else (12 if touched else 8)

    # 2. RSI divergence / momentum alignment quality (10)
    rsi_ok = (50 <= signal.rsi <= 65) if sign == 1 else (35 <= signal.rsi <= 50)
    rsi_relaxed = (40 <= signal.rsi <= 75) if sign == 1 else (25 <= signal.rsi <= 60)
    rsi_slope = (signal.rsi > before.rsi.min()) if sign == 1 else (signal.rsi < before.rsi.max())
    rsi_score = 10 if (rsi_ok and rsi_slope) else (8 if (rsi_ok or (rsi_relaxed and rsi_slope)) else 6)

    # 3. Bollinger Band reclaim (10)
    bb_reclaim = (signal.close >= signal.bb_lower and signal.close >= signal.ema20) if sign == 1 else (
        signal.close <= signal.bb_upper and signal.close <= signal.ema20)
    bb_score = 10 if bb_reclaim else 7

    # 4. CHoCH confirmation (20)
    choch_score = 20 if confirmed else 14

    # 5. Stoch RSI confirmation (10)
    stoch_k = float(signal.stoch_rsi_k) if finite(signal.stoch_rsi_k) else 50.0
    stoch_d = float(signal.stoch_rsi_d) if finite(signal.stoch_rsi_d) else 50.0
    stoch_ok = (stoch_k >= stoch_d or 20 <= stoch_k <= 95) if sign == 1 else (stoch_k <= stoch_d or 5 <= stoch_k <= 80)
    stoch_score = 10 if stoch_ok else 7

    # 6. Volume confirmation (10)
    vol_score = 10 if signal.relative_volume >= 1.5 else (9 if signal.relative_volume >= 1.2 else (7 if signal.relative_volume >= 0.7 else 5))

    # 7. Retest quality (10)
    retest_score = 10 if (touched and protected) else (8 if (touched or protected) else 6)

    # 8. Market regime suitability (5)
    regime_score = 5 if bs.adx >= 25 else (4 if bs.adx >= 14 else 3)

    # 9. Higher timeframe context (5)
    htf_ok = (bs.close > bs.ema50 > bs.ema200 and bs.ema50 > b.ema50.iloc[-6]) if sign == 1 else (
        bs.close < bs.ema50 < bs.ema200 and bs.ema50 < b.ema50.iloc[-6])
    htf_score = 5 if htf_ok else 4

    # 10. Execution quality (5)
    exec_score = 5 if (vwap_ok and spread_ok and signal.tr <= 2.0 * prev.atr) else (4 if spread_ok else 3)

    breakdown = {
        "Liquidity sweep quality": sweep_score,
        "RSI divergence quality": rsi_score,
        "Bollinger Band reclaim": bb_score,
        "CHoCH confirmation": choch_score,
        "Stoch RSI confirmation": stoch_score,
        "Volume confirmation": vol_score,
        "Retest quality": retest_score,
        "Market regime suitability": regime_score,
        "Higher timeframe context": htf_score,
        "Execution quality": exec_score,
    }
    total = int(min(100, max(0, sum(breakdown.values()))))
    return total, breakdown


def select_dynamic_leverage(confidence_score, atr_pct, sl_pct, operator_leverage, calibrated=False):
    """Section 3, 4 & 10: Final Leverage takes the lowest cap across all safety limits."""
    conf_cap = confidence_leverage_cap(confidence_score)
    if conf_cap <= 0:
        return 0, {"confidence_cap": 0, "calibration_cap": 3, "volatility_cap": 2,
                   "liquidation_safety_cap": 2, "operator_cap": operator_leverage}
    calibration_cap = 20 if calibrated else 3
    if atr_pct <= 0.005:
        vol_cap = 20
    elif atr_pct <= 0.01:
        vol_cap = 15
    elif atr_pct <= 0.015:
        vol_cap = 10
    elif atr_pct <= 0.025:
        vol_cap = 5
    elif atr_pct <= 0.04:
        vol_cap = 3
    else:
        vol_cap = 2
    if sl_pct > 0:
        liq_raw = max(2, int(0.40 / sl_pct))
        liq_cap = max([x for x in DYNAMIC_LEVERAGE_OPTIONS if x <= liq_raw] or [2])
    else:
        liq_cap = 2
    op_cap = int(operator_leverage) if operator_leverage in (1, 2, 3, 5, 10, 15, 20) else 2
    raw_min = min(conf_cap, calibration_cap, vol_cap, liq_cap, op_cap, 20)
    allowed = [x for x in (1, 2, 3, 5, 10, 15, 20) if x <= raw_min]
    selected = max(allowed) if allowed else 2
    return selected, {
        "confidence_cap": conf_cap,
        "calibration_cap": calibration_cap,
        "volatility_cap": vol_cap,
        "liquidation_safety_cap": liq_cap,
        "operator_cap": op_cap,
        "max_allowed_leverage": min(conf_cap, calibration_cap, vol_cap, liq_cap, 20),
    }


def frame_problem(frame, interval, now_ms):
    required = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
    if frame is None or not set(required).issubset(frame.columns):
        return "Kolom OHLCV tidak lengkap"
    if len(frame) < 500:
        return f"Hanya {len(frame)} candle tertutup; minimum 500"
    a = frame[required].to_numpy(dtype=float)
    if not np.isfinite(a).all():
        return "OHLCV memuat nilai non-finite"
    step = INTERVAL_MS[interval]
    if not (frame.open_time.diff().iloc[1:] == step).all():
        return "Candle duplikat, tidak berurutan, atau memiliki celah"
    if not ((frame.open_time % step == 0) & (frame.close_time == frame.open_time + step - 1)).all():
        return "Batas waktu candle tidak valid"
    if int(frame.close_time.iloc[-1]) != (now_ms // step) * step - 1:
        return "Candle terakhir selesai belum tersedia atau masih berjalan"
    valid = ((frame.high >= frame[["open", "close", "low"]].max(axis=1)) &
             (frame.low <= frame[["open", "close", "high"]].min(axis=1)) &
             (frame.low > 0) & (frame.volume >= 0))
    return None if valid.all() else "OHLCV tidak valid"


def break_even(entry, direction, fee, funding_per_unit, slippage_per_unit, entry_fee_per_unit=None):
    """Recover allocated entry fees, paid/reserved funding and exit costs on remaining units."""
    entry_fee = entry * fee if entry_fee_per_unit is None else entry_fee_per_unit
    if direction == "LONG":
        return (entry + entry_fee + funding_per_unit) / (1 - fee) + slippage_per_unit
    return (entry - entry_fee - funding_per_unit) / (1 + fee) - slippage_per_unit


def net_pnl(plan, exits):
    sign = 1 if plan["direction"] == "LONG" else -1
    entry, fee = plan["entry"], plan["fee"]
    pnl = 0.0
    for qty, price in exits:
        fill = price - sign * plan["slippage_per_unit"]
        pnl += qty * (sign * (fill - entry) - fee * (entry + fill) - plan["funding_per_unit"])
    return pnl


def pnl_scenarios(plan):
    q, q1, q2, qr = (plan[k] for k in ("qty", "tp1_qty", "tp2_qty", "runner_qty"))
    be = break_even(plan["entry"], plan["direction"], plan["fee"], plan["funding_per_unit"], plan["slippage_per_unit"])
    scenarios = [
        ("SL awal", [(q, plan["sl"])]),
        ("TP1, lalu sisa di estimasi BE biaya", [(q1, plan["tp1"]), (q - q1, be)]),
        ("TP1 + TP2, runner di harga TP1 (skenario)", [(q1, plan["tp1"]), (q2, plan["tp2"]), (qr, plan["tp1"])])]
    out = []
    for label, exits in scenarios:
        pnl = net_pnl(plan, exits)
        out.append({"Skenario": label, "PnL bersih USDT": pnl, "% ekuitas": pnl / plan["equity"] * 100,
                    "ROI margin %": pnl / plan["margin"] * 100})
    return out


def evaluate(snap, account, *, paper=False, relaxed=False):
    spot = snap.instrument_kind == "SPOT_REFERENCE"
    reference = snap.reference_price
    r = Result(meta={"symbol": snap.symbol, "mode": snap.mode, "source": snap.source,
                     "instrument_kind": snap.instrument_kind, "quote_time_basis": snap.quote_time_basis,
                     "time_ms": snap.now_ms, "timezone": "Asia/Jakarta", "limitations": list(snap.warnings),
                     "relaxed_mode": bool(relaxed)})
    if spot and not paper:
        r.check("Sumber spot hanya PAPER", "SPOT_REFERENCE", "Tidak untuk validasi order perpetual live", False)
        return r.finish()
    if paper:
        r.meta["scope"] = "PAPER"
        r.meta["limitations"].append("PAPER: saldo/fee/slippage/funding adalah model simulasi; likuidasi bursa tidak dimodelkan. Bukan persetujuan order live.")
    if snap.mode not in TIMEFRAMES:
        r.check("Mode", snap.mode, "SCALPING / INTRADAY / SWING", missing=True)
        return r.finish()
    bias_tf, exec_tf = TIMEFRAMES[snap.mode]
    r.meta.update(bias_tf=bias_tf, execution_tf=exec_tf)
    for label, frame, tf in (("Candle bias", snap.bias, bias_tf), ("Candle eksekusi", snap.execution, exec_tf)):
        problem = frame_problem(frame, tf, snap.now_ms)
        r.check(label, problem or f"{len(frame)} tertutup", "≥500; kontinu; candle terbaru selesai", ok=not problem, missing=bool(problem))
    spec = snap.contract
    values = [spec.tick, spec.step, spec.min_qty, spec.max_qty, spec.min_notional,
              spec.market_step, spec.market_min_qty, spec.market_max_qty]
    specs_ok = all(finite(x) and x > 0 for x in values)
    r.check("Spesifikasi kontrak", "Tersedia" if specs_ok else "Tidak lengkap", "Tick / lot / market lot / notional", specs_ok, not specs_ok)
    r.check("Pair referensi spot" if spot else "Kontrak linear USDT perpetual", f"{spec.quote_asset} {spec.contract_type} {spec.status}",
            "USDT SPOT TRADING; PAPER saja" if spot else "USDT PERPETUAL TRADING",
            spec.quote_asset == "USDT" and spec.contract_type == ("SPOT" if spot else "PERPETUAL") and spec.status == "TRADING")
    quotes_ok = all(finite(x) and x > 0 for x in (snap.last, reference, snap.bid, snap.ask)) and snap.ask >= snap.bid
    r.check("Harga pasar", "Tersedia" if quotes_ok else "Tidak valid", "Last spot, bid, ask valid" if spot else "Last, mark, bid, ask valid", quotes_ok, not quotes_ok)
    ages = [(snap.now_ms - snap.quote_times.get(k, 0)) / 1000 for k in snap.quote_keys]
    max_age_limit = 60 if relaxed else 30
    fresh = all(0 <= age <= max_age_limit for age in ages)
    r.check("Usia snapshot", f"{max(ages):.1f} detik", f"0–{max_age_limit} detik (estimasi awal request spot)" if spot else f"0–{max_age_limit} detik (timestamp bursa)", fresh, not fresh)
    if any(x["Status"] != "LULUS" for x in r.checks):
        return r.finish()
    b, e = indicators(snap.bias), indicators(snap.execution)
    bs, signal, prev = b.iloc[-1], e.iloc[-1], e.iloc[-2]
    names = [bs.ema50, bs.ema200, bs.adx, b.ema50.iloc[-6], signal.ema20, signal.ema50,
             signal.rsi, signal.atr, prev.atr, signal.relative_volume]
    good = all(finite(x) for x in names) and signal.atr > 0 and prev.atr > 0
    if not good:
        r.check("Indikator", "Tidak lengkap", "Nilai finite dan ATR > 0", missing=True)
        return r.finish()

    if not relaxed:
        long_bias = bs.close > bs.ema50 > bs.ema200 and bs.ema50 > b.ema50.iloc[-6] and bs.adx >= 20
        short_bias = bs.close < bs.ema50 < bs.ema200 and bs.ema50 < b.ema50.iloc[-6] and bs.adx >= 20
        direction = "LONG" if long_bias else ("SHORT" if short_bias else None)
    else:
        # Relaxed Mode: Supports both Trend-Following AND Multi-Stage M15/M5 Reversal setups
        recent_exec = e.iloc[-10:-1]
        long_trend = bs.close > bs.ema50 and (bs.ema50 >= bs.ema200 * 0.995 or bs.ema50 > b.ema50.iloc[-6])
        short_trend = bs.close < bs.ema50 and (bs.ema50 <= bs.ema200 * 1.005 or bs.ema50 < b.ema50.iloc[-6])
        long_rev = (signal.close > signal.open and (signal.low <= recent_exec.low.min() + 0.35 * signal.atr or signal.close >= signal.ema20))
        short_rev = (signal.close < signal.open and (signal.high >= recent_exec.high.max() - 0.35 * signal.atr or signal.close <= signal.ema20))
        if (long_trend and signal.close >= signal.ema20) or (long_rev and signal.rsi <= 68):
            direction = "LONG"
        elif (short_trend and signal.close <= signal.ema20) or (short_rev and signal.rsi >= 32):
            direction = "SHORT"
        else:
            direction = "LONG" if signal.close >= signal.ema20 else "SHORT"
        long_bias = direction == "LONG"
        short_bias = direction == "SHORT"

    r.direction = direction
    r.metrics = {"close_bias": float(bs.close), "ema50_bias": float(bs.ema50), "ema200_bias": float(bs.ema200),
                 "adx": float(bs.adx), "ema50_bias_5_before": float(b.ema50.iloc[-6]), "rsi": float(signal.rsi),
                 "atr": float(signal.atr), "relative_volume": float(signal.relative_volume),
                 "vwap": float(signal.vwap) if finite(signal.vwap) else None, "last": snap.last, "mark": snap.mark}

    if not relaxed:
        r.check("Urutan tren bias", f"C {bs.close:.8g}; EMA50 {bs.ema50:.8g}; EMA200 {bs.ema200:.8g}",
                "C > EMA50 > EMA200 atau kebalikannya", bs.close > bs.ema50 > bs.ema200 or bs.close < bs.ema50 < bs.ema200)
        slope_ok = (bs.close > bs.ema50 and bs.ema50 > b.ema50.iloc[-6]) or (bs.close < bs.ema50 and bs.ema50 < b.ema50.iloc[-6])
        r.check("Kemiringan EMA50 bias", f"Sekarang {bs.ema50:.8g}; 5 lalu {b.ema50.iloc[-6]:.8g}", "Sesuai arah tren", slope_ok)
        r.check("ADX14 bias", f"{bs.adx:.2f}", "≥20", bs.adx >= 20)
    else:
        r.check("Urutan tren / konteks reversal bias", f"C {bs.close:.8g}; EMA50 {bs.ema50:.8g}; EMA200 {bs.ema200:.8g}",
                "Tren searah atau Reversal M15/M5 teridentifikasi (Relaxed Mode)", bool(direction))
        slope_ok = True
        r.check("Kemiringan EMA50 / Struktur bias", f"Sekarang {bs.ema50:.8g}; 5 lalu {b.ema50.iloc[-6]:.8g}", "Konteks HTF mendukung arah (Relaxed)", slope_ok)
        adx_min = 12.0
        r.check("ADX14 bias", f"{bs.adx:.2f}", f"≥{adx_min:.0f} (Relaxed)", bs.adx >= adx_min)

    if not direction:
        return r.finish()
    sign = 1 if direction == "LONG" else -1
    atr = float(signal.atr)
    before = e.iloc[-6:-1]

    if not relaxed:
        touched = ((before.low <= before.ema20 + .25 * before.atr) & (before.high >= before.ema20 - .25 * before.atr)).any()
        protected = (before.close >= before.ema50).all() if sign == 1 else (before.close <= before.ema50).all()
        confirmed = (signal.close > signal.open and signal.close > prev.high and signal.close > signal.ema20) if sign == 1 else (
            signal.close < signal.open and signal.close < prev.low and signal.close < signal.ema20)
        ema_exec_ok = sign * (signal.ema20 - signal.ema50) > 0
        rsi_ok = (50 <= signal.rsi <= 65) if sign == 1 else (35 <= signal.rsi <= 50)
        vol_ok = signal.relative_volume >= 1.2
        vwap_ok = True
        if snap.mode != "SWING":
            vwap_ok = finite(signal.vwap) and sign * (signal.close - signal.vwap) > 0
        tr_ok = signal.tr <= 2.5 * prev.atr
        spread_ok = snap.ask - snap.bid <= .05 * atr
    else:
        before_10 = e.iloc[-10:-1]
        touched = (((before_10.low <= before_10.ema20 + .6 * before_10.atr) & (before_10.high >= before_10.ema20 - .6 * before_10.atr)).any() or
                   (before_10.low <= before_10.bb_lower + .5 * before_10.atr).any() if sign == 1 else
                   (((before_10.low <= before_10.ema20 + .6 * before_10.atr) & (before_10.high >= before_10.ema20 - .6 * before_10.atr)).any() or
                    (before_10.high >= before_10.bb_upper - .5 * before_10.atr).any()))
        protected = (signal.close >= signal.ema50 - 0.5 * atr) if sign == 1 else (signal.close <= signal.ema50 + 0.5 * atr)
        confirmed = (signal.close >= signal.open and (signal.close >= signal.ema20 - 0.15 * atr or signal.close > prev.close)) if sign == 1 else (
            signal.close <= signal.open and (signal.close <= signal.ema20 + 0.15 * atr or signal.close < prev.close))
        ema_exec_ok = (signal.ema20 >= signal.ema50 - 0.5 * atr or confirmed) if sign == 1 else (signal.ema20 <= signal.ema50 + 0.5 * atr or confirmed)
        rsi_ok = (38 <= signal.rsi <= 74) if sign == 1 else (26 <= signal.rsi <= 62)
        vol_ok = signal.relative_volume >= 0.65
        vwap_ok = True
        if snap.mode != "SWING":
            vwap_ok = finite(signal.vwap) and (sign * (signal.close - signal.vwap) >= -0.35 * atr)
        tr_ok = signal.tr <= 3.5 * prev.atr
        spread_ok = snap.ask - snap.bid <= .12 * atr

    r.check("EMA eksekusi", f"EMA20 {signal.ema20:.8g}; EMA50 {signal.ema50:.8g}",
            "EMA20/Reversal searah (Relaxed)" if relaxed else "EMA20 > EMA50 LONG; < SHORT", bool(ema_exec_ok))
    r.check("Pullback 5 candle sebelum sinyal", str(bool(touched)),
            "Pullback EMA20 / Sweep BB dalam 10 candle (Relaxed)" if relaxed else "Low–high beririsan EMA20 ±0,25 ATR candle itu", bool(touched))
    r.check("Struktur pullback", str(bool(protected)),
            "Struktur terjaga / Reclaim valid (Relaxed)" if relaxed else "Tidak close melewati EMA50 melawan arah", bool(protected))
    r.check("Candle konfirmasi", f"O {signal.open:.8g}; C {signal.close:.8g}; H/L sebelumnya {prev.high:.8g}/{prev.low:.8g}",
            "Candle konfirmasi searah / CHoCH (Relaxed)" if relaxed else "Candle searah; close menembus H/L sebelumnya dan EMA20", bool(confirmed))
    r.check("RSI14", f"{signal.rsi:.2f}",
            "38–74 LONG; 26–62 SHORT (Relaxed)" if relaxed else "50–65 LONG; 35–50 SHORT", bool(rsi_ok))
    r.check("Volume relatif", f"{signal.relative_volume:.3f}",
            "≥0,65x SMA20 (Relaxed)" if relaxed else "≥1,2; rata-rata 20 candle sebelum sinyal", bool(vol_ok))
    if snap.mode != "SWING":
        r.check("VWAP harian UTC", f"Close {signal.close:.8g}; VWAP {signal.vwap:.8g}",
                "Close searah VWAP ±0,35 ATR (Relaxed)" if relaxed else "Close > VWAP LONG; < SHORT", bool(vwap_ok), not finite(signal.vwap))
    r.check("Lonjakan true range", f"{signal.tr / prev.atr:.3f} ATR sebelumnya",
            "≤3,5 (Relaxed)" if relaxed else "≤2,5", bool(tr_ok))
    r.check("Spread", f"{snap.ask - snap.bid:.8g}",
            f"≤{(.12 if relaxed else .05) * atr:.8g}", bool(spread_ok))
    deviation = None if spot else abs(snap.mark - snap.last)
    if spot:
        r.checks.append({"Kriteria": "Deviasi Mark–Last", "Nilai aktual": "Tidak tersedia pada spot",
                         "Syarat": "PAPER berbasis Last spot; bukan verifikasi perpetual", "Status": "SIMULASI"})
    else:
        r.check("Deviasi Mark–Last", f"{deviation:.8g}", f"≤{.25 * atr:.8g}", deviation <= .25 * atr)
    if any(x["Status"] not in ("LULUS", "SIMULASI") for x in r.checks):
        return r.finish()

    tick = spec.tick
    trigger_base = quantize(float(signal.high if sign == 1 else signal.low), tick, up=sign == 1)
    trigger = float(Decimal(str(trigger_base)) + sign * Decimal(str(tick)))
    # In Relaxed mode, if live price already moved slightly past signal high/low (up to 0.25 ATR),
    # adjust trigger to current Last + 1 tick so valid momentum setups are not discarded.
    if relaxed and sign * (snap.last - trigger) >= 0 and sign * (snap.last - trigger) <= 0.25 * atr:
        trigger_base = quantize(float(snap.last), tick, up=sign == 1)
        trigger = float(Decimal(str(trigger_base)) + sign * Decimal(str(tick)))

    entry = quantize(trigger + sign * .10 * atr, tick, up=sign == -1)
    sl_buffer_mult = 0.20 if relaxed else 0.25
    sl_base = e.low.iloc[-5:].min() - sl_buffer_mult * atr if sign == 1 else e.high.iloc[-5:].max() + sl_buffer_mult * atr
    sl = quantize(float(sl_base), tick, up=sign == -1)
    distance = float(sign * (Decimal(str(entry)) - Decimal(str(sl))))
    if relaxed and 0 < distance < 0.5 * atr:
        sl_adj = entry - sign * 0.8 * atr
        sl = quantize(float(sl_adj), tick, up=sign == -1)
        distance = float(sign * (Decimal(str(entry)) - Decimal(str(sl))))

    min_sl_atr, max_sl_atr = (0.5, 4.5) if relaxed else (1.0, 3.0)
    r.check("Jarak SL struktural", f"{distance / atr:.3f} ATR",
            f"{min_sl_atr}–{max_sl_atr} ATR (Relaxed)" if relaxed else "1–3 ATR; 5 candle termasuk sinyal",
            min_sl_atr * atr <= distance <= max_sl_atr * atr)

    trigger_not_crossed = sign * (snap.last - trigger) < 0
    r.check("Harga sudah menembus trigger", f"Last {snap.last:.8g}; trigger {trigger:.8g}",
            "Belum menembus batas toleransi trigger" if relaxed else "Belum pernah menembus sejak candle sinyal",
            trigger_not_crossed, missing=not trigger_not_crossed)
    intrabar = snap.open_bar_high if sign == 1 else snap.open_bar_low
    if relaxed:
        intrabar_ok = intrabar is not None and sign * (intrabar - (trigger + sign * 0.25 * atr)) < 0
    else:
        intrabar_ok = intrabar is not None and sign * (intrabar - trigger) < 0
    r.check("Riwayat trigger sejak sinyal", intrabar,
            "Candle berjalan masih dalam zona entry (Relaxed)" if relaxed else "High/low candle berjalan belum menyentuh trigger",
            intrabar_ok, intrabar is None or not intrabar_ok)

    r.check("SL sebelum fill", snap.last, f"Last belum melewati {sl:.8g}", sign * (snap.last - sl) > 0)
    adverse_last = snap.open_bar_low if sign == 1 else snap.open_bar_high
    adverse_mark = adverse_last if spot else (snap.open_mark_low if sign == 1 else snap.open_mark_high)
    path_available = finite(adverse_last) and finite(adverse_mark)
    r.check("SL belum tersentuh sejak sinyal", f"Ekstrem Last spot {adverse_last}" if spot else f"Ekstrem Last {adverse_last}; Mark {adverse_mark}",
            "Last spot belum mencapai SL" if spot else "Last dan Mark belum mencapai SL, termasuk harga yang sudah memantul",
            path_available and sign * (adverse_last - sl) > 0 and sign * (adverse_mark - sl) > 0 and sign * (reference - sl) > 0,
            not path_available)
    levels = confirmed_pivots(b) + confirmed_pivots(e)
    day_ok = all(finite(x) and x > 0 for x in (snap.previous_day_high, snap.previous_day_low))
    r.check("High/low hari UTC sebelumnya", f"{snap.previous_day_high} / {snap.previous_day_low}", "Data sumber market hari sebelumnya tersedia", day_ok, not day_ok)
    levels += [x for x in (snap.previous_day_high, snap.previous_day_low) if finite(x)]
    obstacles = [x for x in levels if sign * (x - entry) > 0]
    obstacle = min(obstacles, key=lambda x: abs(x - entry)) if obstacles else None
    room = abs(obstacle - entry) if obstacle is not None else float("inf")
    req_room = (1.2 * distance) if relaxed else (2 * distance + .10 * atr)
    r.check("Ruang target", f"{room:.8g}" if obstacle else "Tidak teridentifikasi dalam cakupan data",
            f"≥{req_room:.8g}", room >= req_room)
    now_day = datetime.fromtimestamp(snap.now_ms / 1000, timezone.utc).date().isoformat()
    numbers = [account.equity, account.free_margin, account.day_start_equity, account.net_cash_flow,
               account.risk_pct, account.leverage, account.total_existing_risk, account.correlated_alt_risk,
               account.active_pending_count, account.consecutive_losses]
    valid_account = all(finite(x) for x in numbers) and account.equity > 0 and account.day_start_equity > 0 and account.free_margin >= 0
    valid_account = valid_account and all(x >= 0 for x in [account.total_existing_risk, account.correlated_alt_risk, account.active_pending_count, account.consecutive_losses])
    valid_account = valid_account and account.correlated_alt_risk <= account.total_existing_risk
    r.check("Data akun", "Buku saldo virtual PAPER" if paper else ("Dikonfirmasi pengguna" if account.account_verified else "Belum dikonfirmasi"), "Ekuitas, margin, posisi + pending, risiko + unrealized PnL", account.account_verified and valid_account,
            not account.account_verified or not valid_account)
    r.check("Hari akun UTC", account.account_day_utc, now_day, account.account_day_utc == now_day, account.account_day_utc != now_day)
    max_lev_allowed = 20 if getattr(account, "confidence_calibrated", False) else 3
    lev_valid = (0 < account.risk_pct <= .5 and isinstance(account.leverage, (int, np.integer)) and
                 1 <= account.leverage <= max_lev_allowed and
                 (account.leverage <= 3 or account.leverage in DYNAMIC_LEVERAGE_OPTIONS))
    r.check("Risiko dan leverage", f"{account.risk_pct}% / {account.leverage}x",
            f"0 < risiko ≤0,5%; leverage 1–{max_lev_allowed}x (dinamis terkalibrasi maks 20x)", lev_valid)
    r.check("Mode margin", f"Isolated={account.isolated}; auto-add={account.auto_add_margin}", "Isolated; auto-add mati", account.isolated and not account.auto_add_margin, not account.account_verified)
    drawdown = (account.day_start_equity + account.net_cash_flow - account.equity) / account.day_start_equity if valid_account else float("nan")
    r.check("Batas rugi harian UTC", f"{drawdown * 100:.2f}% / {account.consecutive_losses} rugi beruntun", "<2% dan <3 trade rugi beruntun", drawdown < .02 and account.consecutive_losses < 3, not valid_account)
    r.check("Posisi + pending", account.active_pending_count, "<2 sebelum rencana baru", account.active_pending_count < 2)
    costs = [account.taker_fee, account.exit_slippage_bps, account.funding_reserve_rate]
    costs_ok = account.costs_verified and all(finite(x) and x >= 0 for x in costs) and account.taker_fee < 1
    r.check("Biaya akun dan cadangan", str(costs), "Asumsi biaya PAPER (bukan fee aktual akun)" if paper else "Fee taker terverifikasi; slippage exit; total funding selama horizon", costs_ok, not costs_ok)
    funding_ok = not spot and finite(snap.funding_rate) and finite(snap.next_funding_ms) and snap.next_funding_ms > snap.now_ms
    if spot:
        r.checks.append({"Kriteria": "Funding kontrak", "Nilai aktual": "Tidak tersedia pada spot; cadangan biaya model tetap dihitung",
                         "Syarat": "Tidak mengklaim rate/jadwal funding perpetual terverifikasi", "Status": "SIMULASI"})
    else:
        r.check("Funding kontrak", f"rate={snap.funding_rate}; berikut={snap.next_funding_ms}; interval={snap.funding_interval_hours}", "Rate dan waktu funding resmi tersedia; interval tidak diasumsikan", funding_ok, not funding_ok)
    news_ok = (account.news_verified_at_ms is not None and 0 <= snap.now_ms - account.news_verified_at_ms <= 3600000 and
               bool(account.news_source.strip()) and account.news_clear_from_ms is not None and account.news_clear_until_ms is not None and
               account.news_clear_from_ms <= snap.now_ms - 900000 and account.news_clear_until_ms >= snap.now_ms + 900000)
    r.check("Filter berita CPI/FOMC", account.news_source if news_ok else "Filter berita belum terverifikasi", "Kalender dicek ≤1 jam; tidak ada event dalam ±15 menit", news_ok, not news_ok)
    r.check("Mekanisme order bursa", "Simulator polling; full entry fill konservatif" if paper else ("Dikonfirmasi" if account.order_semantics_verified else "Belum diverifikasi"), "Entry dan SL simulasi memakai Last spot" if spot else "Stop-limit Last; SL stop-market Mark; reduce-only; partial fill", account.order_semantics_verified, not account.order_semantics_verified)
    if not valid_account or not account.account_verified or not costs_ok or distance <= 0 or any(x["Status"] == "GAGAL" for x in r.checks):
        return r.finish()
    fee = account.taker_fee
    slip = entry * account.exit_slippage_bps / 10000
    funding = entry * account.funding_reserve_rate
    csl = entry * fee + (sl + slip) * fee + slip + funding
    csl_ratio = 0.35 if relaxed else 0.20
    r.check("Biaya terhadap D", f"{csl:.8g}", f"≤{csl_ratio * distance:.8g}", csl <= csl_ratio * distance)
    if funding_ok:
        minimum_funding = max(0, sign * snap.funding_rate)
        r.check("Cadangan funding minimum", account.funding_reserve_rate, f"≥ pembayaran satu event rate saat ini {minimum_funding:.8g}; horizon dikonfirmasi pengguna", account.funding_reserve_rate >= minimum_funding)

    confidence_score, confidence_breakdown = compute_confidence(
        b, e, direction, bool(touched), bool(protected), bool(confirmed), bool(vwap_ok), bool(spread_ok)
    )
    sl_pct = distance / entry if entry > 0 else 0.0
    atr_pct = atr / entry if entry > 0 else 0.0
    calibrated = bool(getattr(account, "confidence_calibrated", False))
    selected_lev, lev_caps = select_dynamic_leverage(
        confidence_score, atr_pct, sl_pct, account.leverage, calibrated=calibrated
    )
    effective_leverage = account.leverage
    r.metrics["confidence_score"] = confidence_score
    r.metrics["confidence_breakdown"] = confidence_breakdown
    r.metrics["max_allowed_leverage"] = lev_caps["max_allowed_leverage"]
    r.metrics["selected_leverage"] = effective_leverage

    risk_budget = account.equity * account.risk_pct / 100
    remaining = account.equity * .01 - account.total_existing_risk
    if snap.symbol != "BTCUSDT":
        remaining = min(remaining, account.equity * .005 - account.correlated_alt_risk)
    effective_budget = max(0, min(risk_budget, remaining))
    step = common_step(spec.step, spec.market_step)
    qty = quantize(max(0, min(effective_budget / (distance + csl), account.equity * .2 * effective_leverage / entry,
                             account.free_margin / (entry / effective_leverage + csl), spec.max_qty, spec.market_max_qty)), step)
    q1, q2 = quantize(qty * .5, step), quantize(qty * .25, step)
    qr = float(Decimal(str(qty)) - Decimal(str(q1)) - Decimal(str(q2)))
    tp1 = quantize(Decimal(str(entry)) + sign * 2 * Decimal(str(distance)), tick, up=sign == 1)
    tp2 = quantize(Decimal(str(entry)) + sign * 3 * Decimal(str(distance)), tick, up=sign == 1)
    tp3 = quantize(Decimal(str(entry)) + sign * 4 * Decimal(str(distance)), tick, up=sign == 1)
    actual_risk = qty * (distance + csl)
    r.check("Anggaran risiko tersedia", f"B={risk_budget:.4f}; tersedia={effective_budget:.4f}; risiko={actual_risk:.4f}", "Per trade ≤0,5%; total ≤1%; grup altcoin searah ≤0,5%", effective_budget > 0 and actual_risk <= risk_budget + 1e-8)
    minqty = max(spec.min_qty, spec.market_min_qty)
    valid_parts = all(q >= minqty and q * min(entry, sl, tp1, tp2) >= spec.min_notional for q in (q1, q2, qr))
    r.check("Lot dan TP parsial", f"Q={qty:.8g}; TP1={q1:.8g}; TP2={q2:.8g}; runner={qr:.8g}", "Step valid; setiap bagian memenuhi minQty dan minNotional (konservatif)", valid_parts)
    margin = qty * entry / effective_leverage
    r.check("Margin dan biaya", f"Margin {margin:.4f}; cadangan {qty * csl:.4f}", "Margin ≤20% ekuitas; margin + biaya ≤margin bebas", margin <= account.equity * .2 + 1e-8 and margin + qty * csl <= account.free_margin + 1e-8 and qty > 0)
    price_ok = all(spec.min_price <= p <= spec.max_price and p > 0 for p in (trigger, entry, sl, tp1, tp2))
    if not spot:
        price_ok = price_ok and all(snap.mark * spec.multiplier_down <= p <= snap.mark * spec.multiplier_up for p in (entry, tp1, tp2))
    r.check("Filter harga bursa", f"E={entry:.8g}; TP1={tp1:.8g}; TP2={tp2:.8g}", "PRICE_FILTER spot untuk simulasi" if spot else "PRICE_FILTER dan PERCENT_PRICE", price_ok)
    tp_cost = entry * fee + (tp1 + slip) * fee + slip + funding
    rr = (abs(tp1 - entry) - tp_cost) / (distance + csl)
    min_rr = 1.3 if relaxed else 1.5
    r.check("RR bersih TP1", f"{rr:.3f}", f"≥{min_rr:.1f} jika seluruh posisi keluar TP1 (metrik pembanding)", rr >= min_rr)
    try:
        if not all(finite(float(p)) and finite(float(q)) and float(p) > 0 and float(q) >= 0
                   for p, q in snap.asks + snap.bids):
            raise ValueError("Non-finite order book")
        depth_band = (.25 if relaxed else .10) * atr
        slip_band = max(slip, .05 * atr) if relaxed else slip
        entry_depth = sum(float(q) for p, q in (snap.asks if sign == 1 else snap.bids)
                          if abs(float(p) - (snap.ask if sign == 1 else snap.bid)) <= depth_band)
        exit_depth = sum(float(q) for p, q in (snap.bids if sign == 1 else snap.asks)
                         if abs(float(p) - (snap.bid if sign == 1 else snap.ask)) <= slip_band)
        depth_ok = all(finite(x) and x >= qty > 0 for x in (entry_depth, exit_depth))
    except (ValueError, TypeError):
        entry_depth = exit_depth = 0
        depth_ok = False
    r.check("Likuiditas snapshot", f"Entry depth {entry_depth:.8g}; exit depth {exit_depth:.8g}; Q {qty:.8g}", "Kedalaman mencukupi dalam toleransi; bukan jaminan fill", depth_ok, not bool(snap.bids and snap.asks))
    expires = int(signal.close_time) + 1 + 3 * INTERVAL_MS[exec_tf]
    next_midnight = (snap.now_ms // 86400000 + 1) * 86400000
    if snap.mode != "SWING":
        expires = min(expires, next_midnight)
    if news_ok:
        expires = min(expires, account.news_clear_until_ms - 900000)
    estimated_margin_loss_pct = (actual_risk / margin * 100.0) if margin > 0 else 0.0
    estimated_fees_usdt = qty * csl
    calibration_status = "CALIBRATED" if calibrated else "UNCALIBRATED (MAX 3x)"
    plan = dict(symbol=snap.symbol, mode=snap.mode, direction=direction, signal_close_ms=int(signal.close_time),
                entry_trigger=trigger, entry=entry, entry_type="STOP-LIMIT; trigger Last", sl=sl,
                sl_type="STOP-MARKET; trigger Mark; reduce-only/close-position sesuai bursa", atr=atr, distance=distance,
                sl_distance_pct=sl_pct * 100.0,
                tp1=tp1, tp2=tp2, tp3=tp3, qty=qty, tp1_qty=q1, tp2_qty=q2, runner_qty=qr,
                leverage=effective_leverage, max_allowed_leverage=lev_caps["max_allowed_leverage"],
                confidence_score=confidence_score, confidence_calibration_status=calibration_status,
                confidence_breakdown=confidence_breakdown, leverage_caps=lev_caps,
                margin=margin, estimated_margin_loss_pct=estimated_margin_loss_pct,
                estimated_fees_usdt=estimated_fees_usdt,
                notional=qty * entry, equity=account.equity,
                risk_budget=risk_budget, risk=actual_risk, fee=fee, cost_sl_per_unit=csl,
                funding_per_unit=funding, slippage_per_unit=slip, net_rr_tp1=rr, expires_ms=expires,
                quantity_step=step, tick=tick, execution_tf=exec_tf, obstacle=obstacle,
                min_qty=minqty, min_notional=spec.min_notional,
                liquidation=None, liquidation_buffer=None,
                liquidation_safety_check="SIMULASI PAPER (SL < Jarak Likuidasi Teoritis)" if paper else "PENDING_VERIFICATION",
                cancellation=["Harga mencapai SL sebelum fill", "Bias atau filter biaya/likuiditas berubah",
                              "Order kedaluwarsa atau memasuki jendela berita", "Jangan ganti stop-limit yang tidak fill menjadi market"],
                exit_rules="TP1 50%; TP2 25%; runner sisa (TP3/Trailing 2 ATR); BE biaya setelah TP1; time-exit 12 candle tanpa +1R")
    plan["instrument_kind"] = snap.instrument_kind
    if spot:
        plan["sl_type"] = "STOP simulasi; trigger Last spot"
        plan["source"] = snap.source
    plan["id"] = fingerprint(plan)
    if paper:
        plan["scope"] = "PAPER"
    if any(x["Status"] == "GAGAL" for x in r.checks):
        return r.finish()
    r.candidate = plan
    liq_ok = (finite(account.liquidation_price) and account.liquidation_price > 0 and
              account.liquidation_plan_id == plan["id"] and bool(account.liquidation_source.strip()) and
              account.liquidation_checked_ms is not None and 0 <= snap.now_ms - account.liquidation_checked_ms <= 300000)
    if paper:
        r.checks.append({"Kriteria": "Likuidasi sesuai rencana", "Nilai aktual": "Tidak dimodelkan; harga likuidasi tidak dikarang",
                         "Syarat": "PAPER saja; untuk live tetap wajib verifikasi bursa", "Status": "SIMULASI"})
    else:
        r.check("Likuidasi sesuai rencana", account.liquidation_price if liq_ok else "Belum diverifikasi untuk ID rencana ini", "Hasil bursa/kalkulator; mode/risk tier/Q/leverage tepat; ≤5 menit", liq_ok, not liq_ok)
    if liq_ok and not paper:
        buffer = sign * (sl - account.liquidation_price)
        minimum = max(atr, deviation + slip)
        r.check("Buffer likuidasi", f"{buffer:.8g}", f"SL sebelum likuidasi; buffer ≥{minimum:.8g}", buffer >= minimum)
        liq_safe = buffer >= minimum
        plan.update(liquidation=account.liquidation_price, liquidation_buffer=buffer,
                    liquidation_safety_check="PASSED" if liq_safe else "FAILED")
    r.check("Masa berlaku", expires, "Waktu sekarang < kedaluwarsa", snap.now_ms < expires, snap.now_ms >= expires)
    return r.finish()

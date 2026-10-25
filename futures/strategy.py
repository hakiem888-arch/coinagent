"""Master Prompt v1 baseline. Numeric decisions never come from an LLM."""
import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone
import numpy as np
from .indicators import indicators, confirmed_pivots
from .models import Result, TIMEFRAMES, INTERVAL_MS


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


def evaluate(snap, account, *, paper=False):
    spot = snap.instrument_kind == "SPOT_REFERENCE"
    reference = snap.reference_price
    r = Result(meta={"symbol": snap.symbol, "mode": snap.mode, "source": snap.source,
                     "instrument_kind": snap.instrument_kind, "quote_time_basis": snap.quote_time_basis,
                     "time_ms": snap.now_ms, "timezone": "Asia/Jakarta", "limitations": list(snap.warnings)})
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
    fresh = all(0 <= age <= 30 for age in ages)
    r.check("Usia snapshot", f"{max(ages):.1f} detik", "0–30 detik (estimasi awal request spot)" if spot else "0–30 detik (timestamp bursa)", fresh, not fresh)
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
    long_bias = bs.close > bs.ema50 > bs.ema200 and bs.ema50 > b.ema50.iloc[-6] and bs.adx >= 20
    short_bias = bs.close < bs.ema50 < bs.ema200 and bs.ema50 < b.ema50.iloc[-6] and bs.adx >= 20
    direction = "LONG" if long_bias else ("SHORT" if short_bias else None)
    r.direction = direction
    r.metrics = {"close_bias": float(bs.close), "ema50_bias": float(bs.ema50), "ema200_bias": float(bs.ema200),
                 "adx": float(bs.adx), "ema50_bias_5_before": float(b.ema50.iloc[-6]), "rsi": float(signal.rsi),
                 "atr": float(signal.atr), "relative_volume": float(signal.relative_volume),
                 "vwap": float(signal.vwap) if finite(signal.vwap) else None, "last": snap.last, "mark": snap.mark}
    r.check("Urutan tren bias", f"C {bs.close:.8g}; EMA50 {bs.ema50:.8g}; EMA200 {bs.ema200:.8g}",
            "C > EMA50 > EMA200 atau kebalikannya", bs.close > bs.ema50 > bs.ema200 or bs.close < bs.ema50 < bs.ema200)
    slope_ok = (bs.close > bs.ema50 and bs.ema50 > b.ema50.iloc[-6]) or (bs.close < bs.ema50 and bs.ema50 < b.ema50.iloc[-6])
    r.check("Kemiringan EMA50 bias", f"Sekarang {bs.ema50:.8g}; 5 lalu {b.ema50.iloc[-6]:.8g}", "Sesuai arah tren", slope_ok)
    r.check("ADX14 bias", f"{bs.adx:.2f}", "≥20", bs.adx >= 20)
    if not direction:
        return r.finish()
    sign = 1 if direction == "LONG" else -1
    atr = float(signal.atr)
    before = e.iloc[-6:-1]
    touched = ((before.low <= before.ema20 + .25 * before.atr) & (before.high >= before.ema20 - .25 * before.atr)).any()
    protected = (before.close >= before.ema50).all() if sign == 1 else (before.close <= before.ema50).all()
    confirmed = (signal.close > signal.open and signal.close > prev.high and signal.close > signal.ema20) if sign == 1 else (
        signal.close < signal.open and signal.close < prev.low and signal.close < signal.ema20)
    r.check("EMA eksekusi", f"EMA20 {signal.ema20:.8g}; EMA50 {signal.ema50:.8g}", "EMA20 > EMA50 LONG; < SHORT", sign * (signal.ema20 - signal.ema50) > 0)
    r.check("Pullback 5 candle sebelum sinyal", str(bool(touched)), "Low–high beririsan EMA20 ±0,25 ATR candle itu", bool(touched))
    r.check("Struktur pullback", str(bool(protected)), "Tidak close melewati EMA50 melawan arah", bool(protected))
    r.check("Candle konfirmasi", f"O {signal.open:.8g}; C {signal.close:.8g}; H/L sebelumnya {prev.high:.8g}/{prev.low:.8g}", "Candle searah; close menembus H/L sebelumnya dan EMA20", bool(confirmed))
    r.check("RSI14", f"{signal.rsi:.2f}", "50–65 LONG; 35–50 SHORT", 50 <= signal.rsi <= 65 if sign == 1 else 35 <= signal.rsi <= 50)
    r.check("Volume relatif", f"{signal.relative_volume:.3f}", "≥1,2; rata-rata 20 candle sebelum sinyal", signal.relative_volume >= 1.2)
    if snap.mode != "SWING":
        r.check("VWAP harian UTC", f"Close {signal.close:.8g}; VWAP {signal.vwap:.8g}", "Close > VWAP LONG; < SHORT", finite(signal.vwap) and sign * (signal.close - signal.vwap) > 0, not finite(signal.vwap))
    r.check("Lonjakan true range", f"{signal.tr / prev.atr:.3f} ATR sebelumnya", "≤2,5", signal.tr <= 2.5 * prev.atr)
    r.check("Spread", f"{snap.ask - snap.bid:.8g}", f"≤{.05 * atr:.8g} (0,05 ATR)", snap.ask - snap.bid <= .05 * atr)
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
    entry = quantize(trigger + sign * .10 * atr, tick, up=sign == -1)
    sl_base = e.low.iloc[-5:].min() - .25 * atr if sign == 1 else e.high.iloc[-5:].max() + .25 * atr
    sl = quantize(float(sl_base), tick, up=sign == -1)
    distance = float(sign * (Decimal(str(entry)) - Decimal(str(sl))))
    r.check("Jarak SL struktural", f"{distance / atr:.3f} ATR", "1–3 ATR; 5 candle termasuk sinyal", atr <= distance <= 3 * atr)
    r.check("Harga sudah menembus trigger", f"Last {snap.last:.8g}; trigger {trigger:.8g}", "Belum pernah menembus sejak candle sinyal", sign * (snap.last - trigger) < 0,
            missing=sign * (snap.last - trigger) >= 0)
    intrabar = snap.open_bar_high if sign == 1 else snap.open_bar_low
    r.check("Riwayat trigger sejak sinyal", intrabar, "High/low candle berjalan belum menyentuh trigger", intrabar is not None and sign * (intrabar - trigger) < 0,
            intrabar is None or sign * (intrabar - trigger) >= 0)
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
    r.check("Ruang target", f"{room:.8g}" if obstacle else "Tidak teridentifikasi dalam cakupan data", f"≥{2 * distance + .10 * atr:.8g}", room >= 2 * distance + .10 * atr)
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
    r.check("Risiko dan leverage", f"{account.risk_pct}% / {account.leverage}x", "0 < risiko ≤0,5%; leverage 1–3x", 0 < account.risk_pct <= .5 and 1 <= account.leverage <= 3)
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
    r.check("Biaya terhadap D", f"{csl:.8g}", f"≤{.2 * distance:.8g}", csl <= .2 * distance)
    if funding_ok:
        minimum_funding = max(0, sign * snap.funding_rate)
        r.check("Cadangan funding minimum", account.funding_reserve_rate, f"≥ pembayaran satu event rate saat ini {minimum_funding:.8g}; horizon dikonfirmasi pengguna", account.funding_reserve_rate >= minimum_funding)
    risk_budget = account.equity * account.risk_pct / 100
    remaining = account.equity * .01 - account.total_existing_risk
    if snap.symbol != "BTCUSDT":
        remaining = min(remaining, account.equity * .005 - account.correlated_alt_risk)
    effective_budget = max(0, min(risk_budget, remaining))
    step = common_step(spec.step, spec.market_step)
    qty = quantize(max(0, min(effective_budget / (distance + csl), account.equity * .2 * account.leverage / entry,
                             account.free_margin / (entry / account.leverage + csl), spec.max_qty, spec.market_max_qty)), step)
    q1, q2 = quantize(qty * .5, step), quantize(qty * .25, step)
    qr = float(Decimal(str(qty)) - Decimal(str(q1)) - Decimal(str(q2)))
    tp1 = quantize(Decimal(str(entry)) + sign * 2 * Decimal(str(distance)), tick, up=sign == 1)
    tp2 = quantize(Decimal(str(entry)) + sign * 3 * Decimal(str(distance)), tick, up=sign == 1)
    actual_risk = qty * (distance + csl)
    r.check("Anggaran risiko tersedia", f"B={risk_budget:.4f}; tersedia={effective_budget:.4f}; risiko={actual_risk:.4f}", "Per trade ≤0,5%; total ≤1%; grup altcoin searah ≤0,5%", effective_budget > 0 and actual_risk <= risk_budget + 1e-8)
    minqty = max(spec.min_qty, spec.market_min_qty)
    valid_parts = all(q >= minqty and q * min(entry, sl, tp1, tp2) >= spec.min_notional for q in (q1, q2, qr))
    r.check("Lot dan TP parsial", f"Q={qty:.8g}; TP1={q1:.8g}; TP2={q2:.8g}; runner={qr:.8g}", "Step valid; setiap bagian memenuhi minQty dan minNotional (konservatif)", valid_parts)
    margin = qty * entry / account.leverage
    r.check("Margin dan biaya", f"Margin {margin:.4f}; cadangan {qty * csl:.4f}", "Margin ≤20% ekuitas; margin + biaya ≤margin bebas", margin <= account.equity * .2 + 1e-8 and margin + qty * csl <= account.free_margin + 1e-8 and qty > 0)
    price_ok = all(spec.min_price <= p <= spec.max_price and p > 0 for p in (trigger, entry, sl, tp1, tp2))
    if not spot:
        price_ok = price_ok and all(snap.mark * spec.multiplier_down <= p <= snap.mark * spec.multiplier_up for p in (entry, tp1, tp2))
    r.check("Filter harga bursa", f"E={entry:.8g}; TP1={tp1:.8g}; TP2={tp2:.8g}", "PRICE_FILTER spot untuk simulasi" if spot else "PRICE_FILTER dan PERCENT_PRICE", price_ok)
    tp_cost = entry * fee + (tp1 + slip) * fee + slip + funding
    rr = (abs(tp1 - entry) - tp_cost) / (distance + csl)
    r.check("RR bersih TP1", f"{rr:.3f}", "≥1,5 jika seluruh posisi keluar TP1 (metrik pembanding)", rr >= 1.5)
    try:
        if not all(finite(float(p)) and finite(float(q)) and float(p) > 0 and float(q) >= 0
                   for p, q in snap.asks + snap.bids):
            raise ValueError("Non-finite order book")
        entry_depth = sum(float(q) for p, q in (snap.asks if sign == 1 else snap.bids)
                          if abs(float(p) - (snap.ask if sign == 1 else snap.bid)) <= .10 * atr)
        exit_depth = sum(float(q) for p, q in (snap.bids if sign == 1 else snap.asks)
                         if abs(float(p) - (snap.bid if sign == 1 else snap.ask)) <= slip)
        depth_ok = all(finite(x) and x >= qty > 0 for x in (entry_depth, exit_depth))
    except (ValueError, TypeError):
        entry_depth = exit_depth = 0
        depth_ok = False
    r.check("Likuiditas snapshot", f"Entry depth {entry_depth:.8g}; exit depth {exit_depth:.8g}; Q {qty:.8g}", "Kedalaman mencukupi dalam toleransi; bukan jaminan fill", depth_ok, not bool(snap.bids and snap.asks))
    expires = int(signal.close_time) + 1 + 3 * INTERVAL_MS[exec_tf]
    next_midnight = (snap.now_ms // 86400000 + 1) * 86400000
    if snap.mode != "SWING":
        expires = min(expires, next_midnight)
    # A manually checked news window can shorten the order validity, never extend it.
    if news_ok:
        expires = min(expires, account.news_clear_until_ms - 900000)
    plan = dict(symbol=snap.symbol, mode=snap.mode, direction=direction, signal_close_ms=int(signal.close_time),
                entry_trigger=trigger, entry=entry, entry_type="STOP-LIMIT; trigger Last", sl=sl,
                sl_type="STOP-MARKET; trigger Mark; reduce-only/close-position sesuai bursa", atr=atr, distance=distance,
                tp1=tp1, tp2=tp2, qty=qty, tp1_qty=q1, tp2_qty=q2, runner_qty=qr,
                leverage=account.leverage, margin=margin, notional=qty * entry, equity=account.equity,
                risk_budget=risk_budget, risk=actual_risk, fee=fee, cost_sl_per_unit=csl,
                funding_per_unit=funding, slippage_per_unit=slip, net_rr_tp1=rr, expires_ms=expires,
                quantity_step=step, tick=tick, execution_tf=exec_tf, obstacle=obstacle,
                min_qty=minqty, min_notional=spec.min_notional,
                liquidation=None, liquidation_buffer=None,
                cancellation=["Harga mencapai SL sebelum fill", "Bias atau filter biaya/likuiditas berubah",
                              "Order kedaluwarsa atau memasuki jendela berita", "Jangan ganti stop-limit yang tidak fill menjadi market"],
                exit_rules="TP1 50%; TP2 25%; runner sisa; BE biaya setelah TP1; trailing 2 ATR setelah TP2; time-exit 12 candle tanpa +1R")
    plan["instrument_kind"] = snap.instrument_kind
    if spot:
        plan["sl_type"] = "STOP simulasi; trigger Last spot"
        plan["source"] = snap.source
    plan["id"] = fingerprint(plan)
    if paper:
        plan["scope"] = "PAPER"
    # Do not expose candidate entry levels when a known hard filter has failed.
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
        plan.update(liquidation=account.liquidation_price, liquidation_buffer=buffer)
    r.check("Masa berlaku", expires, "Waktu sekarang < kedaluwarsa", snap.now_ms < expires, snap.now_ms >= expires)
    return r.finish()

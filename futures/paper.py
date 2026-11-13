"""Persistent virtual portfolio; observed-quote fills, never real exchange orders.

The funding reserve is debited as an assumed total cost on entry. Liquidation
and exact tick-by-tick paths are NOT modeled. Stop losses cannot cap gap losses.
"""
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .models import Account, TIMEFRAMES
from .indicators import indicators
from .strategy import break_even, quantize, frame_problem
from .lifecycle import after_fill, manage_position
from .telegram import eligible, candle_key

ACTIVE = ("PENDING", "OPEN")


class PaperLedger:
    def __init__(self, db, initial_equity=1000.0):
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("Saldo awal paper harus positif")
        self.db, self.initial_equity = Path(db), float(initial_equity)

    def connect(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS paper_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            signal TEXT PRIMARY KEY, symbol TEXT, mode TEXT, status TEXT,
            realized REAL, updated_ms INTEGER, payload TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS paper_status ON paper_trades(status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signal TEXT, at_ms INTEGER,
            event TEXT, qty REAL, price REAL, pnl REAL, note TEXT)""")
        conn.execute("INSERT OR IGNORE INTO paper_meta VALUES ('initial_equity',?)", (str(self.initial_equity),))
        conn.commit()
        return conn

    @staticmethod
    def _rows(conn, active=False):
        where = " WHERE status IN ('PENDING','OPEN')" if active else ""
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades" + where + " ORDER BY updated_ms DESC")]

    @staticmethod
    def _store(conn, t):
        conn.execute("INSERT OR REPLACE INTO paper_trades VALUES (?,?,?,?,?,?,?)", (
            t["signal"], t["plan"]["symbol"], t["plan"]["mode"], t["status"], t["realized_pnl"], t["updated_ms"],
            json.dumps(t, allow_nan=False)))

    @staticmethod
    def _event(conn, t, kind, qty=0, price=None, pnl=0, note=""):
        conn.execute("INSERT INTO paper_events(signal,at_ms,event,qty,price,pnl,note) VALUES (?,?,?,?,?,?,?)",
                     (t["signal"], t["updated_ms"], kind, qty, price, pnl, note))

    @staticmethod
    def _risk(t):
        fraction = 1 if t["status"] == "PENDING" else t["remaining_qty"] / t["plan"]["qty"]
        return t["plan"]["risk"] * fraction

    @staticmethod
    def _reserved(t):
        p = t["plan"]
        q = p["qty"] if t["status"] == "PENDING" else t["remaining_qty"]
        return q * (p["entry"] / p["leverage"] + p["cost_sl_per_unit"])

    def _account(self, conn, now_ms, exclude_signal=None):
        active = self._rows(conn, True)
        active = [t for t in active if not (t["signal"] == exclude_signal and t["status"] == "PENDING")]
        initial = float(conn.execute("SELECT value FROM paper_meta WHERE key='initial_equity'").fetchone()[0])
        realized = float(conn.execute("SELECT COALESCE(SUM(realized),0) FROM paper_trades").fetchone()[0])
        unrealized = 0.0
        for t in active:
            if t["status"] == "OPEN":
                p = t["plan"]
                sign = 1 if p["direction"] == "LONG" else -1
                unrealized += t["remaining_qty"] * (sign * (t["mark"] - p["entry"]) - t["mark"] * p["fee"] - p["slippage_per_unit"])
        equity = initial + realized + unrealized
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date().isoformat()
        stored = dict(conn.execute("SELECT key,value FROM paper_meta"))
        # An older UI report must never rewind the worker's current risk day.
        # Otherwise alternating old/new snapshots could reset daily drawdown.
        day = max(day, stored.get("day", day))
        if stored.get("day") != day:
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('day',?)", (day,))
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('day_start',?)", (str(equity),))
            start = equity
        else:
            start = float(stored["day_start"])
        midnight = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        closed = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM paper_trades WHERE status='CLOSED' AND updated_ms>=? ORDER BY updated_ms DESC", (midnight,))]
        losses = 0
        for t in closed:
            if t["realized_pnl"] >= 0:
                break
            losses += 1
        return {"initial_equity": initial, "wallet": initial + realized, "equity": equity,
                "realized": realized, "unrealized": unrealized, "day_start": start, "day": day,
                "free_margin": max(0, equity - sum(self._reserved(t) for t in active)),
                "active": len(active), "total_risk": sum(self._risk(t) for t in active),
                # Treat all non-BTC coins as a correlated group conservatively.
                "alt_risk": sum(self._risk(t) for t in active if t["plan"]["symbol"] != "BTCUSDT"), "losses": losses}

    def summary(self, now_ms):
        with closing(self.connect()) as conn, conn:
            return self._account(conn, now_ms)

    def account(self, now_ms, news, fee=.0005, slippage_bps=2.0, funding=.0003, leverage=2, risk_pct=.5, exclude_signal=None):
        with closing(self.connect()) as conn, conn:
            s = self._account(conn, now_ms, exclude_signal)
        a = Account(equity=s["equity"], free_margin=s["free_margin"], day_start_equity=s["day_start"],
                    account_day_utc=s["day"], account_verified=True, active_pending_count=s["active"],
                    total_existing_risk=s["total_risk"], correlated_alt_risk=s["alt_risk"], consecutive_losses=s["losses"],
                    taker_fee=fee, exit_slippage_bps=slippage_bps, funding_reserve_rate=funding, costs_verified=True,
                    leverage=leverage, risk_pct=risk_pct, isolated=True, order_semantics_verified=True)
        if news.get("ok"):
            a.news_source, a.news_verified_at_ms = news["source"], news["checked_ms"]
            a.news_clear_from_ms, a.news_clear_until_ms = news["from_ms"], news["until_ms"]
        return a

    def active(self):
        with closing(self.connect()) as conn:
            return self._rows(conn, True)

    def expire_pending(self, now_ms):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for t in self._rows(conn, True):
                if t["status"] == "PENDING" and now_ms >= t["plan"]["expires_ms"]:
                    t["updated_ms"] = now_ms
                    self._cancel(conn, t, "EXPIRED")
                    self._store(conn, t)

    def trades(self, limit=500):
        with closing(self.connect()) as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM paper_trades ORDER BY updated_ms DESC LIMIT ?", (limit,))]

    def claim_scan_tick(self, now, interval, manual=False):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM paper_meta WHERE key='scan_start'").fetchone()
            if row and not manual and 0 <= now - float(row[0]) < interval - .5:
                return False
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('scan_start',?)", (str(now),))
            return True

    def save_scan_report(self, report):
        summary = {k: v for k, v in report.items() if k not in ("analyses", "display_monotonic", "display_server_age")}
        summary["analyses"] = []
        summary["worker_analyses"] = [{"Pair":d["snapshot"].symbol, "Mode":d["snapshot"].mode,
            "Arah":d["result"].direction, "Status":d["status"], "Paper":d["paper"], "Telegram":d["telegram"]}
            for d in report.get("analyses", [])]
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO paper_meta VALUES ('scan_report',?)", (json.dumps(summary, allow_nan=False),))

    def last_scan_report(self):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='scan_report'").fetchone()
            return json.loads(row[0]) if row else None

    def queue(self, data, monotonic):
        if data["result"].meta.get("scope") != "PAPER" or not eligible(data, monotonic):
            return False, "Tidak ada sinyal PAPER yang valid dan segar"
        p, now_ms = data["result"].plan, data["snapshot"].now_ms
        signal = candle_key(p)
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM paper_trades WHERE signal=?", (signal,)).fetchone():
                return False, "Sinyal candle ini sudah tercatat"
            if conn.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND status IN ('PENDING','OPEN')", (p["symbol"],)).fetchone():
                return False, "Pair sudah memiliki posisi/pending paper"
            s = self._account(conn, now_ms)
            if (s["active"] >= 2 or s["equity"] <= 0 or p["risk"] > s["equity"] * .005 + 1e-8 or
                s["total_risk"] + p["risk"] > s["equity"] * .01 + 1e-8 or
                (p["symbol"] != "BTCUSDT" and s["alt_risk"] + p["risk"] > s["equity"] * .005 + 1e-8) or
                p["margin"] > s["equity"] * .2 + 1e-8 or p["margin"] + p["qty"] * p["cost_sl_per_unit"] > s["free_margin"] + 1e-8 or
                s["losses"] >= 3 or s["equity"] <= s["day_start"] * .98):
                return False, "Batas portofolio berubah; paper trade tidak dibuka"
            t = {"signal": signal, "plan": p, "status": "PENDING", "created_ms": now_ms, "updated_ms": now_ms,
                 "last_quote_ms": now_ms, "remaining_qty": 0.0, "realized_pnl": 0.0, "mark": data["snapshot"].reference_price,
                 "triggered": False, "stop": p["sl"], "tp1_ms": None, "tp2_ms": None, "seen_one_r": False,
                 "quality": "POLLING_SIMULATION", "note": "Menunggu trigger; belum dianggap fill", "telegram": "Belum dicoba",
                 "ai": data.get("ai")}
            self._store(conn, t)
            self._event(conn, t, "SIGNAL", note=t["note"])
        return True, signal

    def telegram_status(self, signal, text):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM paper_trades WHERE signal=?", (signal,)).fetchone()
            if row:
                t = json.loads(row[0]); t["telegram"] = text
                self._store(conn, t)

    def _cancel(self, conn, t, reason):
        t.update(status="CANCELLED", note=reason)
        self._event(conn, t, "CANCELLED", note=reason)

    def _exit(self, conn, t, qty, price, reason):
        p = t["plan"]
        sign = 1 if p["direction"] == "LONG" else -1
        qty = min(qty, t["remaining_qty"])
        pnl = qty * (sign * (price - p["entry"]) - price * p["fee"])
        t["realized_pnl"] += pnl
        t["remaining_qty"] = max(0.0, round(t["remaining_qty"] - qty, 12))
        t["note"] = reason
        if t["remaining_qty"] < p["quantity_step"] * .01:
            t.update(status="CLOSED", closed_ms=t["updated_ms"])
        self._event(conn, t, reason, qty, price, pnl, "Fill virtual; fee taker dan slippage model")

    def advance(self, snap, news_ok=True, max_gap_ms=180000):
        reference = snap.reference_price
        spot = snap.instrument_kind == "SPOT_REFERENCE"
        # No event inferred from an old/incomplete quote, including stale depth.
        if not all(0 <= snap.now_ms - snap.quote_times.get(k, 0) <= 30000 for k in snap.quote_keys):
            raise ValueError("Quote pemantauan paper kedaluwarsa")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (snap.last, reference, snap.bid, snap.ask)) or snap.ask < snap.bid:
            raise ValueError("Harga paper tidak valid")
        if not snap.bids or not snap.asks or not all(math.isfinite(float(p)) and math.isfinite(float(q)) and float(p) > 0 and float(q) >= 0
                                                    for p, q in snap.bids + snap.asks):
            raise ValueError("Depth paper tidak valid")
        if frame_problem(snap.execution, TIMEFRAMES[snap.mode][1], snap.now_ms) or frame_problem(snap.bias, TIMEFRAMES[snap.mode][0], snap.now_ms):
            raise ValueError("Candle pemantauan paper belum lengkap")
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for t in self._rows(conn, True):
                p = t["plan"]
                if p["symbol"] != snap.symbol or p["mode"] != snap.mode or snap.now_ms <= t["last_quote_ms"]:
                    continue
                if p.get("instrument_kind", "USD_M_PERPETUAL") != snap.instrument_kind:
                    raise ValueError("Sumber jurnal berbeda; data spot dan perpetual tidak boleh dicampur")
                gap = snap.now_ms - t["last_quote_ms"] > max_gap_ms
                t.update(updated_ms=snap.now_ms, last_quote_ms=snap.now_ms, mark=reference)
                sign = 1 if p["direction"] == "LONG" else -1
                market_exit = (snap.bid if sign == 1 else snap.ask) - sign * p["slippage_per_unit"]
                if market_exit <= 0:
                    raise ValueError("Harga exit virtual tidak valid")
                if gap:
                    t["quality"] = "DATA_GAP"
                    if t["status"] == "PENDING":
                        self._cancel(conn, t, "DATA_GAP: pending dibatalkan; tidak mengarang fill saat offline")
                    else:
                        self._exit(conn, t, t["remaining_qty"], market_exit, "DATA_GAP_EXIT")
                    self._store(conn, t)
                    continue
                if t["status"] == "PENDING":
                    is_relaxed = bool(p.get("relaxed_mode"))
                    b = indicators(snap.bias)
                    current_atr = float(indicators(snap.execution).atr.iloc[-1])
                    entry_book, exit_book = (snap.asks, snap.bids) if sign == 1 else (snap.bids, snap.asks)
                    entry_quote, exit_quote = (snap.ask, snap.bid) if sign == 1 else (snap.bid, snap.ask)
                    if is_relaxed:
                        # In Relaxed Mode, do not cancel pending Reversal setups on strict Trend-Following EMA alignment
                        # or temporary micro-fluctuations in order-book depth; only require non-empty book & valid ATR.
                        total_book_qty = min(sum(float(q) for _, q in entry_book), sum(float(q) for _, q in exit_book))
                        liquid = total_book_qty >= p["qty"] * 0.1
                        bias_ok = float(b.iloc[-1].adx) >= 10
                        max_spread = 0.25 * current_atr
                    else:
                        liquid = (sum(float(q) for price, q in entry_book if abs(float(price)-entry_quote) <= .10*current_atr) >= p["qty"] and
                                  sum(float(q) for price, q in exit_book if abs(float(price)-exit_quote) <= p["slippage_per_unit"]) >= p["qty"])
                        last, old = b.iloc[-1], b.iloc[-6]
                        bias_ok = (last.close > last.ema50 > last.ema200 and last.ema50 > old.ema50 if sign == 1 else
                                   last.close < last.ema50 < last.ema200 and last.ema50 < old.ema50) and last.adx >= 20
                        max_spread = 0.05 * current_atr
                    if snap.now_ms >= p["expires_ms"]:
                        self._cancel(conn, t, "EXPIRED")
                    elif not news_ok:
                        self._cancel(conn, t, "NEWS_WAIT")
                    elif sign * ((min(snap.last, reference) if sign == 1 else max(snap.last, reference)) - p["sl"]) <= 0:
                        self._cancel(conn, t, "SL_BEFORE_FILL")
                    elif (not bias_ok or not liquid or not math.isfinite(current_atr) or current_atr <= 0 or
                          snap.ask - snap.bid > max_spread or
                          (not spot and not is_relaxed and (abs(reference - snap.last) > .25 * current_atr or
                           snap.funding_rate is None or p["funding_per_unit"] / p["entry"] < max(0, sign * snap.funding_rate)))):
                        self._cancel(conn, t, "FILTER_CHANGED")
                    else:
                        # In Relaxed Mode, also check current bar high/low so triggers reached between 1m/5m scans are not missed!
                        bar_extreme = (snap.open_bar_high if sign == 1 else snap.open_bar_low) if is_relaxed else None
                        if sign * (snap.last - p["entry_trigger"]) >= 0 or (
                            bar_extreme is not None and math.isfinite(bar_extreme) and sign * (bar_extreme - p["entry_trigger"]) >= 0
                        ):
                            t["triggered"] = True
                        quote = snap.ask if sign == 1 else snap.bid
                        book = snap.asks if sign == 1 else snap.bids
                        depth = sum(float(q) for price, q in book if sign * (float(price) - p["entry"]) <= 0 and float(q) >= 0)
                        can_fill = (t["triggered"] and sign * (quote - p["entry"]) <= 0 and depth >= p["qty"]) or (
                            is_relaxed and t["triggered"] and sign * (snap.last - p["sl"]) > 0
                        )
                        if can_fill:
                            p = after_fill(p, p["entry"], p["qty"], snap.now_ms)
                            charge = p["qty"] * (p["entry"] * p["fee"] + p["funding_per_unit"])
                            t.update(plan=p, status="OPEN", remaining_qty=p["qty"], realized_pnl=-charge,
                                     stop=p["sl"], note="Fill PAPER pada limit rencana; fee + cadangan funding dibebankan")
                            self._event(conn, t, "ENTRY", p["qty"], p["entry"], -charge, t["note"])
                if t["status"] == "OPEN":
                    p = t["plan"]
                    if sign * (reference - t["stop"]) <= 0:
                        stop_fill = min(market_exit, t["stop"] - p["slippage_per_unit"]) if sign == 1 else max(market_exit, t["stop"] + p["slippage_per_unit"])
                        self._exit(conn, t, t["remaining_qty"], stop_fill, "STOP_LOSS")
                    else:
                        t["seen_one_r"] |= sign * (snap.last - p["entry"]) >= p["distance"]
                        quote = snap.bid if sign == 1 else snap.ask
                        for target, qty_key, stamp_key in (("tp1", "tp1_qty", "tp1_ms"), ("tp2", "tp2_qty", "tp2_ms")):
                            if t["status"] != "OPEN" or t[stamp_key] is not None or sign * (quote - p[target]) < 0:
                                continue
                            book = snap.bids if sign == 1 else snap.asks
                            depth = sum(float(q) for price, q in book if sign * (float(price) - p[target]) >= 0 and float(q) >= 0)
                            if depth < p[qty_key] or (target == "tp2" and t["tp1_ms"] is None):
                                continue
                            self._exit(conn, t, p[qty_key], p[target] - sign * p["slippage_per_unit"], target.upper())
                            t[stamp_key] = snap.now_ms
                            t["seen_one_r"] = True
                            if target == "tp1":
                                be = quantize(break_even(p["entry"], p["direction"], p["fee"], p["funding_per_unit"], p["slippage_per_unit"]), p["tick"], up=sign == 1)
                                t["stop"] = max(t["stop"], be) if sign == 1 else min(t["stop"], be)
                                if sign * (reference - t["stop"]) <= 0:
                                    self._exit(conn, t, t["remaining_qty"], market_exit, "BREAK_EVEN_EXIT")
                        if t["status"] == "OPEN":
                            advice = manage_position(p, snap.execution, snap.now_ms, reference, t["stop"],
                                t["tp1_ms"], t["tp2_ms"], t["seen_one_r"], p["funding_per_unit"], p["entry"] * p["fee"])
                            t["stop"], t["seen_one_r"] = advice["stop"], advice["reached_one_r"]
                            if advice["action"] in ("EXIT", "REDUCE_OR_EXIT"):
                                self._exit(conn, t, t["remaining_qty"], market_exit, "RULE_EXIT: " + advice["reason"])
                self._store(conn, t)

    def export_json(self):
        with closing(self.connect()) as conn:
            return json.dumps({"schema": 1, "type": "PAPER_SIMULATION", "meta": dict(conn.execute("SELECT key,value FROM paper_meta")),
                "trades": self._rows(conn), "events": [dict(zip(["id", "signal", "at_ms", "event", "qty", "price", "pnl", "note"], r))
                    for r in conn.execute("SELECT * FROM paper_events ORDER BY id")]}, ensure_ascii=False, indent=2, allow_nan=False)

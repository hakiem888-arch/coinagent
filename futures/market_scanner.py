"""Universe -> 10 liquid coins -> 1/2 candidates -> three modes -> AI -> journal/alert."""
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, asdict
from .strategy import evaluate
from .models import TIMEFRAMES
from .screener import shortlist, select_candidates
from .groq_rotation import combine, DEFAULT_MODEL
from .market import MarketError
from .telegram import candle_key
from .process_lock import ProcessLock
from .scanner import ScanBusy, ALLOWED_SCAN_INTERVALS
from . import history


@dataclass(frozen=True)
class ScannerConfig:
    candidates: int = 2
    min_volume: float = 10_000_000
    fee: float = .0005
    slippage_bps: float = 2.0
    funding: float = .0003
    leverage: int = 2
    risk_pct: float = .5
    send_alerts: bool = True
    send_watchlist_alerts: bool = True
    send_scan_summary: bool = True
    relaxed_mode: bool = False
    interval: int = 60

    def account_args(self):
        return {k: getattr(self, k) for k in ("fee", "slippage_bps", "funding", "leverage", "risk_pct")}


def mode_score(result):
    p = result.plan or result.candidate or {}
    return (int(result.status == "VALID"), int(bool(p)),
            float(p.get("net_rr_tp1", 0)) + min(float(result.metrics.get("adx", 0)), 60) / 30 +
            min(float(result.metrics.get("relative_volume", 0)), 3) / 3)


def _effective_news(calendar, now_ms, relaxed_mode=False):
    # Calendar failures are a hard safety hold. Medium-liquidity fallback must never bypass news risk.
    return calendar.window(now_ms)


class MarketScanner:
    def __init__(self, market, pool, calendar, ledger, alerts, model=DEFAULT_MODEL, clock=time.monotonic):
        self.market, self.pool, self.calendar, self.ledger, self.alerts = market, pool, calendar, ledger, alerts
        self.model, self.clock = model, clock
        self.lock, self.last, self.last_start = threading.Lock(), None, float("-inf")
        self.next_symbol = None

    def monitor(self, report, config):
        ok = True
        for trade in self.ledger.active():
            p = trade["plan"]
            try:
                if p.get("instrument_kind", "USD_M_PERPETUAL") != getattr(self.market, "instrument_kind", "USD_M_PERPETUAL"):
                    raise MarketError("Sumber jurnal berbeda. Gunakan jurnal spot terpisah; posisi lama tidak diubah dengan harga spot.")
                snap = self.market.load(p["symbol"], p["mode"])
                news = _effective_news(self.calendar, snap.now_ms, config.relaxed_mode)
                self.ledger.advance(snap, news_ok=news["ok"], max_gap_ms=max(180000, config.interval * 3000))
                report["now_ms"] = max(report["now_ms"], snap.now_ms)
                report["monitored"] += 1
                # Check if pending trade was cancelled on this tick -> send Alert 4 (SIGNAL CANCELLED)
                all_trades = {t["signal"]: t for t in self.ledger.trades(50)}
                updated_trade = all_trades.get(trade["signal"])
                if (updated_trade and trade["status"] == "PENDING" and updated_trade["status"] == "CANCELLED"
                        and config.send_alerts and hasattr(self.alerts, "send_cancellation")):
                    self.alerts.send_cancellation(p["symbol"], p.get("id", trade["signal"]), updated_trade.get("note", "CANCELLED"))
                # Retry explicit rejections/rate limits only, never uncertain sends.
                current = next((t for t in self.ledger.active() if t["signal"] == trade["signal"]), None)
                if current and current["status"] == "PENDING" and config.send_alerts and any(
                    word in current["telegram"] for word in ("Belum dicoba", "rate limit", "cooldown", "belum lengkap", "menolak", "dinonaktifkan")):
                    account = self.ledger.account(snap.now_ms, news, fee=p["fee"],
                        slippage_bps=p["slippage_per_unit"] / p["entry"] * 10000,
                        funding=p["funding_per_unit"] / p["entry"], leverage=p["leverage"],
                        risk_pct=p["risk_budget"] / p["equity"] * 100, exclude_signal=current["signal"])
                    result = evaluate(snap, account, paper=True, relaxed=False)
                    if result.plan and result.plan["id"] == p["id"]:
                        data = {"result": result, "snapshot": snap, "ai": current["ai"],
                                "status": combine(result, current["ai"]), "monotonic": self.clock()}
                        self.ledger.telegram_status(current["signal"], self.alerts.send(data))
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, MarketError) and exc.access_blocked:
                    raise
                ok = False
                report["errors"].append(p["symbol"] + " monitoring WAIT: " + self.pool.redact(str(exc))[:200])
        return ok

    def run(self, config, manual=False):
        if config.candidates not in (1, 2) or config.interval not in ALLOWED_SCAN_INTERVALS:
            raise ValueError("Konfigurasi scanner tidak valid")
        if not self.lock.acquire(blocking=False):
            return dict(self.last or {}, scan_notice="Scan lain sedang berjalan; percobaan baru belum dimulai.")
        process_lock, started = ProcessLock(self.ledger.db), False
        try:
            try:
                process_lock.acquire()
            except ScanBusy:
                return dict(self.ledger.last_scan_report() or {},
                            scan_notice="Scanner/worker lain sedang berjalan; percobaan baru belum dimulai.")
            # A persistent access restriction needs an explicit retry after the
            # hosting/network issue is addressed, not repeated automated traffic.
            if self.last and self.last.get("market_error", {}).get("access_blocked") and not manual:
                return self.last
            # Shared resource: extra browser tabs/reruns do not multiply scans.
            if self.last and not manual and self.clock() - self.last_start < config.interval - .5:
                return self.last
            if not self.ledger.claim_scan_tick(time.time(), config.interval, manual):
                return self.ledger.last_scan_report() or {"errors": ["Menunggu interval scan bersama berikutnya"]}
            self.last_start = self.clock()
            started = True
            report = {"scan_id": uuid4().hex[:12], "started_at_ms": int(time.time() * 1000),
                      "instrument_kind": getattr(self.market, "instrument_kind", "USD_M_PERPETUAL"),
                      "scan_status": "RUNNING", "trigger": "MANUAL" if manual else "AUTO",
                      "now_ms": 0, "universe_loaded": False, "market_error": {},
                      "universe_count": 0, "eligible_count": 0, "shortlist": [],
                      "long_watchlist": [], "short_watchlist": [],
                      "long_watchlist_count": 0, "short_watchlist_count": 0,
                      "selected": [], "ranked": [], "analyses": [], "errors": [], "monitored": 0,
                      "news": {"ok": False, "reason": "Belum diperiksa"}, "config": asdict(config)}
            self.last = report
            try:
                now = int(self.market.get(getattr(self.market, "time_path", "/fapi/v1/time"))["serverTime"])
                report["now_ms"] = now
                before_active = {t["signal"]: t for t in self.ledger.active() if t["status"] == "PENDING"}
                self.ledger.expire_pending(now)
                if before_active and config.send_alerts and hasattr(self.alerts, "send_cancellation"):
                    after_active = {t["signal"] for t in self.ledger.active()}
                    for sig, old_t in before_active.items():
                        if sig not in after_active:
                            p_old = old_t["plan"]
                            self.alerts.send_cancellation(p_old["symbol"], p_old.get("id", sig), "EXPIRED (Melewati batas waktu 3 candle eksekusi)")
                monitor_ok = self.monitor(report, config)
                universe = self.market.universe()
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["market_provider"] = getattr(self.market, "market_label", "Unknown")
                if getattr(self.market, "failover_reason", None):
                    report["provider_failover"] = self.market.failover_reason
                now = universe["now_ms"]
                report["now_ms"] = max(now, report["now_ms"])
                screened = shortlist(universe, config.min_volume, limit=20, allow_medium_liquidity_fallback=config.relaxed_mode)
                report.update(
                    universe_loaded=True,
                    universe_count=screened["universe_count"],
                    eligible_count=screened["eligible_count"],
                    shortlist=screened["rows"],
                    long_watchlist=screened.get("long_watchlist", []),
                    short_watchlist=screened.get("short_watchlist", []),
                    long_watchlist_count=len(screened.get("long_watchlist", [])),
                    short_watchlist_count=len(screened.get("short_watchlist", [])),
                )
                report["news"] = _effective_news(self.calendar, now, config.relaxed_mode)
                if not monitor_ok:
                    report["errors"].append("Sinyal baru ditahan karena posisi paper belum mendapat data segar")
                    return report
                if not report["news"]["ok"]:
                    report["errors"].append(report["news"]["reason"])
                    return report
                active = self.ledger.active()
                if len(active) >= 2:
                    report["errors"].append("Dua posisi/pending paper aktif; tetap dipantau, sinyal baru ditahan")
                    return report
                busy = {t["plan"]["symbol"] for t in active}
                selected, ranked, errors = select_candidates([r for r in screened["rows"] if r["symbol"] not in busy],
                    self.market, now, min(config.candidates, 2 - len(active)))
                report.update(selected=selected, ranked=ranked,
                    long_watchlist=[x for x in ranked if x.get("candidate_direction") == "LONG"],
                    short_watchlist=[x for x in ranked if x.get("candidate_direction") == "SHORT"],
                    long_watchlist_count=sum(x.get("candidate_direction") == "LONG" for x in ranked),
                    short_watchlist_count=sum(x.get("candidate_direction") == "SHORT" for x in ranked))
                report["errors"].extend(errors)
                processing_order = sorted(selected, key=lambda r: r["symbol"] != self.next_symbol)
                for row in processing_order:
                    # Bound batches; slow remote APIs never create a catch-up queue.
                    if self.clock() - self.last_start > 60:
                        self.next_symbol = row["symbol"]
                        report["errors"].append("Anggaran waktu siklus tercapai; kandidat tersisa menunggu siklus berikutnya")
                        break
                    try:
                        if self.next_symbol == row["symbol"]:
                            self.next_symbol = None
                        self._analyse(row["symbol"], config, report, candidate_info=row)
                    except (MarketError, ValueError, KeyError, TypeError) as exc:
                        if isinstance(exc, MarketError) and exc.access_blocked:
                            raise
                        report["errors"].append(row["symbol"] + " WAIT: " + self.pool.redact(str(exc))[:300])
                if config.send_scan_summary and hasattr(self.alerts, "send_summary"):
                    report["summary_telegram"] = self.alerts.send_summary(report)
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                report["scan_status"] = "FAILED"
                if isinstance(exc, MarketError):
                    report["market_error"] = exc.diagnostic()
                report["errors"].append("WAIT: " + self.pool.redact(str(exc))[:300])
            return report
        except Exception:
            if started:
                self.last["scan_status"] = "FAILED"
                self.last["errors"].append("Siklus terhenti karena kesalahan internal; periksa log aplikasi.")
            raise
        finally:
            try:
                if self.last is not None and started:
                    self.last["completed_at_ms"] = int(time.time() * 1000)
                    self.last["duration_seconds"] = round(self.clock() - self.last_start, 2)
                    if self.last["scan_status"] == "RUNNING":
                        self.last["scan_status"] = "COMPLETED_WITH_NOTES" if self.last["errors"] else "COMPLETED"
                    self.ledger.save_scan_report(self.last)
            finally:
                process_lock.release()
                self.lock.release()

    def _analyse(self, symbol, config, report, candidate_info=None):
        snapshots = self.market.load_modes(symbol, ("SCALPING",))
        results = []
        for mode in ("SCALPING",):
            snap = snapshots[mode]
            news_win = _effective_news(self.calendar, snap.now_ms, config.relaxed_mode)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            results.append(evaluate(snap, account, paper=True, relaxed=False))
        # Prefer valid setups, then risk-adjusted target room/trend/activity.
        # This score is a ranking heuristic, never confidence or win probability.
        chosen = max(results, key=mode_score)
        comparison = [{"mode": r.meta["mode"], "direction": r.direction, "status": r.status,
                       "score": round(mode_score(r)[2], 4), "reasons": r.reasons[:5], "metrics": r.metrics}
                      for r in results]
        chosen.meta["mode_comparison"] = comparison
        if candidate_info:
            chosen.meta["stage2_screening"] = {
                "m15_score": candidate_info.get("m15_score"),
                "category": candidate_info.get("category"),
                "candidate_direction": candidate_info.get("candidate_direction"),
                "m15_summary": candidate_info.get("m15_summary"),
            }
        mode = chosen.meta["mode"]
        snap = snapshots[mode]
        acquired = self.clock()
        ai = self.pool.review(chosen, self.model)
        if combine(chosen, ai) == "VALID":
            reviewed_id = chosen.plan["id"]
            # Refresh portfolio marks too: candidate 2 cannot reuse candidate 1's budget.
            if not self.monitor(report, config):
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Portofolio belum terverifikasi ulang"], "cautions": []}
            snap = self.market.load(symbol, mode)
            acquired = self.clock()
            news_win = _effective_news(self.calendar, snap.now_ms, config.relaxed_mode)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            chosen = evaluate(snap, account, paper=True, relaxed=False)
            chosen.meta["mode_comparison"] = comparison
            if not chosen.plan:
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Harga, setup, atau anggaran berubah setelah AI"], "cautions": []}
            elif chosen.plan["id"] != reviewed_id:
                ai = {"ok": False, "decision": "WAIT", "reasons": ["Harga, setup, atau anggaran berubah setelah AI"], "cautions": []}
        final_status = combine(chosen, ai)
        stage_status = "ENTRY VALID" if final_status == "VALID" else (
            "WAITING FOR CONFIRMATION" if (candidate_info and candidate_info.get("m15_score", 0) >= 70) else (
                "WATCHLIST" if final_status == "WAIT" else "REJECTED"
            )
        )
        data = {"snapshot": snap, "result": chosen, "ai": ai, "status": final_status,
                "stage_status": stage_status, "candidate_info": candidate_info or {},
                "monotonic": acquired, "paper": "Tidak dicatat: WAIT/NO TRADE", "telegram": "Tidak dikirim"}
        report["now_ms"] = max(report["now_ms"], snap.now_ms)
        if data["status"] == "VALID":
            queued, reason = self.ledger.queue(data, self.clock)
            if queued:
                data["paper"] = "PENDING · " + reason
                data["telegram"] = self.alerts.send(data) if config.send_alerts else "Alert Telegram dinonaktifkan."
                self.ledger.telegram_status(reason, data["telegram"])
            else:
                data["paper"] = reason
                data["status"] = "WAIT"
                data["stage_status"] = "WAITING FOR CONFIRMATION"
        elif (config.send_watchlist_alerts and hasattr(self.alerts, "send_watchlist")
              and candidate_info and candidate_info.get("m15_score", 0) >= 70):
            data["telegram"] = self.alerts.send_watchlist(data, candidate_info)
        history.save(self.ledger.db, chosen, data["status"], ai)
        report["analyses"].append(data)

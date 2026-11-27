"""Universe -> 30 liquid coins -> 5 rotating candidates -> Hybrid Adaptive Strategy F (M15 Regime -> M5 Trigger A–E) -> Advisor-Only AI -> journal/alert.

BUG-09, BUG-12, BUG-15, BUG-16, BUG-18, BUG-36..BUG-53 + P0-1..P0-7 Mandatory Remediation:
- Integrates `evaluate_hybrid_adaptive()` (`Strategy F`) from `futures.hybrid_adaptive` as the sole production
  evaluator (`MarketScanner._analyse()` and `MarketScanner.monitor()`) with NO silent fallback to legacy engine (`P0-1`).
- Uses `M15` for regime classification (`detect_m15_regime`) and `M5` for trigger routing (`A`–`E`), storing
  `strategy_id` and `regime` (`m15_regime`) on every scan result (`P0-1`).
- Enforces Groq strictly as a non-blocking contextual Advisor (`AI_ADVISORY_OK`, `AI_CAUTION`, `AI_UNAVAILABLE`)
  where Rule Engine (`Strategy F`) is the sole determinant of technical & risk validity (`P0-2`).
- Sets `BALANCED` as explicit strategy profile and synchronizes `effective_strategy_profile` across Admin, Viewer,
  and Worker (`P0-3`).
- Enforces `>= 250` closed M15 and M5 candles (`limit=260`) and surfaces `HISTORY_INSUFFICIENT` separately (`P0-4`).
- Populates both the 19-metric `diagnostic_panel` and the full P0-7 `scanner_diagnostics` (including
  `m15_regime_distribution`, `strategy_candidate_counts`, `m5_trigger_count`, `setup_quality_scores`,
  `hard_gate_rejection_reasons`, `groq_status_counts`, `telegram_status_counts`, `worker_heartbeat`,
  and `persistence_status`) on every scan cycle (`P0-7`).
"""
import inspect
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, asdict
from .strategy import (
    calibrate_confidence_from_paper_trades,
    classify_result_rejection_category,
    resolve_strategy_profile,
    spot_reference_validation_metadata,
)
from .hybrid_adaptive import (
    ALL_REGIMES,
    ArmingStateStore,
    DEFAULT_MIN_CANDLES_M15,
    DEFAULT_MIN_CANDLES_M5,
    STRATEGY_A,
    STRATEGY_B,
    STRATEGY_C,
    STRATEGY_D,
    STRATEGY_E,
    empty_hybrid_funnel,
    evaluate_hybrid_adaptive,
)
from .screener import shortlist, select_candidates, empty_rejection_breakdown
from .groq_rotation import combine, evaluate_contextual_ai, DEFAULT_MODEL
from .market import MarketError
from .paper import PaperLedger, DEFAULT_MAX_GAP_MS
from .spot_market import resolve_db_path
from .security import SecurityManager, GLOBAL_MONITOR
from .process_lock import ProcessLock
from .scanner import ScanBusy, ALLOWED_SCAN_INTERVALS
from .telegram import classify_watchlist_stage, format_predictive_alert
from .predictive_watchlist import (
    PredictiveConfig,
    PredictiveLifecycleManager,
    STAGE_CONFIRMED,
    STAGE_DIRECTION_CHANGED,
    STAGE_EARLY_WATCH,
    STAGE_MISSED_ENTRY,
    STAGE_PRE_WATCH,
    STAGE_SETUP_FORMING,
    STAGE_SIGNAL_INVALIDATED,
    evaluate_early_signal,
)
from .predictive_store import PredictiveSignalRepository, PredictiveStoreError
from .intrabar_stream import IntrabarStreamManager
from .memory_profiler import BoundedLRUDict, profile_scanner_memory
from .db_backend import database_display_identity, database_identity_fingerprint, verify_repository_read_write
from . import history


def empty_diagnostic_panel():
    """Return the Mandatory 19-Metric Diagnostic Panel dictionary initialized to zero."""
    return {
        "universe_total": 0,
        "lolos_liquidity": 0,
        "lolos_spread": 0,
        "lolos_freshness": 0,
        "lolos_candle_history": 0,
        "m15_watch": 0,
        "m15_priority": 0,
        "m5_analyzed": 0,
        "rule_based_valid": 0,
        "ai_confirmed": 0,
        "ai_unavailable": 0,
        "rejected_by_confidence": 0,
        "rejected_by_news": 0,
        "rejected_by_cost": 0,
        "rejected_by_risk": 0,
        "rejected_by_position_limit": 0,
        "telegram_sent": 0,
        "telegram_blocked": 0,
        "last_scan_error": "",
    }


def empty_p0_scanner_diagnostics():
    """P0-7: Mandatory detailed scanner diagnostics recorded on every scan cycle."""
    return {
        "universe_total": 0,
        "lolos_stage_1": 0,
        "stage_1_rejection_reasons": empty_rejection_breakdown(),
        "lolos_candle_history": 0,
        "rejected_by_candle_history": 0,
        "history_insufficient_symbols": [],
        "m15_regime_distribution": {reg: 0 for reg in ALL_REGIMES},
        "strategy_candidate_counts": {k: 0 for k in (STRATEGY_A, STRATEGY_B, STRATEGY_C, STRATEGY_D, STRATEGY_E)},
        "m5_trigger_count": 0,
        "setup_quality_scores": [],
        "hard_gate_rejection_reasons": {},
        "groq_status_counts": {
            "AI_ADVISORY_OK": 0,
            "AI_CAUTION": 0,
            "AI_UNAVAILABLE": 0,
        },
        "telegram_status_counts": {
            "sent": 0,
            "blocked": 0,
            "skipped": 0,
        },
        "worker_heartbeat": {},
        "persistence_status": {},
        "pipeline_reconciliation": {
            "universe_total": 0,
            "stage1_passed": 0,
            "stage1_rejected": 0,
            "stage2_shortlist": 0,
            "queued_count": 0,
            "analyzed_count": 0,
            "deferred_count": 0,
            "expired_count": 0,
            "rejected_count": 0,
            "paper_entry_count": 0,
        },
        "queue_age_ms": [],
        "candidate_processing_latency_ms": [],
        "provider_failover": "",
        "telemetry": {},
    }


@dataclass(frozen=True)
class ScannerConfig:
    candidates: int = 2
    shortlist_limit: int = 50
    max_groq_candidates: int = 5
    max_queue_age_ms: int = 900_000
    futures_primary_only: bool = False
    min_m15_deep_score: int = 55
    min_volume: float = 10_000_000
    fee: float = .0005
    slippage_bps: float = 2.0
    funding: float = .0003
    leverage: int = 2
    risk_pct: float = .25
    send_alerts: bool = True
    send_watchlist_alerts: bool = True
    send_scan_summary: bool = True
    relaxed_mode: bool = False
    strategy_profile: str = "BALANCED"
    strategy_engine: str = "HYBRID_ADAPTIVE_V1"
    allow_paper_on_ai_unavailable: bool = True
    advisor_only_ai: bool = True
    adaptive_history: bool = True
    min_m15_candles: int = DEFAULT_MIN_CANDLES_M15
    min_m5_candles: int = DEFAULT_MIN_CANDLES_M5
    interval: int = 60
    max_gap_ms: int = DEFAULT_MAX_GAP_MS
    market_fetch_budget_s: float = 30.0
    screening_budget_s: float = 45.0
    ai_budget_s: float = 30.0
    per_candidate_timeout_s: float = 25.0
    total_cycle_budget_s: float = 60.0

    def account_args(self):
        return {k: getattr(self, k) for k in ("fee", "slippage_bps", "funding", "leverage", "risk_pct")}



def reconcile_candidate_pipeline(
    *,
    universe_total: int = 0,
    stage1_passed: int = 0,
    stage1_rejected: int = 0,
    stage2_total: int = 0,
    active_queue_count: int = 0,
    analyzed_count: int = 0,
    deferred_count: int = 0,
    expired_count: int = 0,
    rejected_count: int = 0,
    cancelled_count: int = 0,
    paper_entry_count: int = 0,
    candidate_states: list = None,
) -> dict:
    """P0-01: Mutually-exclusive single-source candidate state machine reconciliation.
    States: ACTIVE_QUEUE, ANALYZED, DEFERRED, EXPIRED, REJECTED, CANCELLED.
    - Zero double counting: a DEFERRED candidate is NOT also counted in ACTIVE_QUEUE.
    - `accounted_total = active_queue + analyzed + deferred + expired + rejected + cancelled == stage2_total`.
    """
    s2 = max(0, int(stage2_total))
    aq = max(0, int(active_queue_count))
    an = max(0, int(analyzed_count))
    df = max(0, int(deferred_count))
    ex = max(0, int(expired_count))
    rj = max(0, int(rejected_count))
    cn = max(0, int(cancelled_count))
    accounted = aq + an + df + ex + rj + cn
    ok = (accounted == s2)
    return {
        "universe_total": max(0, int(universe_total)),
        "stage1_passed": max(0, int(stage1_passed)),
        "stage1_rejected": max(0, int(stage1_rejected)),
        "stage2_total": s2,
        "stage2_shortlist": s2,
        "active_queue_count": aq,
        "queued_count": df,
        "analyzed_count": an,
        "deferred_count": df,
        "expired_count": ex,
        "rejected_count": rj,
        "cancelled_count": cn,
        "paper_entry_count": max(0, int(paper_entry_count)),
        "accounted_total": accounted,
        "reconciliation_ok": ok,
        "reconciliation_status": "OK" if ok else "RECONCILIATION_ERROR",
        "candidate_states": list(candidate_states or []),
    }


def mode_score(result):
    p = result.plan or result.candidate or {}
    return (int(result.status == "VALID"), int(bool(p)),
            float(p.get("net_rr_tp1", 0)) + min(float(result.metrics.get("adx", 0)), 60) / 30 +
            min(float(result.metrics.get("relative_volume", 0)), 3) / 3)


class MarketScanner:
    def __init__(self, market, pool, calendar, ledger, alerts, model=DEFAULT_MODEL, clock=time.monotonic, security=None, trusted_worker=True):
        self.market, self.pool, self.calendar, self.alerts = market, pool, calendar, alerts
        self.model, self.clock = model, clock
        self.lock, self.last, self.last_start = threading.Lock(), None, float("-inf")
        self.next_symbol = None
        self.pending_candidate_queue = []
        self.rotating_candidate_queue = []
        self.arming_store = ArmingStateStore()
        # BUG-53: Candle-close caches so M15, M5, and AI reviews are never repeated on the same closed candle
        self.m15_eval_cache = BoundedLRUDict(max_entries=256)
        self.m5_eval_cache = BoundedLRUDict(max_entries=256)
        self.ai_review_cache = BoundedLRUDict(max_entries=256)
        self.instance_id = f"scanner-{uuid4().hex[:8]}"
        self._base_db = ledger.db
        self._initial_equity = ledger.initial_equity
        self._pg_url = getattr(ledger, "pg_url", None)
        self._secret_getter = getattr(ledger, "secret_getter", None)
        initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
        self.ledgers = {initial_kind: ledger}
        self.ledger = ledger
        self.lock_owner_ledger = None
        self.lock_owner_path = None
        self.lock_owner_process_lock = None
        self.lock_owner_db_acquired = False
        self.security = security or SecurityManager(self._base_db, pg_url=self._pg_url, secret_getter=self._secret_getter)
        self.trusted_worker = bool(trusted_worker)
        self.worker_session_token = self.security.issue_worker_session() if self.trusted_worker else None
        if hasattr(self.pool, "security"):
            self.pool.security = self.security
        if hasattr(self.alerts, "security"):
            self.alerts.security = self.security
        self.predictive_config = PredictiveConfig.from_scanner_json()
        self.predictive_lifecycle = PredictiveLifecycleManager(self.predictive_config)
        self.predictive_store_status = "OK"
        self.predictive_store_error = ""
        self.predictive_monitor_health = "HEALTHY"
        try:
            self.predictive_store = PredictiveSignalRepository(
                self._base_db,
                pg_url=self._pg_url,
                secret_getter=self._secret_getter,
            )
        except Exception as ps_exc:
            from .telegram import redact_telegram_secrets as _redact_ps
            self.predictive_store = None
            self.predictive_store_status = "PREDICTIVE_STORE_UNAVAILABLE"
            self.predictive_store_error = _redact_ps(f"{type(ps_exc).__name__}: {ps_exc}")[:220]
            self.predictive_monitor_health = "DEGRADED" 
        self.intrabar_stream = IntrabarStreamManager(
            self.market,
            websocket_enabled=self.predictive_config.websocket_enabled,
            rest_fallback_enabled=self.predictive_config.rest_fallback_enabled,
            max_subscriptions=self.predictive_config.early_monitor_max_candidates,
            stale_threshold_seconds=self.predictive_config.stale_market_data_seconds,
            monitor_interval_seconds=self.predictive_config.monitor_interval_seconds,
        )
        self.last_predictive_diagnostics = {}
        self._cycle_groq_request_time_s = 0.0
        self._cycle_advisor_processing_time_s = 0.0
        self._cycle_persistence_time_s = 0.0

    def _effective_token(self, session_token=None):
        if session_token is not None:
            return session_token
        return self.worker_session_token if self.trusted_worker else None

    @staticmethod
    def _record_telegram_outcome(report, text):
        msg = str(text or "")
        panel = report.get("diagnostic_panel")
        p0_diag = report.get("scanner_diagnostics")
        sent = "terkirim" in msg.lower() or "dikirim" in msg.lower()
        blocked = any(w in msg.lower() for w in ("diblokir", "duplikat", "rate limit", "cooldown", "menolak", "unauthorized", "belum lengkap"))
        if isinstance(panel, dict):
            if sent:
                panel["telegram_sent"] = int(panel.get("telegram_sent", 0)) + 1
            elif blocked:
                panel["telegram_blocked"] = int(panel.get("telegram_blocked", 0)) + 1
        if isinstance(p0_diag, dict):
            tg_counts = p0_diag.setdefault("telegram_status_counts", {"sent": 0, "blocked": 0, "skipped": 0})
            if sent:
                tg_counts["sent"] = int(tg_counts.get("sent", 0)) + 1
            elif blocked:
                tg_counts["blocked"] = int(tg_counts.get("blocked", 0)) + 1
            else:
                tg_counts["skipped"] = int(tg_counts.get("skipped", 0)) + 1

    def _call_alert(self, method_name, *args, session_token=None, report=None, **extra_kwargs):
        fn = getattr(self.alerts, method_name, None)
        if fn is None:
            return "Metode Telegram tidak tersedia"
        eff_token = self._effective_token(session_token)
        if hasattr(self.alerts, "session_token"):
            self.alerts.session_token = eff_token
        params = inspect.signature(fn).parameters
        kwargs = {}
        for k, v in extra_kwargs.items():
            if k in params:
                kwargs[k] = v
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        out = fn(*args, **kwargs)
        if isinstance(report, dict):
            self._record_telegram_outcome(report, out)
        return out

    def _call_ai_review(self, chosen, session_token=None, cache_key=None):
        """BUG-50, BUG-53 & P0-2: Execute or reuse cached contextual AI review per `(symbol, timeframe, candle_close_ms)`."""
        pool_decision_override = getattr(self.pool, "decision", None)
        if cache_key is not None and cache_key in self.ai_review_cache:
            cached_entry = self.ai_review_cache[cache_key]
            if cached_entry.get("_pool_decision") == pool_decision_override and cached_entry.get("ok"):
                out = dict(cached_entry)
                out.pop("_pool_decision", None)
                out["cached"] = True
                return out

        eff_token = self._effective_token(session_token)
        if hasattr(self.pool, "session_token"):
            self.pool.session_token = eff_token
        params = inspect.signature(self.pool.review).parameters
        kwargs = {}
        if "security" in params:
            kwargs["security"] = self.security
        if "session_token" in params:
            kwargs["session_token"] = eff_token
        GLOBAL_MONITOR.increment("groq_ai_requests")
        _t_groq_net = self.clock()
        res = dict(self.pool.review(chosen, self.model, **kwargs) or {})
        self._cycle_groq_request_time_s += max(0.0, self.clock() - _t_groq_net)
        if "ai_status" not in res:
            if not res.get("ok"):
                res["ai_status"] = "AI_UNAVAILABLE"
            elif res.get("decision") == chosen.direction:
                res["ai_status"] = "AI_ADVISORY_OK"
            else:
                res["ai_status"] = "AI_CAUTION"
        res["cached"] = False
        if cache_key is not None and res.get("ok"):
            to_store = dict(res, _pool_decision=pool_decision_override)
            self.ai_review_cache[cache_key] = to_store
        return res

    def ledger_for(self, instrument_kind):
        """BUG-09: Return the isolated PaperLedger for the given instrument_kind."""
        kind = instrument_kind or "USD_M_PERPETUAL"
        if kind not in self.ledgers:
            root = self._base_db.parent.parent if self._base_db.parent.name == "data" else self._base_db.parent
            target_db = resolve_db_path(root, kind, base_db=self._base_db)
            self.ledgers[kind] = PaperLedger(
                target_db,
                self._initial_equity,
                expected_instrument_kind=kind,
                pg_url=self._pg_url,
                secret_getter=self._secret_getter,
            )
        return self.ledgers[kind]

    def _acquire_target_locks(self, target_ledger, now_ms=None):
        """BUG-29: Atomically acquire OS ProcessLock and DB scanner lease lock for target_ledger."""
        proc_lock = ProcessLock(target_ledger.db)
        proc_lock.acquire()
        db_acquired = False
        if hasattr(target_ledger, "acquire_db_scanner_lock"):
            stamp_ms = int(now_ms if now_ms is not None else int(time.time() * 1000))
            if not target_ledger.acquire_db_scanner_lock(self.instance_id, stamp_ms):
                proc_lock.release()
                raise ScanBusy(f"Scanner/worker lain sedang memegang lock database pada {target_ledger.db.name}; percobaan baru belum dimulai.")
            db_acquired = True
        return proc_lock, db_acquired

    def _release_owned_locks(self):
        """BUG-29: Release scanner locks using explicit lock_owner_ledger and lock_owner_path references."""
        owned_ledger = self.lock_owner_ledger
        owned_proc = self.lock_owner_process_lock
        owned_db_acq = self.lock_owner_db_acquired
        self.lock_owner_ledger = None
        self.lock_owner_path = None
        self.lock_owner_process_lock = None
        self.lock_owner_db_acquired = False
        if owned_db_acq and owned_ledger is not None and hasattr(owned_ledger, "release_db_scanner_lock"):
            try:
                owned_ledger.release_db_scanner_lock(self.instance_id)
            except Exception:
                pass
        if owned_proc is not None:
            try:
                owned_proc.release()
            except Exception:
                pass

    def _sync_active_ledger(self, now_ms=None):
        """BUG-09 & BUG-29: Ensure self.ledger matches active market provider and atomically migrate scanner locks on failover."""
        current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
        target = self.ledger_for(current_kind)
        if self.ledger is not target:
            if self.lock_owner_ledger is not None and self.lock_owner_ledger is not target:
                new_proc, new_db_acq = self._acquire_target_locks(target, now_ms=now_ms)
                self._release_owned_locks()
                self.lock_owner_ledger = target
                self.lock_owner_path = target.db
                self.lock_owner_process_lock = new_proc
                self.lock_owner_db_acquired = new_db_acq
            if hasattr(self.alerts, "ledger") and self.alerts.ledger is self.ledger:
                self.alerts.ledger = target
            if hasattr(self.alerts, "db"):
                self.alerts.db = target.db
            self.ledger = target
        return self.ledger

    def _safe_calendar_window(self, now_ms):
        """BUG-42: Fetch news calendar window safely without aborting technical scan on News API errors."""
        try:
            win = self.calendar.window(now_ms)
            if not isinstance(win, dict):
                return {"ok": False, "reason": "NEWS_UNAVAILABLE: Format respons kalender berita tidak valid", "status": "NEWS_UNAVAILABLE"}
            return win
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"NEWS_UNAVAILABLE: Gagal memuat kalender berita ({type(exc).__name__})",
                "status": "NEWS_UNAVAILABLE",
            }

    def monitor(self, report, config, session_token=None):
        ok = True
        self._sync_active_ledger()
        active_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
        active_trades = list(self.ledger.active())
        for trade in active_trades:
            p = trade["plan"]
            trade_kind = p.get("instrument_kind", "USD_M_PERPETUAL")
            try:
                if trade_kind != active_kind:
                    report["errors"].append(
                        f"{p['symbol']} tetap terlindungi di jurnal {trade_kind}; tidak diproses dengan harga {active_kind}."
                    )
                    continue
                snap = self.market.load(p["symbol"], p["mode"])
                if snap.instrument_kind != trade_kind:
                    self._sync_active_ledger()
                    report["instrument_kind"] = snap.instrument_kind
                    report["market_provider"] = getattr(self.market, "market_label", snap.instrument_kind)
                    if getattr(self.market, "failover_reason", None):
                        report["provider_failover"] = self.market.failover_reason
                    report["errors"].append(
                        f"Failover terjadi saat memantau {p['symbol']}: posisi {trade_kind} tetap di jurnal asalnya dan tidak ditutup/diubah dengan harga {snap.instrument_kind}."
                    )
                    continue
                news = self._safe_calendar_window(snap.now_ms)
                gap_limit = int(getattr(config, "max_gap_ms", DEFAULT_MAX_GAP_MS))
                self.ledger.advance(snap, news_ok=news["ok"], max_gap_ms=gap_limit)
                report["now_ms"] = max(report["now_ms"], snap.now_ms)
                report["monitored"] += 1
                all_trades = {t["signal"]: t for t in self.ledger.trades(50)}
                updated_trade = all_trades.get(trade["signal"])
                if updated_trade and updated_trade.get("quality") == "DATA_GAP":
                    report["data_status"] = "DATA_GAP"
                if (updated_trade and trade["status"] == "PENDING" and updated_trade["status"] == "CANCELLED"
                        and config.send_alerts and hasattr(self.alerts, "send_cancellation")):
                    self._call_alert(
                        "send_cancellation",
                        p["symbol"],
                        p.get("id", trade["signal"]),
                        updated_trade.get("note", "CANCELLED"),
                        session_token=session_token,
                        report=report,
                    )
                current = next((t for t in self.ledger.active() if t["signal"] == trade["signal"]), None)
                if current and current["status"] == "PENDING" and config.send_alerts and any(
                    word in current["telegram"] for word in ("Belum dicoba", "rate limit", "cooldown", "belum lengkap", "menolak", "dinonaktifkan")):
                    account = self.ledger.account(
                        snap.now_ms,
                        news,
                        fee=p["fee"],
                        slippage_bps=p["slippage_per_unit"] / p["entry"] * 10000,
                        funding=p["funding_per_unit"] / p["entry"],
                        leverage=p["leverage"],
                        risk_pct=p["risk_budget"] / p["equity"] * 100,
                        exclude_signal=current["signal"],
                    )
                    result = evaluate_hybrid_adaptive(
                        snap,
                        account,
                        paper=True,
                        arming_store=self.arming_store,
                        min_m15_candles=int(getattr(config, "min_m15_candles", DEFAULT_MIN_CANDLES_M15)),
                        min_m5_candles=int(getattr(config, "min_m5_candles", DEFAULT_MIN_CANDLES_M5)),
                        allow_spot_paper_simulation=(snap.instrument_kind == "SPOT_REFERENCE"),
                    )
                    if result.plan and result.plan["id"] == p["id"]:
                        ctx_ai = evaluate_contextual_ai(result, current["ai"], allow_paper_on_ai_unavailable=True)
                        data = {
                            "result": result,
                            "snapshot": snap,
                            "ai": current["ai"],
                            "status": ctx_ai["final_status"],
                            "rule_status": ctx_ai["rule_status"],
                            "ai_status": ctx_ai["ai_status"],
                            "advisory_status": ctx_ai["advisory_status"],
                            "advisor_only": True,
                            "allow_paper_on_ai_unavailable": True,
                            "monotonic": self.clock(),
                        }
                        self.ledger.telegram_status(
                            current["signal"],
                            self._call_alert("send", data, session_token=session_token, report=report),
                        )
            except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                if isinstance(exc, ScanBusy):
                    report["scan_notice"] = str(exc)
                elif isinstance(exc, MarketError):
                    report["data_status"] = "CONNECTION_ERROR"
                    if exc.access_blocked:
                        raise
                elif "STALE_DATA" in str(exc) or "kedaluwarsa" in str(exc):
                    report["data_status"] = "STALE_DATA"
                ok = False
                report["errors"].append(p["symbol"] + " monitoring WAIT: " + self.pool.redact(str(exc))[:200])
        return ok

    def run(self, config, manual=False, session_token=None, worker_id=""):
        if not 1 <= int(config.candidates) <= 50 or config.interval not in ALLOWED_SCAN_INTERVALS:
            raise ValueError("Konfigurasi scanner tidak valid")
        eff_token = self._effective_token(session_token)
        if self.security is not None:
            last_failed = bool(self.last and self.last.get("scan_status") == "FAILED")
            action = "MANUAL_SCAN" if manual else "AUTO_SCAN"
            allowed, reason = self.security.authorize_action(
                action, session_token=eff_token, last_scan_failed=last_failed
            )
            if not allowed:
                return dict(self.last or {}, scan_notice=reason, auth_error=reason)
        if not self.lock.acquire(blocking=False):
            return dict(self.last or {}, scan_notice="Scan lain sedang berjalan; percobaan baru belum dimulai.")
        self._sync_active_ledger()
        started = False
        try:
            try:
                proc_lock, db_acq = self._acquire_target_locks(self.ledger, now_ms=int(time.time() * 1000))
                self.lock_owner_ledger = self.ledger
                self.lock_owner_path = self.ledger.db
                self.lock_owner_process_lock = proc_lock
                self.lock_owner_db_acquired = db_acq
            except ScanBusy as busy_exc:
                return dict(self.ledger.last_scan_report() or {},
                            scan_notice=str(busy_exc) or "Scanner/worker lain sedang berjalan; percobaan baru belum dimulai.")
            if self.last and self.last.get("market_error", {}).get("access_blocked") and not manual:
                return self.last
            if self.last and not manual and self.clock() - self.last_start < config.interval - .5:
                return self.last
            if not self.ledger.claim_scan_tick(time.time(), config.interval, manual):
                return self.ledger.last_scan_report() or {"errors": ["Menunggu interval scan bersama berikutnya"]}
            self.last_start = self.clock()
            self._cycle_groq_request_time_s = 0.0
            self._cycle_advisor_processing_time_s = 0.0
            self._cycle_persistence_time_s = 0.0
            started = True
            started_wall_ms = int(time.time() * 1000)
            if manual:
                GLOBAL_MONITOR.increment("manual_scans")
            else:
                GLOBAL_MONITOR.increment("auto_scans")
            if worker_id:
                GLOBAL_MONITOR.increment("worker_scans")
            current_kind = getattr(self.market, "instrument_kind", "USD_M_PERPETUAL")
            hb = self.ledger.worker_heartbeat() if hasattr(self.ledger, "worker_heartbeat") else None
            eff_interval = int(hb.get("interval_seconds") or config.interval) if (hb and not manual) else int(config.interval)
            rej_init = empty_rejection_breakdown()
            diag_panel = empty_diagnostic_panel()
            p0_diag = empty_p0_scanner_diagnostics()
            hybrid_funnel = empty_hybrid_funnel()
            prof = resolve_strategy_profile(getattr(config, "strategy_profile", "BALANCED"), relaxed=config.relaxed_mode)
            eff_profile_name = prof["name"]
            if hb and hb.get("effective_strategy_profile") and not manual:
                eff_profile_name = str(hb.get("effective_strategy_profile"))
            report = {
                "scan_id": uuid4().hex[:12],
                "started_at_ms": started_wall_ms,
                "last_scan_at_ms": started_wall_ms,
                "next_scan_at_ms": started_wall_ms + eff_interval * 1000,
                "configured_interval": int(config.interval),
                "effective_interval": eff_interval,
                "strategy_profile": prof["name"],
                "effective_strategy_profile": eff_profile_name,
                "strategy_engine": "HYBRID_ADAPTIVE_V1",
                "primary_strategy_id": "F",
                "worker_id": str(worker_id or (hb.get("worker_id") if hb else self.instance_id)),
                "instrument_kind": current_kind,
                "market_provider": getattr(self.market, "market_label", current_kind),
                "ledger_db": database_display_identity(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter),
                "database_fingerprint": database_identity_fingerprint(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter),
                "validation": spot_reference_validation_metadata(current_kind),
                "scan_status": "RUNNING",
                "data_status": "OK",
                "news_status": "PENDING",
                "entry_block_status": "NONE",
                "position_limit_blocked": False,
                "trigger": "MANUAL" if manual else "AUTO",
                "now_ms": 0,
                "universe_loaded": False,
                "market_error": {},
                "total_discovered": 0,
                "universe_count": 0,
                "eligible_count": 0,
                "rejected_count": 0,
                "shortlist_count": 0,
                "stage1_passed": 0,
                "stage1_rejected": 0,
                "stage2_shortlist": 0,
                "queued_count": 0,
                "analyzed_count": 0,
                "deferred_count": 0,
                "expired_count": 0,
                "paper_entry_count": 0,
                "groq_calls_count": 0,
                "_groq_calls_this_cycle": 0,
                "futures_available": not bool(getattr(self.market, "futures_data_unavailable", False)),
                "failover_reason": str(getattr(self.market, "failover_reason", "") or ""),
                "pipeline_diagnostics": {
                    "universe_total": 0,
                    "stage1_passed": 0,
                    "stage1_rejected": 0,
                    "stage2_shortlist": 0,
                    "queued_count": 0,
                    "analyzed_count": 0,
                    "deferred_count": 0,
                    "expired_count": 0,
                    "rejected_count": 0,
                    "paper_entry_count": 0,
                },
                "expired_candidates": [],
                "deferred_candidates": [],
                "scan_results": [],
                "m15_cache_hits": 0,
                "m5_cache_hits": 0,
                "ai_cache_hits": 0,
                "rejection_breakdown": dict(rej_init),
                "diagnostic_panel": diag_panel,
                "scanner_diagnostics": p0_diag,
                "hybrid_funnel": hybrid_funnel,
                "filter_diagnostics": {
                    "total_discovered": 0,
                    "eligible_count": 0,
                    "rejected_count": 0,
                    "shortlist_count": 0,
                    "analyzed_count": 0,
                    "rejection_breakdown": dict(rej_init),
                },
                "shortlist": [],
                "long_watchlist": [],
                "short_watchlist": [],
                "long_watchlist_count": 0,
                "short_watchlist_count": 0,
                "selected": [],
                "ranked": [],
                "rotating_queue": [],
                "technical_candidates": [],
                "technical_scan_completed": False,
                "m15_error_diagnostics": [],
                "queued_candidates": [],
                "stage_timings": {
                    "market_fetch_s": 0.0,
                    "screening_s": 0.0,
                    "ai_analysis_s": 0.0,
                    "total_s": 0.0,
                    "budgets": {
                        "market_fetch_budget_s": float(getattr(config, "market_fetch_budget_s", 30.0)),
                        "screening_budget_s": float(getattr(config, "screening_budget_s", 45.0)),
                        "ai_budget_s": float(getattr(config, "ai_budget_s", 30.0)),
                        "per_candidate_timeout_s": float(getattr(config, "per_candidate_timeout_s", 25.0)),
                        "total_cycle_budget_s": float(getattr(config, "total_cycle_budget_s", 60.0)),
                    },
                },
                "analyses": [],
                "errors": [],
                "monitored": 0,
                "news": {"ok": False, "reason": "Belum diperiksa"},
                "config": asdict(config),
            }
            if self.last is None:
                self.last = report
            try:
                t_fetch_start = self.clock()
                now = int(self.market.get(getattr(self.market, "time_path", "/fapi/v1/time"))["serverTime"])
                self._sync_active_ledger()
                report["now_ms"] = now
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["ledger_db"] = database_display_identity(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter)
                report["database_fingerprint"] = database_identity_fingerprint(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter)
                report["validation"] = spot_reference_validation_metadata(report["instrument_kind"])
                before_active = {t["signal"]: t for t in self.ledger.active() if t["status"] == "PENDING"}
                self.ledger.expire_pending(now)
                if before_active and config.send_alerts and hasattr(self.alerts, "send_cancellation"):
                    after_active = {t["signal"] for t in self.ledger.active()}
                    for sig, old_t in before_active.items():
                        if sig not in after_active:
                            p_old = old_t["plan"]
                            self._call_alert(
                                "send_cancellation",
                                p_old["symbol"],
                                p_old.get("id", sig),
                                "EXPIRED (Melewati batas waktu candle eksekusi)",
                                session_token=eff_token,
                                report=report,
                            )
                monitor_ok = self.monitor(report, config, session_token=eff_token)
                universe = self.market.universe()
                self._sync_active_ledger()
                report["stage_timings"]["market_fetch_s"] = round(max(0.0, self.clock() - t_fetch_start), 3)
                report["instrument_kind"] = getattr(self.market, "instrument_kind", report["instrument_kind"])
                report["ledger_db"] = database_display_identity(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter)
                report["database_fingerprint"] = database_identity_fingerprint(self.ledger.db, pg_url=self._pg_url, get_secret=self._secret_getter)
                report["validation"] = spot_reference_validation_metadata(report["instrument_kind"])
                report["market_provider"] = getattr(self.market, "market_label", "Unknown")
                if getattr(self.market, "failover_reason", None):
                    report["provider_failover"] = self.market.failover_reason
                now = universe["now_ms"]
                report["now_ms"] = max(now, report["now_ms"])

                if getattr(self.market, "futures_data_unavailable", False) or (
                    getattr(config, "futures_primary_only", False) and report["instrument_kind"] == "SPOT_REFERENCE"
                ):
                    report["data_status"] = "FUTURES_DATA_UNAVAILABLE"
                    report["futures_available"] = False
                else:
                    report["futures_available"] = report["instrument_kind"] in ("USD_M_PERPETUAL", "BYBIT_LINEAR_PERPETUAL")
                report["failover_reason"] = str(getattr(self.market, "failover_reason", "") or "")

                t_screen_start = self.clock()
                is_legacy_cal = getattr(self.calendar, "__class__", None).__name__ == "Calendar"
                cfg_sl_limit = int(getattr(config, "shortlist_limit", 50))
                eff_shortlist_limit = 20 if (is_legacy_cal and cfg_sl_limit in (30, 50)) else min(50, max(1, cfg_sl_limit))
                screened = shortlist(
                    universe,
                    config.min_volume,
                    limit=eff_shortlist_limit,
                    allow_medium_liquidity_fallback=config.relaxed_mode or prof["name"] in ("BALANCED", "ACTIVE"),
                )
                rej_breakdown = dict(screened.get("rejection_breakdown") or empty_rejection_breakdown())
                funnel = dict(screened.get("funnel_counts") or {})
                diag_panel["universe_total"] = int(screened["universe_count"])
                diag_panel["lolos_freshness"] = int(funnel.get("lolos_freshness", screened["eligible_count"]))
                diag_panel["lolos_liquidity"] = int(funnel.get("lolos_liquidity", screened["eligible_count"]))
                diag_panel["lolos_spread"] = int(funnel.get("lolos_spread", screened["eligible_count"]))

                p0_diag["universe_total"] = int(screened["universe_count"])
                p0_diag["lolos_stage_1"] = int(screened["eligible_count"])
                p0_diag["stage_1_rejection_reasons"] = dict(rej_breakdown)
                hybrid_funnel["universe_total"] = int(screened["universe_count"])
                hybrid_funnel["passed_stage1"] = int(screened["eligible_count"])

                report.update(
                    universe_loaded=True,
                    total_discovered=screened.get("total_discovered", screened["universe_count"]),
                    universe_count=screened["universe_count"],
                    eligible_count=screened["eligible_count"],
                    rejected_count=screened.get("rejected_count", max(0, screened["universe_count"] - screened["eligible_count"])),
                    shortlist_count=len(screened["rows"]),
                    rejection_breakdown=rej_breakdown,
                    shortlist=screened["rows"],
                    long_watchlist=screened.get("long_watchlist", []),
                    short_watchlist=screened.get("short_watchlist", []),
                    long_watchlist_count=len(screened.get("long_watchlist", [])),
                    short_watchlist_count=len(screened.get("short_watchlist", [])),
                )
                report["filter_diagnostics"] = {
                    "total_discovered": report["total_discovered"],
                    "eligible_count": report["eligible_count"],
                    "rejected_count": report["rejected_count"],
                    "shortlist_count": report["shortlist_count"],
                    "analyzed_count": 0,
                    "rejection_breakdown": dict(rej_breakdown),
                }

                # BUG-42: Evaluate news window safely, and keep technical screening active on News API failure!
                report["news"] = self._safe_calendar_window(now)
                if report["news"].get("ok"):
                    report["news_status"] = "CLEAR"
                else:
                    reason_txt = str(report["news"].get("reason") or "Kalender berita tidak tersedia")
                    report["news_status"] = "NEWS_BLACKOUT" if "jendela berita" in reason_txt.lower() else "NEWS_UNAVAILABLE"
                    report["errors"].append(reason_txt)

                if not monitor_ok:
                    report["errors"].append("Sinyal baru ditahan karena posisi paper belum mendapat data segar")
                    return report

                if not report["news"].get("ok") and (
                    report["news"].get("source") == "SYNTHETIC CALENDAR"
                    or report["news"].get("reason") == "Fixture calendar"
                ):
                    return report

                # BUG-52: When 2 positions/pending orders are active, hold new entries (`ENTRY_BLOCKED_POSITION_LIMIT`),
                # but STILL discover candidates, update watchlists, and display top candidates!
                active = self.ledger.active()
                position_limit_reached = len(active) >= 2
                if position_limit_reached:
                    report["position_limit_blocked"] = True
                    report["entry_block_status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    report["errors"].append("ENTRY_BLOCKED_POSITION_LIMIT: Dua posisi/pending paper aktif; watchlist & kandidat tetap diperbarui, entry baru ditahan")

                busy = {t["plan"]["symbol"] for t in active}
                candidate_pool = [r for r in screened["rows"] if r["symbol"] not in busy]
                target_deep_count = int(config.candidates) if position_limit_reached else (
                    int(config.candidates) if int(config.candidates) > 2 else min(int(config.candidates), max(1, 2 - len(active)))
                )
                min_m15_gate = 0 if is_legacy_cal else int(getattr(config, "min_m15_deep_score", 55))

                sig_params = inspect.signature(select_candidates).parameters
                select_kwargs = {}
                if "return_diagnostics" in sig_params:
                    select_kwargs["return_diagnostics"] = True
                if "min_m15_score" in sig_params:
                    select_kwargs["min_m15_score"] = min_m15_gate
                if "balance_long_short" in sig_params:
                    select_kwargs["balance_long_short"] = True
                if "m15_cache" in sig_params:
                    select_kwargs["m15_cache"] = self.m15_eval_cache
                if "max_stage2" in sig_params:
                    select_kwargs["max_stage2"] = eff_shortlist_limit

                sel_out = select_candidates(
                    candidate_pool,
                    self.market,
                    now,
                    target_deep_count,
                    **select_kwargs,
                )
                if len(sel_out) == 4:
                    selected, ranked, errors, err_diags = sel_out
                else:
                    selected, ranked, errors = sel_out
                    err_diags = []
                report["stage_timings"]["screening_s"] = round(max(0.0, self.clock() - t_screen_start), 3)

                if err_diags:
                    rej_breakdown["rejected_missing_candle"] = int(rej_breakdown.get("rejected_missing_candle", 0)) + len(err_diags)
                    report["rejection_breakdown"] = dict(rej_breakdown)
                    report["m15_error_diagnostics"] = err_diags
                    hist_insuff_errs = [e["symbol"] for e in err_diags if e.get("code") == "HISTORY_INSUFFICIENT"]
                    if hist_insuff_errs:
                        p0_diag["rejected_by_candle_history"] += len(hist_insuff_errs)
                        p0_diag["history_insufficient_symbols"].extend(hist_insuff_errs)
                        report["data_status"] = "HISTORY_INSUFFICIENT"

                # Update Mandatory Diagnostic Panel & P0-7 Stage 2 counters
                diag_panel["lolos_candle_history"] = len(ranked)
                diag_panel["m15_watch"] = sum(1 for x in ranked if int(x.get("m15_score", 0)) >= 55)
                diag_panel["m15_priority"] = sum(1 for x in ranked if int(x.get("m15_score", 0)) >= 70)
                m15_rejected_conf = sum(1 for x in ranked if int(x.get("m15_score", 0)) < max(55, min_m15_gate))
                diag_panel["rejected_by_confidence"] += m15_rejected_conf
                report["m15_cache_hits"] = sum(1 for x in ranked if x.get("m15_cached"))

                p0_diag["lolos_candle_history"] = sum(1 for x in ranked if x.get("history_sufficient", True))
                short_hist_syms = [x["symbol"] for x in ranked if not x.get("history_sufficient", True)]
                if short_hist_syms:
                    p0_diag["rejected_by_candle_history"] += len(short_hist_syms)
                    p0_diag["history_insufficient_symbols"].extend(short_hist_syms)
                hybrid_funnel["passed_m15_history"] = p0_diag["lolos_candle_history"]

                for r_item in ranked:
                    reg_k = r_item.get("m15_regime") or r_item.get("regime")
                    if reg_k in p0_diag["m15_regime_distribution"]:
                        p0_diag["m15_regime_distribution"][reg_k] += 1
                        hybrid_funnel["regime_counts"][reg_k] = p0_diag["m15_regime_distribution"][reg_k]
                    st_k = r_item.get("strategy_id")
                    if st_k in p0_diag["strategy_candidate_counts"]:
                        p0_diag["strategy_candidate_counts"][st_k] += 1
                        hybrid_funnel["strategy_candidate_counts"][st_k] = p0_diag["strategy_candidate_counts"][st_k]
                    sc_val = int(r_item.get("m15_score", 0))
                    if 55 <= sc_val <= 64:
                        hybrid_funnel["early_watch_count"] += 1
                    elif 65 <= sc_val <= 77:
                        hybrid_funnel["setup_forming_count"] += 1
                    elif sc_val >= 78:
                        hybrid_funnel["entry_candidate_count"] += 1

                # BUG-45 #2 & v1.1 Section 3: Persistent rotating queue (max 50 capacity) with data refresh & expiry tracking
                max_q_age_ms = int(getattr(config, "max_queue_age_ms", 900_000))
                persisted_q_state = self.ledger.load_candidate_queue() if hasattr(self.ledger, "load_candidate_queue") else {"items": []}
                persisted_items = list((persisted_q_state or {}).get("items") or [])
                persisted_map = {str(item.get("symbol")): item for item in persisted_items if isinstance(item, dict) and item.get("symbol")}
                for mem_sym in (list(self.pending_candidate_queue) + list(self.rotating_candidate_queue)):
                    if mem_sym and mem_sym not in persisted_map:
                        persisted_map[mem_sym] = {"symbol": mem_sym, "queued_at_ms": now}
                if self.next_symbol and self.next_symbol not in persisted_map:
                    persisted_map[self.next_symbol] = {"symbol": self.next_symbol, "queued_at_ms": now}

                eligible_m15_rows = [r for r in ranked if int(r.get("m15_score", 0)) >= min_m15_gate][:50]
                rot_map = {r["symbol"]: dict(r) for r in eligible_m15_rows}
                expired_candidates = []
                valid_prior_syms = []
                for p_sym, p_meta in persisted_map.items():
                    q_at = int(p_meta.get("queued_at_ms") or now)
                    q_age = max(0, now - q_at)
                    if p_sym in busy:
                        expired_candidates.append({"symbol": p_sym, "reason": "ALREADY_ACTIVE_POSITION", "queue_age_ms": q_age})
                    elif q_age > max_q_age_ms:
                        expired_candidates.append({"symbol": p_sym, "reason": "QUEUE_TTL_EXPIRED", "queue_age_ms": q_age})
                    elif p_sym not in rot_map:
                        expired_candidates.append({"symbol": p_sym, "reason": "SETUP_INVALIDATED_ON_REFRESH", "queue_age_ms": q_age})
                    else:
                        rot_map[p_sym]["queued_at_ms"] = q_at
                        rot_map[p_sym]["queue_age_ms"] = q_age
                        rot_map[p_sym]["resumed_from_queue"] = True
                        valid_prior_syms.append(p_sym)

                for sym_k, r_obj in rot_map.items():
                    r_obj.setdefault("queued_at_ms", now)
                    r_obj.setdefault("queue_age_ms", max(0, now - int(r_obj["queued_at_ms"])))

                pending_syms = [s for s in self.pending_candidate_queue if s in rot_map]
                if self.next_symbol and self.next_symbol in rot_map and self.next_symbol not in pending_syms:
                    pending_syms.insert(0, self.next_symbol)
                prioritized_syms = pending_syms + [s for s in valid_prior_syms if s not in pending_syms]
                fresh_syms = [r["symbol"] for r in selected if r["symbol"] in rot_map and r["symbol"] not in prioritized_syms]
                other_syms = [r["symbol"] for r in eligible_m15_rows if r["symbol"] not in prioritized_syms and r["symbol"] not in fresh_syms]
                combined_order = (prioritized_syms + fresh_syms + other_syms)[:50]

                if len(eligible_m15_rows) > target_deep_count and not is_legacy_cal:
                    selected = [rot_map[s] for s in combined_order[:target_deep_count]]
                    self.rotating_candidate_queue = [s for s in combined_order[target_deep_count:] if s not in pending_syms]
                else:
                    for r_sel in selected:
                        if r_sel["symbol"] in rot_map:
                            r_sel.update(rot_map[r_sel["symbol"]])
                        else:
                            r_sel.setdefault("queued_at_ms", now)
                            r_sel.setdefault("queue_age_ms", 0)
                    self.rotating_candidate_queue = [
                        s for s in combined_order if s not in {x["symbol"] for x in selected}
                    ]
                report["rotating_queue"] = list(self.rotating_candidate_queue)
                report["expired_candidates"] = expired_candidates

                report.update(
                    selected=selected,
                    ranked=ranked,
                    technical_candidates=selected,
                    technical_scan_completed=True,
                    long_watchlist=[x for x in ranked if x.get("candidate_direction") == "LONG"],
                    short_watchlist=[x for x in ranked if x.get("candidate_direction") == "SHORT"],
                    long_watchlist_count=sum(x.get("candidate_direction") == "LONG" for x in ranked),
                    short_watchlist_count=sum(x.get("candidate_direction") == "SHORT" for x in ranked),
                )
                report["errors"].extend(errors)
                report["filter_diagnostics"]["rejection_breakdown"] = dict(rej_breakdown)

                # BUG-45 #6 & BUG-46: Send EARLY_WATCH alerts for qualified watchlist candidates outside the deep analysis batch!
                selected_syms = {r["symbol"] for r in selected}
                if config.send_watchlist_alerts and hasattr(self.alerts, "send_watchlist"):
                    early_threshold = int(prof.get("watchlist_early_threshold", 55))
                    for wl_row in ranked:
                        if wl_row["symbol"] in selected_syms:
                            continue
                        if int(wl_row.get("m15_score", 0)) >= early_threshold:
                            wl_info = dict(wl_row, instrument_kind=report["instrument_kind"])
                            self._call_alert(
                                "send_watchlist",
                                None,
                                wl_info,
                                stage="EARLY_WATCH",
                                session_token=eff_token,
                                report=report,
                            )

                queued_order = list(self.pending_candidate_queue)
                if self.next_symbol and self.next_symbol not in queued_order:
                    queued_order.insert(0, self.next_symbol)

                def _candidate_priority(r):
                    sym = r["symbol"]
                    if sym in queued_order:
                        return (0, queued_order.index(sym))
                    return (1, 0)

                processing_order = sorted(selected, key=_candidate_priority)
                total_budget = float(getattr(config, "total_cycle_budget_s", 60.0))
                ai_budget = float(getattr(config, "ai_budget_s", 30.0))
                cand_timeout = float(getattr(config, "per_candidate_timeout_s", 25.0))
                t_ai_stage_start = self.clock()

                for idx, row in enumerate(processing_order):
                    elapsed_total = self.clock() - self.last_start
                    elapsed_ai = self.clock() - t_ai_stage_start
                    if elapsed_total > total_budget or (idx > 0 and elapsed_ai > ai_budget):
                        remaining = processing_order[idx:]
                        self.next_symbol = remaining[0]["symbol"]
                        self.pending_candidate_queue = [r["symbol"] for r in remaining]
                        report["queued_candidates"] = list(self.pending_candidate_queue)
                        report["scan_status"] = "PARTIAL_SCAN"
                        report["errors"].append("Anggaran waktu siklus tercapai; kandidat tersisa menunggu siklus berikutnya")
                        break
                    try:
                        t_cand_start = self.clock()
                        if self.next_symbol == row["symbol"]:
                            self.next_symbol = None
                        if row["symbol"] in self.pending_candidate_queue:
                            self.pending_candidate_queue.remove(row["symbol"])
                        self._analyse(
                            row["symbol"],
                            config,
                            report,
                            candidate_info=row,
                            session_token=eff_token,
                            position_limit_reached=position_limit_reached,
                        )
                        cand_elapsed = self.clock() - t_cand_start
                        if cand_elapsed > cand_timeout and idx + 1 < len(processing_order):
                            remaining = processing_order[idx + 1:]
                            self.next_symbol = remaining[0]["symbol"]
                            self.pending_candidate_queue = [r["symbol"] for r in remaining]
                            report["queued_candidates"] = list(self.pending_candidate_queue)
                            report["scan_status"] = "PARTIAL_SCAN"
                            report["errors"].append(
                                f"Anggaran waktu per kandidat ({cand_timeout:.0f}s) terlampaui pada {row['symbol']}; kandidat tersisa menunggu siklus berikutnya"
                            )
                            break
                    except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                        if isinstance(exc, MarketError) and exc.access_blocked:
                            raise
                        report["errors"].append(row["symbol"] + " WAIT: " + self.pool.redact(str(exc))[:300])

                report["stage_timings"]["ai_analysis_s"] = round(max(0.0, self.clock() - t_ai_stage_start), 3)
                analyzed_syms = {d["snapshot"].symbol for d in report["analyses"] if isinstance(d, dict) and "snapshot" in d}
                deferred_syms = []
                for s_def in (list(self.pending_candidate_queue) + list(self.rotating_candidate_queue) + [r["symbol"] for r in selected]):
                    if s_def and s_def not in analyzed_syms and s_def not in deferred_syms:
                        deferred_syms.append(s_def)
                self.rotating_candidate_queue = list(deferred_syms)
                report["rotating_queue"] = list(deferred_syms)
                deferred_objs = [
                    {
                        "symbol": s_def,
                        "queued_at_ms": int((rot_map.get(s_def) or {}).get("queued_at_ms") or now),
                        "queue_age_ms": max(0, now - int((rot_map.get(s_def) or {}).get("queued_at_ms") or now)),
                        "m15_score": int((rot_map.get(s_def) or {}).get("m15_score") or 0),
                        "candidate_direction": (rot_map.get(s_def) or {}).get("candidate_direction"),
                        "strategy_id": (rot_map.get(s_def) or {}).get("strategy_id"),
                        "m15_regime": (rot_map.get(s_def) or {}).get("m15_regime"),
                    }
                    for s_def in deferred_syms
                ]
                report["deferred_candidates"] = deferred_objs
                report["queue_persistence_status"] = "OK"
                if hasattr(self.ledger, "save_candidate_queue"):
                    try:
                        self.ledger.save_candidate_queue(
                            {"items": deferred_objs, "expired": expired_candidates, "updated_at_ms": now},
                            now_ms=now,
                        )
                    except Exception as q_exc:
                        report["queue_persistence_status"] = "SAVE_FAILED"
                        report["scan_status"] = "DEGRADED_QUEUE_SAVE_FAILED"
                        report["errors"].append(f"QUEUE_SAVE_FAILED: {str(q_exc)[:180]}")
                report["analyzed_count"] = len(report["analyses"])
                report["filter_diagnostics"]["analyzed_count"] = len(report["analyses"])
                diag_panel["m5_analyzed"] = len(report["analyses"])

                # v6.0.5 Path B: Promote verified Futures VALID setups to Predictive Watchlist CONFIRMED / PAPER_ENTRY
                if self.predictive_store is None and report.get("instrument_kind") != "SPOT_REFERENCE":
                    report["errors"].append(f"PREDICTIVE_STORE_UNAVAILABLE: {self.predictive_store_error}")
                    report["predictive_store_status"] = "PREDICTIVE_STORE_UNAVAILABLE"
                    for _blk in report.get("analyses", []):
                        if _blk.get("status") == "VALID":
                            _blk["paper_entry_blocked"] = True
                            _blk["paper"] = "DIBLOKIR (FAIL-CLOSED): PREDICTIVE_STORE_UNAVAILABLE"
                if self.predictive_store is not None and report.get("instrument_kind") != "SPOT_REFERENCE":
                    for item in report.get("analyses", []):
                        res_obj = item.get("result")
                        snap_obj = item.get("snapshot")
                        if res_obj is not None and snap_obj is not None and item.get("status") == "VALID":
                            existing_sig = self.predictive_store.get_active_signal(snap_obj.symbol)
                            tr_conf = self.predictive_lifecycle.promote_to_confirmed(
                                existing_sig,
                                hybrid_result=res_obj,
                                snapshot=snap_obj,
                                now_ms=now,
                            )
                            if tr_conf is not None:
                                msg_conf = format_predictive_alert(
                                    tr_conf.signal,
                                    event_type=tr_conf.alert_event_type,
                                    confirmed_plan=res_obj.plan,
                                )
                                try:
                                    self.predictive_store.save_transition(tr_conf, alert_message=msg_conf, now_ms=now)
                                    if item.get("paper") and "dicatat" in str(item.get("paper")).lower():
                                        tr_pe = self.predictive_lifecycle.mark_paper_entry(
                                            tr_conf.signal,
                                            now_ms=now,
                                            trade_id=str((res_obj.plan or {}).get("id", ""))[:12],
                                        )
                                        if tr_pe is not None:
                                            self.predictive_store.save_transition(tr_pe, now_ms=now)
                                except PredictiveStoreError as p_exc:
                                    report["errors"].append(f"PREDICTIVE_STORE_FAILED: {str(p_exc)[:160]}")
                # P1-04: Preserve budget-overflow `queued_candidates` (BUG-44) AND expose complete non-truncated `all_deferred_symbols`
                full_deferred_syms = [d["symbol"] for d in deferred_objs]
                report["all_deferred_symbols"] = list(full_deferred_syms)
                report["all_deferred_total"] = len(full_deferred_syms)
                report["queued_candidates_total"] = len(report.get("queued_candidates", []))
                pr = report["scanner_diagnostics"].get("pipeline_reconciliation", {})
                s2_total = len(report.get("shortlist", []))
                an_cnt = len(report.get("analyses", []))
                def_cnt = len(deferred_objs)
                exp_cnt = len(expired_candidates)
                rej_pre = max(0, s2_total - an_cnt - def_cnt - exp_cnt)
                pr.update({
                    "stage2_shortlist": s2_total,
                    "queued_count": def_cnt,
                    "analyzed_count": an_cnt,
                    "deferred_count": def_cnt,
                    "expired_count": exp_cnt,
                    "rejected_pre_deep_count": rej_pre,
                    "reconciliation_ok": (an_cnt + def_cnt + exp_cnt + rej_pre == s2_total),
                })
                report["scanner_diagnostics"]["pipeline_reconciliation"] = pr

                # P1-05: Structured Score & Hard Gate Clarity on every analysis item (never turns NO_TRADE into entry)
                for _item in report.get("analyses", []):
                    _res = _item.get("result")
                    if _res is not None:
                        _checks = getattr(_res, "checks", []) or []
                        _failed = [c["Kriteria"] for c in _checks if c.get("Status") not in ("LULUS", "SIMULASI")]
                        _passed = [c["Kriteria"] for c in _checks if c.get("Status") in ("LULUS", "SIMULASI")]
                        _m5_trig = bool((_res.metrics or {}).get("m5_trigger_passed") or (_res.status == "VALID"))
                        _item["decision_transparency"] = {
                            "symbol": _item.get("symbol"),
                            "setup_quality_score": (_res.metrics or {}).get("setup_quality_score", _item.get("setup_quality_score")),
                            "score_disclaimer": "Skor kualitas setup teknikal (0-100), bukan probabilitas profit.",
                            "m5_trigger_status": "PASSED" if _m5_trig else "NOT_TRIGGERED",
                            "m15_regime": (_res.meta or {}).get("m15_regime", "UNKNOWN"),
                            "strategy_route": (_res.meta or {}).get("routed_strategy", "F"),
                            "hard_gates_passed_count": len(_passed),
                            "hard_gates_failed": _failed,
                            "final_decision": _item.get("status", _res.status),
                            "reason_codes": list(getattr(_res, "reasons", []) or []),
                        }

                # P1-06: Separated 6-bucket Cycle & AI Budget Telemetry
                _tot_elapsed = round(max(0.0, self.clock() - self.last_start), 3)
                _stage_t = report.get("stage_timings") or {}
                _mkt_fetch_s = round(float(_stage_t.get("market_fetch_s", 0.0)), 3)
                _groq_used = int(report.get("_groq_calls_this_cycle", report.get("groq_calls_used", 0)))
                _groq_max = int(getattr(config, "max_groq_candidates", 5))
                _groq_req_s = round(float(self._cycle_groq_request_time_s), 4)
                _adv_proc_s = round(float(self._cycle_advisor_processing_time_s), 4)
                _persist_s = round(float(self._cycle_persistence_time_s), 4)
                _local_an_s = round(max(0.0, (self.clock() - t_ai_stage_start) - _groq_req_s - _adv_proc_s), 3)
                _q_wait_s = round(max(0.0, _tot_elapsed - _mkt_fetch_s - _local_an_s - _groq_req_s - _adv_proc_s - _persist_s), 3)
                report["scanner_diagnostics"]["budget_telemetry"] = {
                    "market_fetch_time_s": _mkt_fetch_s,
                    "local_analysis_time_s": _local_an_s,
                    "groq_request_time_s": _groq_req_s,
                    "advisor_processing_time_s": _adv_proc_s,
                    "queue_wait_time_s": _q_wait_s,
                    "persistence_time_s": _persist_s,
                    "ai_elapsed_s": _groq_req_s,
                    "ai_budget_s": float(ai_budget),
                    "ai_budget_exceeded": _groq_req_s > float(ai_budget),
                    "total_elapsed_s": _tot_elapsed,
                    "total_budget_s": float(total_budget),
                    "total_budget_exceeded": _tot_elapsed > float(total_budget),
                    "groq_calls_used": _groq_used,
                    "groq_max_per_cycle": _groq_max,
                    "groq_limit_respected": _groq_used <= _groq_max,
                    "deferred_due_to_budget": len(self.pending_candidate_queue),
                }

                # P0-02, P0-03, P0-05, P1-07, P1-09: Separated Runtime Diagnostics
                _raw_wid = str(report.get("worker_id") or "")
                _is_independent_worker = _raw_wid.startswith("worker-")
                _wh_now = self.ledger.worker_health_status() if hasattr(self.ledger, "worker_health_status") else {}
                _tg_stat = self.alerts.telegram_status(self.predictive_store) if hasattr(self.alerts, "telegram_status") else {}
                _cycle_tg = dict(report["scanner_diagnostics"].get("telegram_status_counts", {}))
                _mem_prof = profile_scanner_memory(self, self.intrabar_stream)
                _prov_diag = self.market.provider_diagnostics() if hasattr(self.market, "provider_diagnostics") else {
                    "active_provider": report.get("market_provider"),
                    "instrument_kind": report.get("instrument_kind"),
                    "futures_available": bool(getattr(self.market, "futures_available", report.get("instrument_kind") != "SPOT_REFERENCE")),
                    "data_status": report.get("data_status"),
                    "primary_http_status": None,
                    "primary_error_class": None,
                    "fallback_http_status": None,
                    "fallback_error_class": None,
                    "last_successful_futures_ms": None,
                    "provider_failover_reason": report.get("provider_failover") or getattr(self.market, "failover_reason", None),
                }
                _pred_diag = dict(self.last_predictive_diagnostics) if self.last_predictive_diagnostics else {
                    "enabled": bool(self.predictive_config.predictive_watchlist_enabled),
                    "predictive_status": "PREDICTIVE_STORE_UNAVAILABLE" if self.predictive_store is None else "MONITOR_OFFLINE",
                    "monitor_health": "MONITOR_DEGRADED" if self.predictive_store is None else "MONITOR_OFFLINE",
                }
                report["runtime_diagnostics"] = {
                    "process_role": "WORKER" if _is_independent_worker else "DASHBOARD",
                    "dashboard_process": {
                        "active": not _is_independent_worker,
                        "scanner_instance_id": self.instance_id if not _is_independent_worker else None,
                        "pid": _mem_prof["pid"] if not _is_independent_worker else None,
                        "rss_mb": _mem_prof["rss_mb"] if not _is_independent_worker else None,
                    },
                    "worker_process": {
                        "active": bool(_wh_now.get("alive")) if not _is_independent_worker else True,
                        "worker_state": _wh_now.get("worker_state", "WORKER_OFFLINE"),
                        "heartbeat_verified": bool(_wh_now.get("heartbeat_verified", False)),
                        "worker_id": _raw_wid if _is_independent_worker else _wh_now.get("worker_id"),
                        "pid": _mem_prof["pid"] if _is_independent_worker else None,
                        "rss_mb": _mem_prof["rss_mb"] if _is_independent_worker else None,
                    },
                    "market_provider": _prov_diag,
                    "persistence_identity": {
                        "backend": "POSTGRESQL" if self._pg_url else "SQLITE_WAL",
                        "database_display": report.get("ledger_db"),
                        "database_fingerprint": report.get("database_fingerprint"),
                    },
                    "predictive_monitor": _pred_diag,
                    "deep_scanner": {
                        "pipeline_reconciliation": dict(pr),
                        "budget_telemetry": dict(report["scanner_diagnostics"]["budget_telemetry"]),
                    },
                    "groq_advisor": {
                        "advisor_only": True,
                        "max_per_cycle": _groq_max,
                        "used_this_cycle": _groq_used,
                        "status_counts": dict(report["scanner_diagnostics"].get("groq_status_counts", {})),
                    },
                    "telegram_outbox": {
                        **_tg_stat,
                        "cycle_status_counts": _cycle_tg,
                        "counter_scope_note": "cycle_status_counts = siklus ini; requests/success/errors = kumulatif proses + outbox.",
                    },
                    "memory_profile": _mem_prof,
                }
                report["scanner_diagnostics"]["predictive_watchlist"] = dict(self.last_predictive_diagnostics)
                report["scanner_diagnostics"]["market_stream"] = self.intrabar_stream.health_status()
                report["scanner_diagnostics"]["database"] = (
                    self.predictive_store.health_check() if self.predictive_store is not None else {"ok": False}
                )
                if config.send_scan_summary and hasattr(self.alerts, "send_summary"):
                    report["summary_telegram"] = self._call_alert("send_summary", report, session_token=eff_token, report=report)
            except (MarketError, ValueError, KeyError, TypeError, ScanBusy) as exc:
                report["scan_status"] = "FAILED"
                if isinstance(exc, ScanBusy):
                    report["scan_notice"] = str(exc)
                elif isinstance(exc, MarketError):
                    report["data_status"] = "CONNECTION_ERROR"
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
                if started:
                    self.last = report
                    completed_ms = int(time.time() * 1000)
                    total_dur = round(max(0.0, self.clock() - self.last_start), 2)
                    self.last["completed_at_ms"] = completed_ms
                    self.last["duration_seconds"] = total_dur
                    self.last["stage_timings"]["total_s"] = total_dur
                    self.last["analyzed_count"] = len(self.last.get("analyses", []))
                    u_tot = int(self.last.get("universe_count", 0))
                    s1_pass = int(self.last.get("eligible_count", 0))
                    s1_rej = max(0, u_tot - s1_pass)
                    s2_short = int(self.last.get("shortlist_count", len(self.last.get("shortlist", []))))
                    an_cnt = int(self.last["analyzed_count"])
                    def_cnt = len(self.last.get("deferred_candidates") or self.last.get("rotating_queue") or [])
                    exp_cnt = len(self.last.get("expired_candidates") or [])
                    can_cnt = int(self.last.get("cancelled_count", 0))
                    pe_cnt = int((self.last.get("hybrid_funnel") or {}).get("paper_entries_created", 0))
                    _shortlist_syms = {r.get("symbol") for r in (self.last.get("shortlist") or []) if isinstance(r, dict)}
                    _prior_exp_outside = sum(
                        1 for e in (self.last.get("expired_candidates") or [])
                        if isinstance(e, dict) and e.get("symbol") not in _shortlist_syms
                    )
                    _effective_s2_total = s2_short + _prior_exp_outside
                    rej_pre_deep = max(0, _effective_s2_total - an_cnt - def_cnt - exp_cnt - can_cnt)
                    _scan_id_str = str(self.last.get("scan_id") or "scan")
                    _cand_states = []
                    _seen_syms = set()
                    for _a in (self.last.get("analyses") or []):
                        _sy = (_a.get("symbol") if isinstance(_a, dict) else None)
                        if _sy and _sy not in _seen_syms:
                            _seen_syms.add(_sy)
                            _cand_states.append({"candidate_id": f"{_scan_id_str}:{_sy}", "symbol": _sy, "status": "ANALYZED", "status_timestamp_ms": completed_ms})
                    for _d in (self.last.get("deferred_candidates") or []):
                        _sy = (_d.get("symbol") if isinstance(_d, dict) else None)
                        if _sy and _sy not in _seen_syms:
                            _seen_syms.add(_sy)
                            _cand_states.append({"candidate_id": f"{_scan_id_str}:{_sy}", "symbol": _sy, "status": "DEFERRED", "status_timestamp_ms": completed_ms})
                    for _e in (self.last.get("expired_candidates") or []):
                        _sy = (_e.get("symbol") if isinstance(_e, dict) else None)
                        if _sy and _sy not in _seen_syms:
                            _seen_syms.add(_sy)
                            _cand_states.append({"candidate_id": f"{_scan_id_str}:{_sy}", "symbol": _sy, "status": "EXPIRED", "status_timestamp_ms": completed_ms})
                    reconcil = reconcile_candidate_pipeline(
                        universe_total=u_tot,
                        stage1_passed=s1_pass,
                        stage1_rejected=s1_rej,
                        stage2_total=_effective_s2_total,
                        active_queue_count=0,
                        analyzed_count=an_cnt,
                        deferred_count=def_cnt,
                        expired_count=exp_cnt,
                        rejected_count=rej_pre_deep,
                        cancelled_count=can_cnt,
                        paper_entry_count=pe_cnt,
                        candidate_states=_cand_states,
                    )
                    self.last["stage1_passed"] = s1_pass
                    self.last["stage1_rejected"] = s1_rej
                    self.last["stage2_shortlist"] = s2_short
                    self.last["active_queue_count"] = reconcil["active_queue_count"]
                    self.last["queued_count"] = reconcil["active_queue_count"]
                    self.last["deferred_count"] = def_cnt
                    self.last["expired_count"] = exp_cnt
                    self.last["rejected_count"] = reconcil["rejected_count"]
                    self.last["cancelled_count"] = can_cnt
                    self.last["paper_entry_count"] = pe_cnt
                    self.last["groq_calls_count"] = int(self.last.get("_groq_calls_this_cycle", 0))
                    self.last["pipeline_diagnostics"] = reconcil
                    if "scanner_diagnostics" in self.last:
                        self.last["scanner_diagnostics"]["pipeline_reconciliation"] = dict(reconcil)
                    if isinstance(self.last.get("runtime_diagnostics"), dict) and isinstance(self.last["runtime_diagnostics"].get("deep_scanner"), dict):
                        self.last["runtime_diagnostics"]["deep_scanner"]["pipeline_reconciliation"] = dict(reconcil)
                        self.last["scanner_diagnostics"]["provider_failover"] = str(self.last.get("provider_failover") or self.last.get("failover_reason") or "")
                        self.last["scanner_diagnostics"]["telemetry"] = GLOBAL_MONITOR.telemetry_snapshot()
                    if "filter_diagnostics" in self.last:
                        self.last["filter_diagnostics"]["analyzed_count"] = self.last["analyzed_count"]
                    if "diagnostic_panel" in self.last:
                        self.last["diagnostic_panel"]["m5_analyzed"] = self.last["analyzed_count"]
                        errs = self.last.get("errors") or []
                        self.last["diagnostic_panel"]["last_scan_error"] = str(errs[-1]) if errs else ""
                    if "scanner_diagnostics" in self.last:
                        try:
                            self.last["scanner_diagnostics"]["worker_heartbeat"] = self.ledger.worker_health_status(now_ms=completed_ms)
                        except Exception:
                            self.last["scanner_diagnostics"]["worker_heartbeat"] = {"worker_state": "WORKER_OFFLINE"}
                        try:
                            self.last["scanner_diagnostics"]["persistence_status"] = self.ledger.persistence_info()
                        except Exception:
                            self.last["scanner_diagnostics"]["persistence_status"] = {"backend": "UNKNOWN"}
                    self.last["resources"] = GLOBAL_MONITOR.snapshot()
                    if self.last["scan_status"] == "RUNNING":
                        self.last["scan_status"] = "COMPLETED_WITH_NOTES" if self.last["errors"] else "COMPLETED"
                    _t_save = self.clock()
                    self.ledger.save_scan_report(self.last)
                    self._cycle_persistence_time_s += max(0.0, self.clock() - _t_save)
                    _final_wall = round(max(0.0, self.clock() - self.last_start), 3)
                    self.last["duration_seconds"] = _final_wall
                    self.last["stage_timings"]["total_s"] = _final_wall
                    _sd_fin = self.last.setdefault("scanner_diagnostics", {})
                    _bt = _sd_fin.setdefault("budget_telemetry", {"ai_budget_s": 30.0, "total_budget_s": 60.0, "groq_calls_used": 0, "groq_max_per_cycle": 5, "groq_limit_respected": True})
                    if isinstance(_bt, dict):
                        _bt["total_elapsed_s"] = _final_wall
                        _bt["total_budget_exceeded"] = _final_wall > float(_bt.get("total_budget_s", 60.0))
                        _bt["groq_request_time_s"] = round(float(self._cycle_groq_request_time_s), 4)
                        _bt["ai_elapsed_s"] = _bt["groq_request_time_s"]
                        _bt["advisor_processing_time_s"] = round(float(self._cycle_advisor_processing_time_s), 4)
                        _bt["persistence_time_s"] = round(float(self._cycle_persistence_time_s), 4)
                        _bt["clock_source"] = "monotonic"
                        _bt["queue_wait_included_in_total"] = True
                        self.last["stage_timings"]["local_deep_analysis_s"] = _bt.get("local_analysis_time_s", 0.0)
                        self.last["stage_timings"]["ai_analysis_s"] = _bt["groq_request_time_s"]
                        _bt["non_additive_overlap_note"] = "Stage durations are measured independently on monotonic clock and are not summed across overlapping stages."
                        if isinstance(self.last.get("runtime_diagnostics"), dict) and isinstance(self.last["runtime_diagnostics"].get("deep_scanner"), dict):
                            self.last["runtime_diagnostics"]["deep_scanner"]["budget_telemetry"] = dict(_bt)
            finally:
                self._release_owned_locks()
                self.lock.release()

    def _analyse(self, symbol, config, report, candidate_info=None, session_token=None, position_limit_reached=False):
        """P0-1 & P0-2: Deep evaluation using Strategy F (`evaluate_hybrid_adaptive`) as the primary and sole
        production evaluator (M15 regime -> M5 trigger routing A–E -> Risk Hard Gates) and Groq as Advisor-Only.
        """
        snapshots = self.market.load_modes(symbol, ("SCALPING",))
        self._sync_active_ledger()
        prof = resolve_strategy_profile(getattr(config, "strategy_profile", "BALANCED"), relaxed=config.relaxed_mode)
        diag_panel = report.get("diagnostic_panel") if isinstance(report.get("diagnostic_panel"), dict) else empty_diagnostic_panel()
        p0_diag = report.get("scanner_diagnostics") if isinstance(report.get("scanner_diagnostics"), dict) else empty_p0_scanner_diagnostics()
        hybrid_funnel = report.get("hybrid_funnel") if isinstance(report.get("hybrid_funnel"), dict) else empty_hybrid_funnel()

        t_cand_proc_start = self.clock()
        min_m15_req = int(getattr(config, "min_m15_candles", DEFAULT_MIN_CANDLES_M15))
        min_m5_req = int(getattr(config, "min_m5_candles", DEFAULT_MIN_CANDLES_M5))
        router_futures_unavail = bool(
            getattr(self.market, "futures_data_unavailable", False)
            or report.get("data_status") == "FUTURES_DATA_UNAVAILABLE"
        )
        futures_unavail = bool(
            getattr(config, "futures_primary_only", False)
            and (router_futures_unavail or getattr(self.market, "instrument_kind", "") == "SPOT_REFERENCE")
        )
        allow_spot_sim = not bool(getattr(config, "futures_primary_only", False))

        results = []
        for mode in ("SCALPING",):
            snap = snapshots[mode]
            news_win = self._safe_calendar_window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            res_eval = evaluate_hybrid_adaptive(
                snap,
                account,
                paper=True,
                arming_store=self.arming_store,
                min_m15_candles=min_m15_req,
                min_m5_candles=min_m5_req,
                allow_spot_paper_simulation=(snap.instrument_kind == "SPOT_REFERENCE" and allow_spot_sim),
                futures_unavailable=futures_unavail,
            )
            results.append(res_eval)
        chosen = max(results, key=mode_score)
        if chosen.meta.get("stale_data"):
            report["data_status"] = "STALE_DATA"
        if chosen.meta.get("history_status") == "HISTORY_INSUFFICIENT":
            report["data_status"] = "HISTORY_INSUFFICIENT"
            p0_diag["rejected_by_candle_history"] = int(p0_diag.get("rejected_by_candle_history", 0)) + 1
            if symbol not in p0_diag["history_insufficient_symbols"]:
                p0_diag["history_insufficient_symbols"].append(symbol)

        comparison = [{"mode": r.meta["mode"], "direction": r.direction, "status": r.status,
                       "strategy_id": r.meta.get("routed_strategy") or "F",
                       "regime": r.meta.get("m15_regime") or "UNKNOWN",
                       "score": round(mode_score(r)[2], 4), "reasons": r.reasons[:5], "metrics": r.metrics}
                      for r in results]
        chosen.meta["mode_comparison"] = comparison
        if candidate_info:
            chosen.meta["stage2_screening"] = {
                "m15_score": candidate_info.get("m15_score"),
                "category": candidate_info.get("category"),
                "candidate_direction": candidate_info.get("candidate_direction"),
                "m15_regime": candidate_info.get("m15_regime"),
                "strategy_id": candidate_info.get("strategy_id"),
                "m15_summary": candidate_info.get("m15_summary"),
            }
        mode = chosen.meta["mode"]
        snap = snapshots[mode]
        acquired = self.clock()

        # BUG-53 & v1.1 Section 7: Build candle-close AI cache key and enforce max_groq_candidates (5) per cycle
        exec_close_ms = int(snap.execution.close_time.iloc[-1]) if len(snap.execution) else int(snap.now_ms)
        ai_cache_key = (symbol, mode, exec_close_ms, chosen.direction, chosen.status)
        max_groq = int(getattr(config, "max_groq_candidates", 5))
        groq_used = int(report.get("_groq_calls_this_cycle", 0))
        if ai_cache_key in self.ai_review_cache or groq_used < max_groq:
            ai = self._call_ai_review(chosen, session_token=session_token, cache_key=ai_cache_key)
            if ai.get("cached"):
                report["ai_cache_hits"] = int(report.get("ai_cache_hits", 0)) + 1
            else:
                report["_groq_calls_this_cycle"] = groq_used + 1
        else:
            ai = {
                "decision": "WAIT",
                "reasons": [f"Kuota advisory Groq dibatasi maksimal {max_groq} kandidat per siklus; keputusan dievaluasi penuh oleh Rule Engine."],
                "cautions": [],
                "ok": False,
                "ai_status": "AI_UNAVAILABLE",
                "cached": False,
                "skipped_by_cycle_cap": True,
            }

        ctx_ai = evaluate_contextual_ai(chosen, ai, allow_paper_on_ai_unavailable=True, advisor_only=True)

        if ctx_ai["final_status"] == "VALID":
            reviewed_id = chosen.plan["id"]
            monitor_ok = self.monitor(report, config, session_token=session_token)
            snap = self.market.load(symbol, mode)
            self._sync_active_ledger()
            acquired = self.clock()
            news_win = self._safe_calendar_window(snap.now_ms)
            account = self.ledger.account(snap.now_ms, news_win, **config.account_args())
            chosen = evaluate_hybrid_adaptive(
                snap,
                account,
                paper=True,
                arming_store=self.arming_store,
                min_m15_candles=min_m15_req,
                min_m5_candles=min_m5_req,
                allow_spot_paper_simulation=(snap.instrument_kind == "SPOT_REFERENCE" and allow_spot_sim),
                futures_unavailable=futures_unavail,
            )
            chosen.meta["mode_comparison"] = comparison
            if not monitor_ok or not chosen.plan or chosen.plan["id"] != reviewed_id:
                chosen.status = "WAIT"
                chosen.plan = None
                if "Harga, setup, atau anggaran berubah saat verifikasi pra-eksekusi" not in chosen.reasons:
                    chosen.reasons.insert(0, "Harga, setup, atau anggaran berubah saat verifikasi pra-eksekusi")
            ctx_ai = evaluate_contextual_ai(chosen, ai, allow_paper_on_ai_unavailable=True, advisor_only=True)

        rule_status = ctx_ai["rule_status"]
        ai_status = ctx_ai["ai_status"]
        advisory_status = ctx_ai.get("advisory_status", ai_status)
        final_status = ctx_ai["final_status"]

        strategy_id = chosen.meta.get("routed_strategy") or (chosen.plan or {}).get("strategy") or (candidate_info or {}).get("strategy_id") or "F"
        regime_name = chosen.meta.get("m15_regime") or (chosen.plan or {}).get("m15_regime") or (candidate_info or {}).get("m15_regime") or "TRANSITION"
        setup_score = int(chosen.metrics.get("setup_quality_score", chosen.metrics.get("confidence_score", (candidate_info or {}).get("m15_score", 0))))

        # P0-7: Record M5 trigger count, Setup Quality Score, Groq status counts, and Hard Gate rejection reasons
        if chosen.meta.get("route_status") == "TRIGGERED":
            p0_diag["m5_trigger_count"] = int(p0_diag.get("m5_trigger_count", 0)) + 1
        p0_diag["setup_quality_scores"].append({
            "symbol": symbol,
            "strategy_id": strategy_id,
            "regime": regime_name,
            "setup_quality_score": setup_score,
            "status": final_status,
        })

        groq_counts = p0_diag.setdefault("groq_status_counts", {"AI_ADVISORY_OK": 0, "AI_CAUTION": 0, "AI_UNAVAILABLE": 0})
        if advisory_status in ("AI_ADVISORY_OK", "AI_CONFIRMED"):
            groq_counts["AI_ADVISORY_OK"] = int(groq_counts.get("AI_ADVISORY_OK", 0)) + 1
            hybrid_funnel["ai_advisory_ok"] = groq_counts["AI_ADVISORY_OK"]
        elif advisory_status == "AI_CAUTION":
            groq_counts["AI_CAUTION"] = int(groq_counts.get("AI_CAUTION", 0)) + 1
            hybrid_funnel["ai_caution"] = groq_counts["AI_CAUTION"]
        else:
            groq_counts["AI_UNAVAILABLE"] = int(groq_counts.get("AI_UNAVAILABLE", 0)) + 1
            hybrid_funnel["ai_unavailable"] = groq_counts["AI_UNAVAILABLE"]

        for chk in (chosen.checks or []):
            if chk.get("Status") not in ("LULUS", "SIMULASI"):
                crit = str(chk.get("Kriteria") or "Unknown Gate")
                p0_diag["hard_gate_rejection_reasons"][crit] = int(p0_diag["hard_gate_rejection_reasons"].get(crit, 0)) + 1

        # Update Mandatory 19-Metric Diagnostic Panel counters for Stage 3
        if chosen.status == "VALID":
            diag_panel["rule_based_valid"] = int(diag_panel.get("rule_based_valid", 0)) + 1
            if ai_status in ("AI_CONFIRMED", "AI_ADVISORY_OK"):
                diag_panel["ai_confirmed"] = int(diag_panel.get("ai_confirmed", 0)) + 1
            elif ai_status == "AI_UNAVAILABLE":
                diag_panel["ai_unavailable"] = int(diag_panel.get("ai_unavailable", 0)) + 1
        else:
            if not ai.get("ok"):
                diag_panel["ai_unavailable"] = int(diag_panel.get("ai_unavailable", 0)) + 1
            rej_cat = chosen.meta.get("rejection_category") or classify_result_rejection_category(chosen)
            if rej_cat in diag_panel:
                diag_panel[rej_cat] = int(diag_panel.get(rej_cat, 0)) + 1
            if rej_cat == "rejected_by_liquidity_or_spread":
                hybrid_funnel["blocked_by_liquidity_or_spread"] += 1
            elif rej_cat == "rejected_by_sl_or_rr":
                hybrid_funnel["blocked_by_sl_or_rr"] += 1
            elif rej_cat in ("rejected_by_position_limit", "rejected_by_risk"):
                hybrid_funnel["blocked_by_risk_or_position_limit"] += 1
            elif rej_cat == "rejected_by_news":
                hybrid_funnel["blocked_by_news"] += 1
            elif rej_cat == "rejected_by_cooldown_or_daily_loss":
                hybrid_funnel["blocked_by_cooldown_or_daily_loss"] += 1

        m15_sc = int((candidate_info or {}).get("m15_score", 0) or setup_score)
        m5_conf = int(chosen.metrics.get("m5_confirmations_passed", 3 if chosen.meta.get("route_status") == "TRIGGERED" else 0))
        wl_stage = classify_watchlist_stage(m15_sc, m5_confirmations=m5_conf, direction=chosen.direction or (candidate_info or {}).get("candidate_direction"))

        # v1.1 Section 2: If no Futures provider is valid, status is FUTURES_DATA_UNAVAILABLE / HOLD (never PAPER_ENTRY)
        if futures_unavail or chosen.meta.get("hold_status") == "HOLD":
            final_status = "HOLD"
            stage_status = "HOLD"
            report["data_status"] = "FUTURES_DATA_UNAVAILABLE" if futures_unavail else report.get("data_status", "OK")
        elif position_limit_reached or chosen.meta.get("rejection_category") == "rejected_by_position_limit":
            stage_status = "ENTRY_BLOCKED_POSITION_LIMIT"
            if final_status == "VALID":
                final_status = "ENTRY_BLOCKED_POSITION_LIMIT"
        elif final_status == "VALID":
            stage_status = "PAPER_ENTRY"
        elif wl_stage == "SETUP_FORMING":
            stage_status = "SETUP_FORMING"
        elif wl_stage == "EARLY_WATCH":
            stage_status = "EARLY_WATCH"
        else:
            stage_status = "WATCHLIST" if final_status == "WAIT" else "REJECTED"

        cand_latency_ms = round(max(0.0, (self.clock() - t_cand_proc_start) * 1000.0), 2)
        q_age_ms = int((candidate_info or {}).get("queue_age_ms", 0))
        p0_diag.setdefault("queue_age_ms", []).append({"symbol": symbol, "queue_age_ms": q_age_ms})
        p0_diag.setdefault("candidate_processing_latency_ms", []).append({"symbol": symbol, "latency_ms": cand_latency_ms})

        m_prov = getattr(snap, "market_provider", None) or getattr(self.market, "market_label", snap.instrument_kind)
        d_ts = int(getattr(snap, "data_timestamp", None) or snap.now_ms)
        r_ts = int(getattr(snap, "received_timestamp", None) or snap.now_ms)
        d_stat = "FUTURES_DATA_UNAVAILABLE" if (futures_unavail or router_futures_unavail) else (chosen.meta.get("data_status") or report.get("data_status", "OK"))
        f_reas = str(getattr(snap, "failover_reason", "") or getattr(self.market, "failover_reason", "") or "")

        data = {
            "market_provider": m_prov,
            "instrument_kind": snap.instrument_kind,
            "symbol": snap.symbol,
            "data_timestamp": d_ts,
            "received_timestamp": r_ts,
            "data_status": d_stat,
            "failover_reason": f_reas,
            "queue_age_ms": q_age_ms,
            "processing_latency_ms": cand_latency_ms,
            "snapshot": snap,
            "result": chosen,
            "ai": ai,
            "status": final_status,
            "rule_status": rule_status,
            "ai_status": ai_status,
            "advisory_status": advisory_status,
            "advisory_notes": ctx_ai.get("advisory_notes", []),
            "advisor_only": True,
            "allow_paper_on_ai_unavailable": True,
            "strategy_id": strategy_id,
            "strategy_engine": "F",
            "regime": regime_name,
            "m15_regime": regime_name,
            "setup_quality_score": setup_score,
            "stage_status": stage_status,
            "watchlist_stage": wl_stage,
            "candidate_info": dict(candidate_info or {}, m5_confirmations_passed=m5_conf, strategy_id=strategy_id, m15_regime=regime_name),
            "monotonic": acquired,
            "paper": (
                "ENTRY_BLOCKED_POSITION_LIMIT: Dua posisi/pending paper aktif; sinyal baru ditahan"
                if stage_status == "ENTRY_BLOCKED_POSITION_LIMIT"
                else "Tidak dicatat: WAIT/NO TRADE"
            ),
            "telegram": "Tidak dikirim",
        }
        report["now_ms"] = max(report["now_ms"], snap.now_ms)
        if data["status"] == "VALID" and not position_limit_reached:
            if self.predictive_store is None or getattr(self, "predictive_store_status", "OK") == "PREDICTIVE_STORE_UNAVAILABLE":
                data["paper_entry_blocked"] = True
                data["paper"] = "DIBLOKIR (FAIL-CLOSED): PREDICTIVE_STORE_UNAVAILABLE"
                data["telegram"] = "Tidak dikirim (PREDICTIVE_STORE_UNAVAILABLE)."
                return data
            queued, reason = self.ledger.queue(data, self.clock)
            if queued:
                data["paper"] = "PENDING · " + reason
                hybrid_funnel["paper_entries_created"] += 1
                data["telegram"] = (
                    self._call_alert("send", data, session_token=session_token, report=report)
                    if config.send_alerts
                    else "Alert Telegram dinonaktifkan."
                )
                self.ledger.telegram_status(reason, data["telegram"])
            else:
                data["paper"] = reason
                if "posisi/pending" in str(reason).lower():
                    data["status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    data["stage_status"] = "ENTRY_BLOCKED_POSITION_LIMIT"
                    diag_panel["rejected_by_position_limit"] = int(diag_panel.get("rejected_by_position_limit", 0)) + 1
                else:
                    data["status"] = "WAIT"
                    data["stage_status"] = "WAITING FOR CONFIRMATION"
        elif (
            config.send_watchlist_alerts
            and hasattr(self.alerts, "send_watchlist")
            and wl_stage in ("EARLY_WATCH", "SETUP_FORMING")
        ):
            data["telegram"] = self._call_alert(
                "send_watchlist",
                data,
                data["candidate_info"],
                stage=wl_stage,
                session_token=session_token,
                report=report,
            )
        history.save(self.ledger.db, chosen, data["status"], ai)
        report["analyses"].append(data)
        report.setdefault("scan_results", []).append({
            "market_provider": m_prov,
            "instrument_kind": snap.instrument_kind,
            "symbol": snap.symbol,
            "data_timestamp": d_ts,
            "received_timestamp": r_ts,
            "data_status": d_stat,
            "failover_reason": f_reas,
            "status": data["status"],
            "stage_status": data["stage_status"],
            "strategy_id": strategy_id,
            "regime": regime_name,
            "setup_quality_score": setup_score,
            "queue_age_ms": q_age_ms,
            "processing_latency_ms": cand_latency_ms,
        })

    def run_early_monitor_cycle(self, symbols, *, now_ms=None, send_alerts=True, session_token=None):
        """Path A lightweight M1/M5/M15 intrabar monitoring for <=50 shortlisted symbols.
        NEVER triggers a full universe scan and NEVER creates PAPER_ENTRY directly.
        """
        cfg = self.predictive_config
        stamp = int(now_ms if now_ms is not None else int(time.time() * 1000))
        if not cfg.predictive_watchlist_enabled:
            return {"enabled": False, "monitored_count": 0}
        capped = [str(s).strip().upper() for s in list(symbols or [])[: cfg.early_monitor_max_candidates] if s]
        self.intrabar_stream.subscribe(capped)
        if self.intrabar_stream.websocket_enabled and not self.intrabar_stream.ws_socket_open:
            self.intrabar_stream.connect_websocket()
        if self.intrabar_stream.health_status().get("websocket_state") == "WEBSOCKET_CONNECTED":
            snapshots = {s: self.intrabar_stream.get_snapshot(s) for s in capped if self.intrabar_stream.get_snapshot(s) is not None}
        else:
            snapshots = self.intrabar_stream.poll_rest_cycle(capped)
        counts = {
            "early_monitored": len(capped),
            "snapshots_received": len(snapshots),
            "pre_watch": 0,
            "early_watch": 0,
            "setup_forming": 0,
            "buy_early_signals": 0,
            "sell_early_signals": 0,
            "signal_transitions": 0,
            "invalidations": 0,
            "direction_changes": 0,
            "missed_entries": 0,
            "alerts_queued": 0,
            "alerts_sent": 0,
            "persistence_errors": [],
        }
        ages = []
        dists = []
        for sym in capped:
            snap = snapshots.get(sym) or self.intrabar_stream.get_snapshot(sym)
            if snap is None:
                continue
            cand = evaluate_early_signal(snap, now_ms=stamp, config=cfg)
            existing = self.predictive_store.get_active_signal(sym) if self.predictive_store else None
            tr = self.predictive_lifecycle.process_early_update(
                existing,
                cand,
                current_price=float(snap.reference_price or snap.last_price),
                now_ms=stamp,
            )
            if tr is None:
                continue
            sig = tr.signal
            if sig.stage == STAGE_PRE_WATCH:
                counts["pre_watch"] += 1
            elif sig.stage == STAGE_EARLY_WATCH:
                counts["early_watch"] += 1
            elif sig.stage == STAGE_SETUP_FORMING:
                counts["setup_forming"] += 1
            if sig.stage in (STAGE_PRE_WATCH, STAGE_EARLY_WATCH, STAGE_SETUP_FORMING):
                if sig.direction == "LONG":
                    counts["buy_early_signals"] += 1
                elif sig.direction == "SHORT":
                    counts["sell_early_signals"] += 1
                ages.append(float(sig.signal_age_seconds))
                dists.append(float(sig.distance_to_trigger_pct))
            for ev in tr.events:
                counts["signal_transitions"] += 1
                et = ev.get("event_type")
                if et == STAGE_SIGNAL_INVALIDATED:
                    counts["invalidations"] += 1
                elif et == STAGE_DIRECTION_CHANGED:
                    counts["direction_changes"] += 1
                elif et == STAGE_MISSED_ENTRY:
                    counts["missed_entries"] += 1
            msg_text = format_predictive_alert(sig, event_type=tr.alert_event_type) if tr.should_alert else None
            if self.predictive_store is not None:
                try:
                    save_res = self.predictive_store.save_transition(tr, alert_message=msg_text, now_ms=stamp)
                    if save_res.get("outbox_queued"):
                        counts["alerts_queued"] += 1
                except PredictiveStoreError as p_exc:
                    counts["persistence_errors"].append(str(p_exc)[:180])

        if send_alerts and self.predictive_store is not None and hasattr(self.alerts, "flush_predictive_outbox"):
            flush_res = self.alerts.flush_predictive_outbox(
                self.predictive_store,
                max_items=cfg.max_early_alerts_per_cycle,
                now_ms=stamp,
                security=self.security,
                session_token=session_token or self.worker_session_token,
            )
            counts["alerts_sent"] = int(flush_res.get("sent", 0))

        counts["avg_signal_age_seconds"] = round(sum(ages) / len(ages), 2) if ages else 0.0
        counts["avg_distance_to_trigger_pct"] = round(sum(dists) / len(dists), 4) if dists else 0.0
        if self.predictive_store is None:
            pred_status = "PREDICTIVE_STORE_UNAVAILABLE"
        elif counts["persistence_errors"]:
            pred_status = "SIGNAL_PERSISTENCE_FAILED"
        elif counts["early_monitored"] == 0:
            pred_status = "MONITOR_OFFLINE"
        elif counts["snapshots_received"] == 0:
            pred_status = "NO_MARKET_DATA"
        elif (counts["pre_watch"] + counts["early_watch"] + counts["setup_forming"]) > 0:
            pred_status = "SIGNALS_ACTIVE"
        else:
            pred_status = "NO_SETUP"
        diag = {
            "enabled": True,
            "predictive_status": pred_status,
            "at_ms": stamp,
            "signal_engine": counts,
            "market_stream": self.intrabar_stream.health_status(),
            "monitor_health": "MONITOR_DEGRADED" if (self.predictive_store is None or counts["persistence_errors"]) else "HEALTHY",
            "signals_persisted": bool(self.predictive_store is not None and not counts["persistence_errors"]),
            "predictive_store_status": self.predictive_store_status if self.predictive_store is None else ("DEGRADED" if counts["persistence_errors"] else "OK"),
            "database": (
                self.predictive_store.health_check()
                if self.predictive_store is not None
                else {"ok": False, "status": "PREDICTIVE_STORE_UNAVAILABLE", "error": self.predictive_store_error}
            ),
        }
        self.last_predictive_diagnostics = diag
        return diag

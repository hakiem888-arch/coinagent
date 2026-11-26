"""Standalone 24/7 background scanner worker. Does not depend on Streamlit browser tabs or Admin session tokens.

BUG-18, BUG-22, BUG-24, BUG-36, BUG-38 + P0-1, P0-2, P0-3, P0-4 + Hybrid Adaptive v1.1 (Section 9):
- Propagates `DATABASE_URL` (`pg_url`) and `SupabaseStateSync` across `PaperLedger`,
  `SecurityManager`, `TelegramAlerts`, and `MarketScanner` so worker state and dashboard
  state remain synchronized across reboots.
- Explicitly defaults to `strategy_profile="BALANCED"` and `strategy_engine="HYBRID_ADAPTIVE_V1"`,
  `interval=60`, `candidates=20`, `shortlist_limit=50`, `max_groq_candidates=5`,
  and synchronizes `strategy_profile`, `relaxed_mode`, `shortlist_limit`, and all operator
  preferences from global `user_preferences` on every cycle without losing preferences on restart (`P0-3`).
- Emits verified worker heartbeats every 30 seconds (stale threshold > 90 seconds) with full runtime proof:
  `worker_id`, `last_heartbeat_ms`, `last_scan_ms`, `next_scan_ms`, `effective_strategy_profile`,
  `effective_strategy_engine`, and `database_name` (Section 9).
"""
import argparse
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from futures.db_backend import (
    database_display_identity,
    database_identity_fingerprint,
    SupabaseStateSync,
    configured_database_url,
    configured_supabase_rest,
    load_global_preferences,
    persistence_status,
)
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.process_lock import ProcessLock
from futures.provider_router import FuturesProviderRouter
from futures.scanner import ALLOWED_SCAN_INTERVALS
from futures.security import SecurityManager
from futures.spot_market import resolve_db_path
from futures.telegram import TelegramAlerts
from futures.version import APP_VERSION

WORKER_HEARTBEAT_INTERVAL_S = 30
WORKER_STALE_THRESHOLD_S = 90


def sync_worker_config_from_preferences(engine_or_ledger, base_config):
    """BUG-38, P0-3 & v1.1 Section 9: Synchronize worker ScannerConfig with saved global `user_preferences`."""
    try:
        target_ledger = getattr(engine_or_ledger, "ledger", engine_or_ledger)
        prefs = load_global_preferences(
            target_ledger.db,
            pg_url=getattr(target_ledger, "pg_url", None),
            get_secret=getattr(target_ledger, "secret_getter", None),
        )
        if not prefs or not prefs.get("_exists"):
            return base_config
        interval_val = int(prefs.get("scan_interval", base_config.interval))
        if interval_val not in ALLOWED_SCAN_INTERVALS:
            interval_val = base_config.interval
        cand_val = max(1, min(50, int(prefs.get("candidates", base_config.candidates))))
        shortlist_val = max(5, min(100, int(prefs.get("shortlist_limit", getattr(base_config, "shortlist_limit", 50)))))
        relaxed_flag = bool(prefs.get("relaxed_mode", base_config.relaxed_mode))
        raw_profile = str(prefs.get("strategy_profile") or ("BALANCED" if relaxed_flag else getattr(base_config, "strategy_profile", "BALANCED"))).strip().upper()
        if raw_profile not in ("CONSERVATIVE", "BALANCED", "ACTIVE"):
            raw_profile = "BALANCED"
        if relaxed_flag and raw_profile == "CONSERVATIVE":
            raw_profile = "BALANCED"
        return replace(
            base_config,
            interval=interval_val,
            candidates=cand_val,
            shortlist_limit=shortlist_val,
            relaxed_mode=relaxed_flag,
            strategy_profile=raw_profile,
            strategy_engine="HYBRID_ADAPTIVE_V1",
            allow_paper_on_ai_unavailable=True,
            advisor_only_ai=True,
            futures_primary_only=True,
            send_alerts=bool(prefs.get("send_alerts", base_config.send_alerts)),
            send_watchlist_alerts=bool(prefs.get("send_watchlist_alerts", base_config.send_watchlist_alerts)),
            send_scan_summary=bool(prefs.get("send_scan_summary", base_config.send_scan_summary)),
            min_volume=float(prefs.get("min_volume", base_config.min_volume)),
            leverage=int(prefs.get("leverage", base_config.leverage)),
            risk_pct=float(prefs.get("risk_pct", base_config.risk_pct)),
            fee=float(prefs.get("fee_pct", base_config.fee * 100.0)) / 100.0,
            slippage_bps=float(prefs.get("slippage_bps", base_config.slippage_bps)),
            funding=float(prefs.get("funding_pct", base_config.funding * 100.0)) / 100.0,
        )
    except Exception:
        return base_config



def execute_worker_monitor_cycle(engine, symbols, monitor_telemetry, *, send_alerts=True):
    """P0-01: Run Path A early monitor cycle WITHOUT swallowing exceptions; tracks full monitor telemetry.
    Marks `monitor_degraded=True` on any exception or persistence failure so worker is NEVER falsely HEALTHY.
    """
    from futures.telegram import redact_telegram_secrets
    req_syms = [str(s).strip().upper() for s in (symbols or []) if s]
    t0 = time.monotonic()
    monitor_telemetry["monitor_symbols_requested"] = len(req_syms)
    try:
        diag = engine.run_early_monitor_cycle(req_syms, send_alerts=send_alerts)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        monitor_telemetry["monitor_latency_ms"] = elapsed_ms
        se = (diag or {}).get("signal_engine") or {}
        snaps_ok = int(se.get("snapshots_received", 0))
        pers_errs = list(se.get("persistence_errors") or [])
        db_ok = bool(((diag or {}).get("database") or {}).get("ok", True))
        monitor_telemetry["monitor_symbols_success"] = snaps_ok
        monitor_telemetry["monitor_symbols_failed"] = max(0, len(req_syms) - snaps_ok)
        if pers_errs or not db_ok:
            monitor_telemetry["monitor_errors"] = int(monitor_telemetry.get("monitor_errors", 0)) + 1
            err_msg = redact_telegram_secrets(str(pers_errs[0] if pers_errs else "PREDICTIVE_STORE_UNAVAILABLE"))[:200]
            monitor_telemetry["monitor_last_error"] = err_msg
            monitor_telemetry["monitor_degraded"] = True
            monitor_telemetry["monitor_health"] = "MONITOR_DEGRADED"
            print(f"[CoinAgentX Worker v{APP_VERSION}] WARN monitor degraded: {err_msg}", flush=True)
            return {"ok": False, "degraded": True, "error": err_msg, "telemetry": dict(monitor_telemetry)}
        monitor_telemetry["monitor_last_success_ms"] = int(time.time() * 1000)
        monitor_telemetry["monitor_degraded"] = False
        monitor_telemetry["monitor_health"] = "HEALTHY"
        return {"ok": True, "degraded": False, "diag": diag, "telemetry": dict(monitor_telemetry)}
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        err_msg = redact_telegram_secrets(f"{type(exc).__name__}: {exc}")[:200]
        monitor_telemetry["monitor_latency_ms"] = elapsed_ms
        monitor_telemetry["monitor_errors"] = int(monitor_telemetry.get("monitor_errors", 0)) + 1
        monitor_telemetry["monitor_last_error"] = err_msg
        monitor_telemetry["monitor_symbols_success"] = 0
        monitor_telemetry["monitor_symbols_failed"] = len(req_syms)
        monitor_telemetry["monitor_degraded"] = True
        monitor_telemetry["monitor_health"] = "MONITOR_DEGRADED"
        print(f"[CoinAgentX Worker v{APP_VERSION}] ERROR early monitor cycle failed: {err_msg}", flush=True)
        return {"ok": False, "degraded": True, "error": err_msg, "telemetry": dict(monitor_telemetry)}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Single-flight market scanner worker (Hybrid Adaptive v1.1)")
    p.add_argument("--interval", type=int, default=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    p.add_argument("--monitor-interval", type=int, default=int(os.getenv("EARLY_MONITOR_INTERVAL_SECONDS", "10")))
    p.add_argument("--heartbeat-interval", type=int, default=int(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "30")))
    p.add_argument("--candidates", type=int, default=int(os.getenv("SCAN_CANDIDATES", "20")))
    p.add_argument("--shortlist-limit", type=int, default=int(os.getenv("SCAN_SHORTLIST_LIMIT", "50")))
    p.add_argument("--max-groq-candidates", type=int, default=int(os.getenv("SCAN_MAX_GROQ_CANDIDATES", "5")))
    p.add_argument("--strategy-profile", default=os.getenv("STRATEGY_PROFILE", "BALANCED"))
    p.add_argument("--min-volume", type=float, default=float(os.getenv("SCAN_MIN_VOLUME", "5000000")))
    p.add_argument("--leverage", type=int, default=int(os.getenv("PAPER_LEVERAGE", "2")))
    p.add_argument("--risk-pct", type=float, default=float(os.getenv("PAPER_RISK_PCT", "0.25")))
    p.add_argument("--fee", type=float, default=float(os.getenv("PAPER_TAKER_FEE", "0.0005")))
    p.add_argument("--slippage-bps", type=float, default=float(os.getenv("PAPER_SLIPPAGE_BPS", "2")))
    p.add_argument("--funding", type=float, default=float(os.getenv("PAPER_FUNDING_RESERVE", "0.0003")))
    p.add_argument("--initial-equity", type=float, default=float(os.getenv("PAPER_INITIAL_EQUITY", "1000")))
    p.add_argument("--db", default="")
    p.add_argument("--model", default=os.getenv("GROQ_MODEL", DEFAULT_MODEL))
    p.add_argument("--no-alerts", "--no-telegram", dest="no_alerts", action="store_true")
    p.add_argument("--once", action="store_true")
    return p.parse_args(argv)


def create_worker_engine(args, root=None, secret_getter=None):
    """Build a standalone MarketScanner wired to the centralized persistence layer with Strategy F (BALANCED) as default."""
    root = Path(root or Path(__file__).resolve().parent)
    getter = secret_getter or os.environ.get
    from futures.market import FuturesMarket
    disable_multi = str(os.getenv("COINAGENT_DISABLE_MULTI_PROVIDER", "")).strip().lower() in ("1", "true", "yes")
    market = FuturesMarket() if disable_multi else FuturesProviderRouter()
    initial_kind = getattr(market, "instrument_kind", "USD_M_PERPETUAL")
    db_path = Path(args.db) if getattr(args, "db", "") else resolve_db_path(root, initial_kind)

    pg_url = configured_database_url(get_secret=getter)
    sb_url, sb_key = configured_supabase_rest(get_secret=getter)
    sb_sync = SupabaseStateSync(sb_url, sb_key)

    ledger = PaperLedger(
        db_path,
        args.initial_equity,
        expected_instrument_kind=initial_kind,
        pg_url=pg_url,
        secret_getter=getter,
        supabase_sync=sb_sync,
    )
    security = SecurityManager(db_path, pg_url=pg_url, secret_getter=getter)
    pool = GroqRotation(load_keys(getter), security=security)
    alerts = TelegramAlerts(
        getter("TELEGRAM_BOT_TOKEN"),
        getter("TELEGRAM_CHAT_ID"),
        db_path,
        security=security,
        pg_url=pg_url,
        secret_getter=getter,
    )
    scanner = MarketScanner(
        market,
        pool,
        NewsCalendar(),
        ledger,
        alerts,
        model=args.model,
        security=security,
        trusted_worker=True,
    )
    raw_prof = str(getattr(args, "strategy_profile", "BALANCED") or "BALANCED").strip().upper()
    if raw_prof not in ("CONSERVATIVE", "BALANCED", "ACTIVE"):
        raw_prof = "BALANCED"
    config = ScannerConfig(
        candidates=args.candidates,
        shortlist_limit=int(getattr(args, "shortlist_limit", 50)),
        max_groq_candidates=int(getattr(args, "max_groq_candidates", 5)),
        min_volume=args.min_volume,
        fee=args.fee,
        slippage_bps=args.slippage_bps,
        funding=args.funding,
        leverage=args.leverage,
        risk_pct=args.risk_pct,
        send_alerts=not args.no_alerts,
        relaxed_mode=(raw_prof in ("BALANCED", "ACTIVE")),
        strategy_profile=raw_prof,
        strategy_engine="HYBRID_ADAPTIVE_V1",
        allow_paper_on_ai_unavailable=True,
        advisor_only_ai=True,
        futures_primary_only=True,
        interval=args.interval,
    )
    config = sync_worker_config_from_preferences(ledger, config)
    return scanner, config


def main(argv=None):
    args = parse_args(argv)
    if args.interval < 30 or args.interval > 3600:
        raise SystemExit(f"Interval harus salah satu dari {ALLOWED_SCAN_INTERVALS} detik")
    hb_interval_s = max(5, min(30, int(getattr(args, "heartbeat_interval", WORKER_HEARTBEAT_INTERVAL_S))))
    root = Path(__file__).resolve().parent
    engine, config = create_worker_engine(args, root=root)
    worker_id = f"worker-{os.getpid()}-{uuid4().hex[:6]}"

    health = engine.ledger.integrity_check()
    p_info = persistence_status(
        db_path=engine.ledger.db,
        get_secret=os.environ.get,
        pg_url=engine.ledger.pg_url,
        ledger=engine.ledger,
        supabase_sync=engine.ledger.supabase_sync,
    )
    print(
        f"[CoinAgentX Worker v{APP_VERSION}] worker_id={worker_id} "
        f"backend={p_info['backend']} status={p_info.get('status', p_info.get('backend', 'OK'))} "
        f"db_healthy={health.get('ok')} db={database_display_identity(engine.ledger.db, pg_url=engine.ledger.pg_url, get_secret=os.environ.get)} fp={database_identity_fingerprint(engine.ledger.db, pg_url=engine.ledger.pg_url, get_secret=os.environ.get)} "
        f"effective_strategy_profile={config.strategy_profile} strategy_engine={config.strategy_engine} "
        f"heartbeat_interval={hb_interval_s}s scan_interval={config.interval}s",
        flush=True,
    )

    last_scan_ms = int(time.time() * 1000)
    next_scan_ms = last_scan_ms + int(config.interval) * 1000
    stop_hb_event = threading.Event()

    monitor_telemetry = {
        "monitor_errors": 0,
        "monitor_last_error": "",
        "monitor_last_success_ms": 0,
        "monitor_symbols_requested": 0,
        "monitor_symbols_success": 0,
        "monitor_symbols_failed": 0,
        "monitor_latency_ms": 0.0,
        "monitor_degraded": False,
    }
    db_fp = database_identity_fingerprint(engine.ledger.db, pg_url=engine.ledger.pg_url, get_secret=os.environ.get)
    worker_scans_completed = 0

    def _emit_heartbeat(now_override_ms=None, state_override=None):
        stamp = int(now_override_ms if now_override_ms is not None else int(time.time() * 1000))
        eff_st = state_override or ("WORKER_DEGRADED" if monitor_telemetry.get("monitor_degraded") else "WORKER_ONLINE")
        try:
            hb_res = engine.ledger.record_worker_heartbeat(
                worker_id,
                stamp,
                config.interval,
                strategy_profile=config.strategy_profile,
                strategy_engine=config.strategy_engine,
                last_scan_ms=last_scan_ms,
                next_scan_ms=next_scan_ms,
                database_name=database_display_identity(engine.ledger.db, pg_url=engine.ledger.pg_url, get_secret=os.environ.get),
                database_fingerprint=db_fp,
                worker_state=eff_st,
                monitor_degraded=bool(monitor_telemetry.get("monitor_degraded")),
                monitor_telemetry=dict(monitor_telemetry),
                worker_scans=worker_scans_completed,
            )
            if isinstance(hb_res, dict) and not hb_res.get("verified", True):
                print(f"[CoinAgentX Worker v{APP_VERSION}] WARN heartbeat readback verification failed!", flush=True)
        except Exception as hb_exc:
            print(f"[CoinAgentX Worker v{APP_VERSION}] WARN heartbeat write failed: {hb_exc}", flush=True)

    def _heartbeat_daemon():
        while not stop_hb_event.wait(hb_interval_s):
            _emit_heartbeat()

    worker_lease = ProcessLock(Path(str(engine.ledger.db) + ".worker_lease"))
    worker_lease.acquire()
    _emit_heartbeat(state_override='WORKER_STARTING')
    if hasattr(engine.ledger, 'record_monitor_heartbeat'):
        engine.ledger.record_monitor_heartbeat(worker_id, int(time.time() * 1000), monitor_state='MONITOR_STARTING', database_fingerprint=db_fp)
    monitor_interval_s = max(5, min(60, int(getattr(args, "monitor_interval", 10))))
    last_shortlist_symbols = []
    if not args.once:
        hb_thread = threading.Thread(target=_heartbeat_daemon, name="worker-heartbeat-30s", daemon=True)
        hb_thread.start()

    try:
        while True:
            started = time.monotonic()
            now_ms = int(time.time() * 1000)
            config = sync_worker_config_from_preferences(engine.ledger, config)
            last_scan_ms = now_ms
            next_scan_ms = now_ms + int(config.interval) * 1000
            _emit_heartbeat(now_ms)

            import inspect
            run_params = inspect.signature(engine.run).parameters
            if "worker_id" in run_params:
                report = engine.run(config, worker_id=worker_id)
            else:
                report = engine.run(config)
            if isinstance(report, dict) and (report.get("market_error") or {}).get("access_blocked"):
                http_st = (report.get("market_error") or {}).get("http_status", 451)
                print(
                    f"[CoinAgentX Worker v{APP_VERSION}] STOPPED_HTTP_{http_st} "
                    f"started_at_ms={report.get('started_at_ms', 0)} "
                    "Akses data pasar diblokir; menghentikan loop retry otomatis.",
                    flush=True,
                )
                return 2

            worker_scans_completed += 1
            completed_ms = int(time.time() * 1000)
            next_scan_ms = int(report.get("next_scan_at_ms") or (last_scan_ms + int(config.interval) * 1000))
            if isinstance(report, dict) and report.get("shortlist"):
                last_shortlist_symbols = [r.get("symbol") for r in report.get("shortlist", []) if isinstance(r, dict) and r.get("symbol")][:50]
            if last_shortlist_symbols and hasattr(engine, "run_early_monitor_cycle"):
                mon_res = execute_worker_monitor_cycle(engine, last_shortlist_symbols, monitor_telemetry, send_alerts=config.send_alerts)
                if hasattr(engine.ledger, "record_monitor_heartbeat"):
                    _ms_st = ((mon_res.get("diag") or {}).get("market_stream") or {})
                    _m_state = (
                        "MONITOR_DEGRADED" if mon_res.get("degraded")
                        else ("MONITOR_ONLINE" if (_ms_st.get("feed_active") or monitor_telemetry.get("monitor_symbols_success", 0) > 0) else "MONITOR_DATA_UNAVAILABLE")
                    )
                    engine.ledger.record_monitor_heartbeat(
                        worker_id,
                        completed_ms,
                        monitor_state=_m_state,
                        symbols_requested=monitor_telemetry.get("monitor_symbols_requested", 0),
                        symbols_success=monitor_telemetry.get("monitor_symbols_success", 0),
                        feed_active=bool(_ms_st.get("feed_active")),
                        websocket_state=_ms_st.get("websocket_state", "DISCONNECTED"),
                        database_fingerprint=db_fp,
                    )
            _emit_heartbeat(completed_ms)

            valid = sum(d.get("status") == "VALID" for d in report.get("analyses", []))
            print(
                f"[CoinAgentX Worker v{APP_VERSION}] scan={report.get('scan_id', '-')}"
                f" provider={report.get('market_provider', getattr(engine.market, 'market_label', 'Unknown'))}"
                f" kind={report.get('instrument_kind', getattr(engine.market, 'instrument_kind', 'USD_M_PERPETUAL'))}"
                f" profile={report.get('effective_strategy_profile', config.strategy_profile)}"
                f" engine={report.get('strategy_engine', 'HYBRID_ADAPTIVE_V1')}"
                f" db={Path(report.get('ledger_db', str(engine.ledger.db))).name}"
                f" market={report.get('universe_count', 0)}"
                f" shortlist={len(report.get('shortlist', []))}"
                f" deep={len(report.get('selected', []))}"
                f" analyzed={report.get('analyzed_count', len(report.get('analyses', [])))}"
                f" deferred={report.get('deferred_count', 0)}"
                f" valid={valid}"
                f" interval={config.interval}s"
                f" errors={len(report.get('errors', []))}",
                flush=True,
            )
            if isinstance(report, dict) and report.get("shortlist"):
                last_shortlist_symbols = [r.get("symbol") for r in report.get("shortlist", []) if isinstance(r, dict) and r.get("symbol")][:50]
            if args.once:
                return 0
            remaining_sleep = max(1.0, config.interval - (time.monotonic() - started))
            next_hb_t = time.monotonic() + float(hb_interval_s)
            while remaining_sleep > 0:
                chunk = min(float(monitor_interval_s), float(hb_interval_s), remaining_sleep)
                time.sleep(chunk)
                remaining_sleep -= chunk
                if last_shortlist_symbols and hasattr(engine, "run_early_monitor_cycle"):
                    mon_res = execute_worker_monitor_cycle(
                        engine,
                        last_shortlist_symbols,
                        monitor_telemetry,
                        send_alerts=config.send_alerts,
                    )
                    if mon_res.get("degraded"):
                        _emit_heartbeat(state_override="WORKER_DEGRADED")
                if time.monotonic() >= next_hb_t:
                    _emit_heartbeat()
                    next_hb_t = time.monotonic() + float(hb_interval_s)
    finally:
        stop_hb_event.set()
        if not args.once:
            try:
                _end_ms = int(time.time() * 1000)
                if hasattr(engine.ledger, "record_monitor_heartbeat"):
                    engine.ledger.record_monitor_heartbeat(worker_id, _end_ms, monitor_state="MONITOR_OFFLINE", database_fingerprint=db_fp, detail="Graceful shutdown")
                _emit_heartbeat(_end_ms, state_override="WORKER_OFFLINE")
            except Exception:
                pass
        worker_lease.release()


if __name__ == "__main__":
    sys.exit(main())

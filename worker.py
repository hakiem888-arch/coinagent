"""Optional persistent-host worker: python worker.py --interval 300 --medium-liquidity-fallback.

Reads local .streamlit/secrets.toml or environment variables. Community Cloud
Secrets cannot be read remotely by this process on another host.
"""
import argparse
import json
import os
import signal
import threading
import time
import tomllib
from pathlib import Path
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.provider_router import FuturesProviderRouter
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar
from futures.paper import PaperLedger
from futures.telegram import TelegramAlerts
from futures.scanner import ALLOWED_SCAN_INTERVALS


def main():
    parser = argparse.ArgumentParser(description="Automatic USDT perpetual PAPER worker (Binance primary, Bybit fallback); no live exchange orders")
    parser.add_argument("--interval", type=int, choices=list(ALLOWED_SCAN_INTERVALS), default=300)
    parser.add_argument("--candidates", type=int, choices=[1, 2], default=2)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--medium-liquidity-fallback", "--relaxed", dest="medium_fallback", action="store_true", help="Allow 5M USDT liquidity fallback for watchlist only; entry gates remain strict")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    path = root / ".streamlit" / "secrets.toml"
    settings = tomllib.loads(path.read_text()) if path.exists() else {}
    get = lambda name: settings.get(name, os.getenv(name, ""))
    db = Path(get("FUTURES_HISTORY_DB") or (root / "data" / "paper_history.sqlite3"))
    ledger = PaperLedger(db, float(get("PAPER_INITIAL_EQUITY") or 1000))
    pool = GroqRotation(load_keys(get))
    engine = MarketScanner(FuturesProviderRouter(), pool, NewsCalendar(), ledger,
                           TelegramAlerts(get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_CHAT_ID"), db),
                           str(get("GROQ_MODEL") or DEFAULT_MODEL))
    relaxed_env = str(get("MEDIUM_LIQUIDITY_FALLBACK")).lower() in ("1", "true", "yes", "on")
    config = ScannerConfig(candidates=args.candidates, interval=args.interval,
                           send_alerts=not args.no_telegram,
                           send_watchlist_alerts=not args.no_telegram,
                           send_scan_summary=not args.no_telegram,
                           relaxed_mode=bool(args.medium_fallback or relaxed_env))
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        started = time.monotonic()
        try:
            report = engine.run(config)
            print(json.dumps({"scope":"PAPER", "provider":engine.market.market_label, "scan_id":report.get("scan_id"),
                "started_at_ms":report.get("started_at_ms"), "scan_status":report.get("scan_status"),
                "market_error":report.get("market_error", {}),
                "at_ms":report.get("now_ms"), "universe":report.get("universe_count"),
                "shortlist":len(report.get("shortlist", [])), "candidates":len(report.get("selected", [])),
                "analyses":len(report.get("analyses", [])), "errors":report.get("errors", [])}, ensure_ascii=False), flush=True)
            if report.get("market_error", {}).get("access_blocked"):
                print(json.dumps({"scope": "PAPER", "status": "STOPPED_HTTP_451",
                    "message": "Kedua provider gagal. Periksa akses jaringan dan pembatasan layanan; worker dihentikan."}), flush=True)
                return 2
        except Exception as exc:
            # Never print provider responses, tokens, or credential-bearing URLs.
            print(json.dumps({"scope":"PAPER", "status":"WAIT", "error_type":type(exc).__name__}), flush=True)
        if args.once:
            break
        elapsed = time.monotonic() - started
        # No catch-up loop if a cycle took longer than the configured interval.
        stop.wait(max(1, args.interval - elapsed) if elapsed < args.interval else args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

"""Optional lightweight 24/7 scanner worker for CoinAgentX Simple Reversal."""
from __future__ import annotations

import argparse
import json
import os
import time

from simple_engine import ScanSettings, SimpleTradingEngine


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="CoinAgentX Simple Reversal worker (paper only)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    parser.add_argument("--symbols", type=int, default=int(os.getenv("SCAN_SYMBOLS", "50")))
    parser.add_argument("--initial-equity", type=float, default=float(os.getenv("PAPER_INITIAL_EQUITY", "1000")))
    parser.add_argument("--risk-pct", type=float, default=float(os.getenv("PAPER_RISK_PCT", "0.5")))
    parser.add_argument("--leverage", type=int, default=int(os.getenv("PAPER_LEVERAGE", "2")))
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.interval < 30:
        raise SystemExit("--interval minimal 30 detik")
    engine = SimpleTradingEngine(secret_getter=os.environ.get)
    settings = ScanSettings(
        symbols=max(1, min(200, args.symbols)), interval_seconds=args.interval,
        initial_equity=max(50.0, args.initial_equity), risk_pct=min(2.0, max(0.1, args.risk_pct)),
        leverage=max(1, min(20, args.leverage)), send_telegram=not args.no_telegram,
    )
    print("CoinAgentX Simple Reversal worker dimulai · PAPER ONLY", flush=True)
    while True:
        started = time.monotonic()
        report = engine.scan(settings)
        compact = {k: report.get(k) for k in ("status", "provider", "instrument_kind", "scanned", "failed", "completed_ms", "error", "note")}
        compact["new_signals"] = [
            {"symbol": s["symbol"], "direction": s["direction"], "entry": s["entry"], "stop": s["stop"], "target": s["target"]}
            for s in report.get("signals", [])
        ]
        print(json.dumps(compact, ensure_ascii=False), flush=True)
        if args.once:
            break
        sleep_for = max(1.0, args.interval - (time.monotonic() - started))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
